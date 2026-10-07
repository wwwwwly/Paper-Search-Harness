from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from paper_search.domain import (
    Conflict,
    Effort,
    ExecutionSettings,
    Job,
    Message,
    Run,
    RunStatus,
    Session,
    ToolResult,
    now,
)
from paper_search.ports import UowFactory
from paper_search.run_lifecycle import transition_run


class SessionService:
    def __init__(
        self,
        uow: UowFactory,
        cancel_execution: Callable[[str], None],
        execution_for: Callable[[Effort], ExecutionSettings],
    ):
        self.uow, self.cancel_execution = uow, cancel_execution
        self.execution_for = execution_for

    async def create(self, title: str, effort: Effort = "quick") -> Session:
        self.execution_for(effort)
        session = Session(title=title, effort=effort)
        async with self.uow() as tx:
            await tx.sessions.save(session)
            await tx.sessions.append_event(
                session.id,
                None,
                "session.created",
                {"session": session.model_dump(mode="json")},
            )
        return session

    async def send(
        self,
        session_id: str,
        text: str,
        client_id: str,
        *,
        document_ids: tuple[str, ...] = (),
        effort: Effort | None = None,
    ) -> Message:
        async with self.uow() as tx:
            session = await tx.sessions.get(session_id)
            previous = await tx.sessions.find_client_message(session_id, client_id)
            if previous:
                if (
                    previous.content != text
                    or previous.document_ids != document_ids
                    or previous.effort != effort
                ):
                    raise Conflict("同一客户端消息 ID 不能用于不同内容")
                return previous
            for document_id in document_ids:
                document = await tx.documents.get(document_id)
                if document.session_id != session_id:
                    raise Conflict("附件不属于此会话")
            roots = [
                item for item in await tx.runs.list(session_id) if not item.parent_id
            ]
            run = max(roots, key=lambda item: item.created_at) if roots else None
            received_at = now()
            if run is None:
                execution = self.execution_for(effort or session.effort)
                run = Run(
                    session_id=session_id,
                    execution=execution,
                    token_soft_target=execution.limits.soft_token_target,
                )
                await tx.runs.save(run)
                await tx.jobs.enqueue(
                    Job(
                        id=f"run-{run.id}",
                        kind="run",
                        session_id=session_id,
                        target_id=run.id,
                    )
                )
            elif run.status not in {RunStatus.RUNNING, RunStatus.QUEUED} or (
                run.deadline_at is not None
                and run.deadline_at
                <= received_at
                + timedelta(seconds=run.execution.limits.finalization_reserve_seconds)
            ):
                # A new human message begins another execution segment in the same Run.
                # Automatic recovery and mid-flight steering never renew that budget.
                execution = self.execution_for(effort or session.effort)
                run = await transition_run(
                    tx,
                    run.id,
                    RunStatus.QUEUED,
                    reason="new_user_input",
                    updates={
                        "execution": execution,
                        "budget_extensions": run.budget_extensions + 1,
                        "token_soft_target": run.token_soft_target
                        + execution.limits.soft_token_target,
                        "deadline_at": received_at
                        + timedelta(seconds=execution.limits.deadline_seconds),
                        "waiting_document_ids": (),
                        "waiting_since": None,
                    },
                )
                # Cancel an older coroutine before the new delivery becomes visible.
                self.cancel_execution(run.id)
                await tx.jobs.enqueue(
                    Job(
                        id=f"run-{run.id}",
                        kind="run",
                        session_id=session_id,
                        target_id=run.id,
                    )
                )
            run = run.model_copy(update={"input_revision": session.input_revision + 1})
            await tx.runs.save(run)
            message = Message(
                session_id=session_id,
                run_id=run.id,
                role="user",
                content=text,
                client_id=client_id,
                document_ids=document_ids,
                input_revision=run.input_revision,
                effort=effort,
            )
            await tx.sessions.append_message(message)
            session = session.model_copy(
                update={
                    "title": text[:50] if session.title == "新研究" else session.title,
                    "updated_at": now(),
                    "input_revision": run.input_revision,
                    "effort": effort or session.effort,
                }
            )
            await tx.sessions.save(session)
            await tx.sessions.append_event(
                session_id,
                run.id,
                "session.updated",
                {"session": session.model_dump(mode="json")},
            )
            await tx.sessions.append_event(
                session_id,
                run.id,
                "message.completed",
                {"message": message.model_dump(mode="json")},
            )
            await tx.sessions.append_event(
                session_id, run.id, "run.updated", {"run": run.model_dump(mode="json")}
            )
            return message

    async def cancel(self, run_id: str) -> Run:
        async with self.uow() as tx:
            run = await tx.runs.get(run_id)
            if run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                return run
            run = await transition_run(
                tx, run.id, RunStatus.CANCELLED, reason="用户取消"
            )
            for child in await tx.runs.list(run.session_id):
                if child.parent_id == run.id and child.status not in {
                    RunStatus.COMPLETED,
                    RunStatus.CANCELLED,
                }:
                    await transition_run(
                        tx,
                        child.id,
                        RunStatus.CANCELLED,
                        reason="主任务已取消",
                    )
            for job in await tx.jobs.list():
                if job.target_id == run.id:
                    await tx.jobs.save(job.model_copy(update={"status": "cancelled"}))
        self.cancel_execution(run.id)
        return run

    async def resume(self, run_id: str, *, extend_budget: bool = False) -> Run:
        async with self.uow() as tx:
            run = await tx.runs.get(run_id)
            if run.parent_id or run.status not in {
                RunStatus.FAILED,
                RunStatus.WAITING,
                RunStatus.INTERRUPTED,
            }:
                raise Conflict("只有等待、失败或中断的主任务可以接续")
            session = await tx.sessions.get(run.session_id)
            if run.brief and run.brief.basis_revision != session.brief.basis_revision:
                raise Conflict("研究依据已改变，请接续当前研究任务")
            run = run.model_copy(update={"input_revision": session.input_revision})
            await tx.runs.save(run)
            if run.waiting_document_ids:
                raise Conflict("正在等待文档任务，完成后会自动接续")
            if any(
                item.status == RunStatus.RUNNING
                and item.id != run.id
                and not item.parent_id
                for item in await tx.runs.list(run.session_id)
            ):
                raise Conflict("此会话已有执行中的任务")
            if any(
                item.state not in {"completed", "yielded"}
                and not item.replay_safe
                and item.input_revision == run.input_revision
                for item in await tx.runs.tools(run.id)
            ):
                raise Conflict("仍有结果不确定的工具调用，需要先确认结果")
            run = await transition_run(
                tx,
                run.id,
                RunStatus.QUEUED,
                updates={
                    "budget_extensions": run.budget_extensions + int(extend_budget),
                    "token_soft_target": run.token_soft_target
                    + (run.execution.limits.soft_token_target if extend_budget else 0),
                    "deadline_at": now()
                    + timedelta(seconds=run.execution.limits.deadline_seconds)
                    if extend_budget
                    else run.deadline_at,
                },
            )
            board = await tx.research.get(run.session_id)
            board = board.model_copy(update={"result": None})
            await tx.research.save(board)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "research.updated",
                {"research": board.model_dump(mode="json")},
            )
            await tx.sessions.append_message(
                Message(
                    session_id=run.session_id,
                    run_id=run.id,
                    role="system",
                    content="用户请求接续研究。复用已确认判断和来源，继续解决缺口；预算段 "
                    + str(run.budget_extensions + 1),
                )
            )
            await tx.jobs.enqueue(
                Job(
                    id=f"run-{run.id}",
                    kind="run",
                    session_id=run.session_id,
                    target_id=run.id,
                )
            )
        return run

    async def resolve_tool(self, run_id: str, call_id: str, *, result: ToolResult):
        async with self.uow() as tx:
            run = await tx.runs.get(run_id)
            if run.status in {RunStatus.RUNNING, RunStatus.QUEUED}:
                raise Conflict("执行中不能修改工具结果")
            tool = await tx.runs.tool(run_id, call_id)
            if tool is None or tool.state in {"completed", "yielded"}:
                raise Conflict("没有需要确认的工具调用")
            await tx.runs.save_tool(
                tool.model_copy(
                    update={
                        "state": "completed",
                        "result": result,
                        "finished_at": now(),
                    }
                )
            )
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "tool.resolved",
                {
                    "execution": (await tx.runs.tool(run_id, call_id)).model_dump(
                        mode="json"
                    )
                },
            )
