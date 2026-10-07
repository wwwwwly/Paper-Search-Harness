"""Root-owned, bounded asynchronous research tasks; durable state lives in the board."""

from __future__ import annotations

import asyncio

from paper_search.diagnostics import current_tool_call
from paper_search.domain import (
    Message,
    Run,
    RunStatus,
    TaskAssignment,
    ToolInputError,
    WorkerTask,
)
from paper_search.research import current_basis
from paper_search.run_lifecycle import transition_run


class ResearchCoordinator:
    def __init__(self, runner, root: Run, budget):
        self.runner, self.root, self.budget = runner, root, budget
        self.slots = asyncio.Semaphore(root.execution.limits.max_concurrent_workers)
        self.running: dict[str, asyncio.Task] = {}
        self.preserve = False
        self.closing = False

    async def spawn(self, assignment: TaskAssignment):
        if self.root.execution.effort == "quick" or self.budget.research_seconds <= 0:
            raise ToolInputError("当前档位或剩余时间不允许委派")
        async with self.runner.uow() as tx:
            brief = await current_basis(tx, self.root)
            board = await tx.research.get(self.root.session_id)
            known = {
                item.id: item
                for item in board.tasks
                if item.root_run_id == self.root.id
            }
            if any(
                key not in {item.id for item in board.questions}
                for key in assignment.question_ids
            ):
                raise ToolInputError("任务引用了不存在的研究问题")
            if any(key not in known for key in assignment.depends_on):
                raise ToolInputError(
                    "依赖必须引用本根 Run 已存在的任务；新任务不能形成循环"
                )
            if assignment.kind == "evidence" and not assignment.paper_ids:
                raise ToolInputError("证据任务需要已知论文 ID")
            call_id = current_tool_call.get()
            task_id = f"research-{self.root.id}-{call_id}" if call_id else None
            existing = known.get(task_id)
            if existing:
                return existing
            task = WorkerTask(
                **({"id": task_id} if task_id else {}),
                root_run_id=self.root.id,
                assignment=assignment,
                input_revision=brief.input_revision,
                basis_revision=brief.basis_revision,
            )
            child = Run(
                id=task.id,
                session_id=self.root.session_id,
                parent_id=self.root.id,
                execution=self.root.execution,
                input_revision=brief.input_revision,
                deadline_at=self.root.deadline_at,
                token_soft_target=self.root.token_soft_target,
                task_revision=task.task_revision,
                brief=brief.model_copy(
                    update={
                        "objective": assignment.goal,
                        "open_questions": (assignment.goal,),
                    }
                ),
            )
            await tx.runs.save(child)
            await tx.sessions.append_message(
                Message(
                    session_id=child.session_id,
                    run_id=child.id,
                    role="user",
                    content="研究任务：\n" + assignment.model_dump_json(),
                )
            )
            board = board.model_copy(update={"tasks": (*board.tasks, task)})
            await tx.research.save(board)
            await self._changed(tx, board)
        self.launch(task.id)
        return task

    def launch(self, task_id):
        if task_id not in self.running or self.running[task_id].done():
            task = asyncio.create_task(self._run(task_id), name=f"research:{task_id}")
            self.running[task_id] = task
            task.add_done_callback(
                lambda completed: asyncio.create_task(
                    self._continue_queued(task_id, completed)
                )
            )

    async def _continue_queued(self, key, completed):
        # A follow-up can arrive after execute() returns but before its owner coroutine exits.
        if self.closing or self.running.get(key) is not completed:
            return
        async with self.runner.uow() as tx:
            board = await tx.research.get(self.root.session_id)
            task = next(item for item in board.tasks if item.id == key)
        if (
            not self.closing
            and task.status == "queued"
            and self.budget.research_seconds > 0
        ):
            self.launch(key)

    async def recover(self):
        if self.root.execution.limits.max_concurrent_workers == 0:
            return
        async with self.runner.uow() as tx:
            board = await tx.research.get(self.root.session_id)
        for item in board.tasks:
            if item.root_run_id == self.root.id and item.status in {
                "queued",
                "running",
                "waiting",
            }:
                async with self.runner.uow() as tx:
                    child = await tx.runs.get(item.id)
                    if child.status in {
                        RunStatus.INTERRUPTED,
                        RunStatus.WAITING,
                        RunStatus.FAILED,
                    }:
                        await transition_run(
                            tx, child.id, RunStatus.QUEUED, reason="root_recovered"
                        )
                self.launch(item.id)

    async def _run(self, key):
        try:
            while self.budget.research_seconds > 0:
                async with self.runner.uow() as tx:
                    board = await tx.research.get(self.root.session_id)
                    task = next(item for item in board.tasks if item.id == key)
                    session = await tx.sessions.get(self.root.session_id)
                    root = await tx.runs.get(self.root.id)
                if task.status in {"cancelled", "stale"}:
                    return
                if task.basis_revision != session.brief.basis_revision:
                    await self._status(key, "stale", "研究依据已改变")
                    return
                # Corrections suspend new paid calls until the lead interprets the new input.
                if session.brief.input_revision != session.input_revision:
                    await asyncio.sleep(0.2)
                    continue
                if root.status == RunStatus.CANCELLED:
                    raise asyncio.CancelledError
                dependencies = [
                    item
                    for item in board.tasks
                    if item.id in task.assignment.depends_on
                ]
                if any(
                    item.status in {"failed", "cancelled", "stale", "blocked"}
                    for item in dependencies
                ):
                    await self._status(key, "blocked", "依赖失败或已取消；请调整任务")
                    return
                if all(item.status == "done" for item in dependencies):
                    break
                await asyncio.sleep(0.2)
            else:
                await self._status(key, "blocked", "研究期限已到")
                return
            async with self.slots:
                await self._status(key, "running")
                await self.runner.execute(key, budget=self.budget)
                async with self.runner.uow() as tx:
                    child = await tx.runs.get(key)
                    board = await tx.research.get(self.root.session_id)
                    latest = next(item for item in board.tasks if item.id == key)
                    if (
                        latest.task_revision > child.task_revision
                        or child.status == RunStatus.QUEUED
                    ):
                        return
                    status = (
                        "done"
                        if child.status == RunStatus.COMPLETED
                        else "cancelled"
                        if child.status == RunStatus.CANCELLED
                        else "waiting"
                        if child.status in {RunStatus.WAITING, RunStatus.INTERRUPTED}
                        else "failed"
                    )
                    await self._save_status(tx, board, key, status, child.reason)
        except asyncio.CancelledError:
            async with self.runner.uow() as tx:
                child = await tx.runs.get(key)
            await self._status(
                key,
                "done"
                if child.status == RunStatus.COMPLETED
                else "waiting"
                if self.preserve
                else "cancelled",
                child.reason
                if child.status == RunStatus.COMPLETED
                else "根任务中断，可恢复"
                if self.preserve
                else "根任务结束或取消",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - isolate one Worker without losing other results
            await self._status(key, "failed", str(exc))

    async def _changed(self, tx, board):
        await tx.sessions.append_event(
            self.root.session_id,
            self.root.id,
            "research.updated",
            {"research": board.model_dump(mode="json")},
        )

    async def _status(self, key, status, reason=None):
        async with self.runner.uow() as tx:
            board = await tx.research.get(self.root.session_id)
            await self._save_status(tx, board, key, status, reason)

    async def _save_status(self, tx, board, key, status, reason):
        tasks = tuple(
            item.model_copy(update={"status": status, "reason": reason or ""})
            if item.id == key and item.status not in {"cancelled", "stale"}
            else item
            for item in board.tasks
        )
        board = board.model_copy(update={"tasks": tasks})
        await tx.research.save(board)
        if status in {"cancelled", "stale"}:
            child = await tx.runs.get(key)
            if child.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                await transition_run(tx, key, RunStatus.CANCELLED, reason=reason)
        await self._changed(tx, board)

    async def collect(self, task_ids: tuple[str, ...], after_cursor: int, wait_ms: int):
        end = asyncio.get_running_loop().time() + min(
            30, wait_ms / 1000, self.budget.research_seconds
        )
        while True:
            async with self.runner.uow() as tx:
                board = await tx.research.get(self.root.session_id)
            owned = [item for item in board.tasks if item.root_run_id == self.root.id]
            if task_ids and not set(task_ids) <= {item.id for item in owned}:
                raise ToolInputError("任务不属于当前根 Run")
            tasks = [item for item in owned if not task_ids or item.id in task_ids]
            findings = [
                item
                for item in board.findings
                if item.cursor > after_cursor
                and item.run_id in {task.id for task in tasks}
            ]
            if (
                findings
                or all(
                    item.status in {"done", "blocked", "failed", "cancelled", "stale"}
                    for item in tasks
                )
                or asyncio.get_running_loop().time() >= end
            ):
                return {
                    "tasks": tasks,
                    "findings": findings,
                    "cursor": max(
                        (item.cursor for item in findings), default=after_cursor
                    ),
                }
            await asyncio.sleep(0.2)

    async def send(self, key: str, instruction: str):
        async with self.runner.uow() as tx:
            brief = await current_basis(tx, self.root)
            board = await tx.research.get(self.root.session_id)
            task = next(
                (
                    item
                    for item in board.tasks
                    if item.id == key and item.root_run_id == self.root.id
                ),
                None,
            )
            if not task or task.basis_revision != brief.basis_revision:
                raise ToolInputError("任务不存在或研究依据已过期")
            if task.status in {"cancelled", "stale"}:
                raise ToolInputError("已取消任务不能续接；请创建新任务")
            update_id = current_tool_call.get()
            if update_id and update_id in task.applied_update_ids:
                return task
            child = await tx.runs.get(key)
            terminal_child = child.status in {
                RunStatus.COMPLETED,
                RunStatus.WAITING,
                RunStatus.INTERRUPTED,
                RunStatus.FAILED,
            }
            task = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "instructions": (*task.instructions, instruction),
                    "applied_update_ids": (*task.applied_update_ids, update_id)
                    if update_id
                    else task.applied_update_ids,
                    "input_revision": brief.input_revision,
                    "status": "queued"
                    if terminal_child
                    or task.status in {"done", "failed", "blocked", "waiting"}
                    else task.status,
                }
            )
            await tx.research.save(
                board.model_copy(
                    update={
                        "tasks": tuple(
                            task if item.id == key else item for item in board.tasks
                        )
                    }
                )
            )
            child = await tx.runs.get(key)
            await tx.runs.save(
                child.model_copy(
                    update={
                        "input_revision": brief.input_revision,
                        "brief": child.brief.model_copy(
                            update={"input_revision": brief.input_revision}
                        ),
                    }
                )
            )
            if task.status == "queued" and child.status != RunStatus.QUEUED:
                await transition_run(tx, key, RunStatus.QUEUED, reason="task_updated")
            await self._changed(tx, await tx.research.get(self.root.session_id))
            await tx.sessions.append_message(
                Message(
                    session_id=child.session_id,
                    run_id=key,
                    role="user",
                    content="追加指令：" + instruction,
                )
            )
        self.launch(key)
        return task

    async def cancel(self, key, reason):
        async with self.runner.uow() as tx:
            board = await tx.research.get(self.root.session_id)
            if not any(
                item.id == key and item.root_run_id == self.root.id
                for item in board.tasks
            ):
                raise ToolInputError("任务不属于当前根 Run")
            await tx.research.save(
                board.model_copy(
                    update={
                        "tasks": tuple(
                            item.model_copy(
                                update={"status": "cancelled", "reason": reason}
                            )
                            if item.id == key
                            else item
                            for item in board.tasks
                        )
                    }
                )
            )
            child = await tx.runs.get(key)
            if child.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                await transition_run(tx, key, RunStatus.CANCELLED, reason=reason)
            await self._changed(tx, await tx.research.get(self.root.session_id))
        if key in self.running:
            self.running[key].cancel()
        if self.runner.stop_documents:
            await self.runner.stop_documents(key)
        return {"task_id": key, "status": "cancelled"}

    async def close(self):
        self.closing = True
        async with self.runner.uow() as tx:
            root = await tx.runs.get(self.root.id)
        self.preserve = root.deadline_at != self.root.deadline_at or root.status in {
            RunStatus.INTERRUPTED,
            RunStatus.WAITING,
            RunStatus.FAILED,
        }
        tasks = [task for task in self.running.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
