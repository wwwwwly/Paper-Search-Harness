from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import timedelta

from paper_search.domain import (
    DeadlineExceeded,
    Job,
    Message,
    ModelRequest,
    ModelUnavailable,
    Run,
    RunStatus,
    ToolCall,
    ToolResult,
    now,
)
from paper_search.harness.conversation import ContextBuilder, ContextChanged
from paper_search.harness.coordinator import ResearchCoordinator
from paper_search.harness.limits import RunMeter
from paper_search.harness.token_estimation import exactish_tokens
from paper_search.harness.turn import TurnMixin
from paper_search.harness.usage import restore_usage
from paper_search.ports import AgentCallbacks, ArtifactStore, ModelClient, UowFactory
from paper_search.research import ResearchService, valid_result
from paper_search.run_lifecycle import transition_run
from paper_search.tools.executor import ToolExecutor, UncertainToolOutcome
from paper_search.tools.functions import ToolRegistry

logger = logging.getLogger(__name__)


class AgentRunner(TurnMixin):
    def __init__(
        self,
        *,
        uow: UowFactory,
        artifacts: ArtifactStore,
        model_for: Callable[[str], ModelClient],
        tools_for,
        context: ContextBuilder,
        stop_documents=None,
    ):
        self.uow, self.artifacts, self.model_for, self.tools_for, self.context = (
            uow,
            artifacts,
            model_for,
            tools_for,
            context,
        )
        self.stop_documents = stop_documents
        self.executor = ToolExecutor(uow, artifacts)
        self.research = ResearchService(uow)

    async def emit(self, run, kind, data):
        async with self.uow() as tx:
            await tx.sessions.append_event(run.session_id, run.id, kind, data)

    async def _state(self, run, status, budget, reason=None):
        async with self.uow() as tx:
            current = await tx.runs.get(run.id)
            if not run.parent_id and current.deadline_at != budget.deadline_at:
                return current  # A newer user turn owns this Run's execution state.
            return await transition_run(
                tx,
                run.id,
                status,
                reason=reason,
                updates={
                    "usage": budget.own_usage(run.id)
                    if run.parent_id
                    else budget.usage,
                    "own_usage": budget.own_usage(run.id),
                    **(
                        {"waiting_document_ids": (), "waiting_since": None}
                        if status == RunStatus.INTERRUPTED
                        else {}
                    ),
                },
                cancellation_wins=True,
            )

    async def execute(self, run_id: str, *, budget: RunMeter | None = None) -> str:
        async with self.uow() as tx:
            run = await tx.runs.get(run_id)
            if run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                return ""
            if budget is not None and run.deadline_at != budget.deadline_at:
                run = run.model_copy(update={"deadline_at": budget.deadline_at})
                await tx.runs.save(run)
            if run.deadline_at is None:
                run = run.model_copy(
                    update={
                        "deadline_at": now()
                        + timedelta(seconds=run.execution.limits.deadline_seconds),
                        "token_soft_target": run.execution.limits.soft_token_target,
                    }
                )
                await tx.runs.save(run)
            if budget is None:
                related = [
                    item
                    for item in await tx.runs.list(run.session_id)
                    if item.id == run.id or item.parent_id == run.id
                ]
                initial, owners = await restore_usage(tx, run, related)
                budget = RunMeter(
                    run.execution.limits.model_copy(
                        update={"soft_token_target": run.token_soft_target}
                    ),
                    initial,
                    deadline_at=run.deadline_at,
                    owners=owners,
                )
        coordinator = None if run.parent_id else ResearchCoordinator(self, run, budget)

        async def forbidden(*args):
            raise ValueError("Worker cannot delegate or manage other tasks")

        async def complete(prompt, output_type, *, purpose="structured"):
            return await self.complete_structured(
                run, client, profile, budget, prompt, output_type, purpose=purpose
            )

        callbacks = AgentCallbacks(
            complete,
            coordinator.spawn if coordinator else forbidden,
            coordinator.collect if coordinator else forbidden,
            coordinator.send if coordinator else forbidden,
            coordinator.cancel if coordinator else forbidden,
        )
        try:
            client, profile = self.model_for(run.execution.profile), run.execution.model
            await self._state(run, RunStatus.RUNNING, budget)
            await self.emit(
                run,
                "run.started",
                {
                    "model_profile": run.execution.profile,
                    "effort": run.execution.effort,
                    "model": profile.model,
                    "limits": run.execution.limits.model_dump(mode="json"),
                },
            )
            if coordinator:
                await coordinator.recover()
            reminded = False
            pressure_state = None
            async with asyncio.timeout(budget.remaining_seconds):
                while True:
                    budget.check_time()
                    async with self.uow() as tx:
                        current = await tx.runs.get(run.id)
                        session = await tx.sessions.get(run.session_id)
                        board = await tx.research.get(run.session_id)
                    if current.status == RunStatus.CANCELLED:
                        raise asyncio.CancelledError
                    if not run.parent_id and current.deadline_at != budget.deadline_at:
                        return ""
                    if (
                        not run.parent_id
                        and current.input_revision != session.input_revision
                    ):
                        await self._state(
                            run,
                            RunStatus.INTERRUPTED,
                            budget,
                            "后续输入属于另一根任务，旧版本停止探索",
                        )
                        return ""
                    if run.parent_id:
                        task = next(item for item in board.tasks if item.id == run.id)
                        if (
                            task.status in {"cancelled", "stale"}
                            or task.basis_revision != session.brief.basis_revision
                        ):
                            raise asyncio.CancelledError
                        if session.brief.input_revision != session.input_revision:
                            await asyncio.sleep(0.2)
                            continue
                        current = current.model_copy(
                            update={
                                "input_revision": session.input_revision,
                                "task_revision": task.task_revision,
                                "brief": session.brief.model_copy(
                                    update={
                                        "objective": task.assignment.goal,
                                        "open_questions": (task.assignment.goal,),
                                    }
                                ),
                            }
                        )
                        async with self.uow() as tx:
                            await tx.runs.save(current)
                    run = current.model_copy(
                        update={
                            "brief": current.brief
                            if current.parent_id
                            else session.brief
                        }
                    )
                    if coordinator:
                        coordinator.root = run
                    registry = self.tools_for(run, callbacks)
                    history = await self.context.history(run)
                    completed = {
                        (item.run_id, item.tool_call_id)
                        for item in history
                        if item.role == "tool"
                    }
                    pending = [
                        item
                        for item in history
                        if item.run_id == run.id
                        and any(
                            (run.id, call.id) not in completed
                            for call in item.tool_calls
                        )
                    ]
                    if pending:
                        for item in pending:
                            origin = run.model_copy(
                                update={
                                    "input_revision": run.input_revision
                                    if run.parent_id
                                    else item.input_revision,
                                    "task_revision": item.task_revision
                                    if run.parent_id
                                    else run.task_revision,
                                }
                            )
                            await self._tools(
                                origin,
                                [
                                    call
                                    for call in item.tool_calls
                                    if (run.id, call.id) not in completed
                                ],
                                registry,
                                budget,
                            )
                        await self._wait_documents(run, budget)
                        continue
                    delivered = (
                        await self._finish_result(run, budget)
                        if not run.parent_id
                        else None
                    )
                    if delivered is not None:
                        return delivered
                    final = budget.research_seconds <= 0 or budget.finalizing
                    if final and run.parent_id:
                        raise DeadlineExceeded("研究期限已到")
                    pressure = (final, budget.soft_target_exceeded)
                    if pressure != pressure_state and any(pressure):
                        await self.emit(
                            run,
                            "budget.pressure",
                            {
                                "phase": "delivery" if final else "research",
                                "seconds_remaining": int(budget.remaining_seconds),
                                "tokens_used": budget.usage.input_tokens
                                + budget.usage.output_tokens,
                                "inflight_estimate": budget.reserved,
                                "soft_token_target": budget.limits.soft_token_target,
                            },
                        )
                    pressure_state = pressure

                    async def summarize(text, current_run=run):
                        return await self.complete_compaction(
                            current_run,
                            client,
                            profile,
                            budget,
                            self.context.summary_prompt + "\n" + text,
                        )

                    definitions = registry.definitions()
                    if final:
                        definitions = tuple(
                            item
                            for item in definitions
                            if item["function"]["name"]
                            in {
                                "update_task",
                                "submit_research",
                                "record_findings",
                                "get_paper",
                                "read_document",
                                "read_artifact",
                                "create_report",
                            }
                        )
                    try:
                        messages = await self.context.build(
                            run,
                            context_window=profile.context_window,
                            output_tokens=profile.context_output_headroom,
                            tool_schema_tokens=exactish_tokens(json.dumps(definitions)),
                            summarize=summarize,
                            budget_state={
                                "phase": "delivery" if final else "research",
                                "seconds_remaining": int(budget.remaining_seconds),
                                "research_seconds": int(budget.research_seconds),
                                "target_seconds_remaining": max(
                                    0,
                                    int(
                                        budget.remaining_seconds
                                        - budget.limits.deadline_seconds
                                        + budget.limits.target_seconds
                                    ),
                                ),
                                "token_soft_target": budget.limits.soft_token_target,
                                "tokens_used": budget.usage.input_tokens
                                + budget.usage.output_tokens,
                                "inflight_estimate": budget.reserved,
                                "soft_target_exceeded": budget.soft_target_exceeded,
                            },
                        )
                    except ContextChanged:
                        continue
                    async with self.uow() as tx:
                        refreshed = await tx.runs.get(run.id)
                        latest_input = (
                            await tx.sessions.get(run.session_id)
                        ).input_revision
                    if not run.parent_id and (
                        refreshed.input_revision != run.input_revision
                        or latest_input != run.input_revision
                    ):
                        continue
                    try:
                        response = await self._respond(
                            run,
                            client,
                            ModelRequest(
                                messages=messages,
                                tools=definitions,
                                max_output_tokens=None,
                                options=profile,
                            ),
                            budget,
                            publish=False,
                            final=final,
                        )
                    except ContextChanged:
                        continue
                    async with self.uow() as tx:
                        latest = await tx.sessions.get(run.session_id)
                        if (
                            not run.parent_id
                            and latest.input_revision != run.input_revision
                        ):
                            continue
                        if run.parent_id:
                            latest_board = await tx.research.get(run.session_id)
                            latest_task = next(
                                item for item in latest_board.tasks if item.id == run.id
                            )
                            if (
                                latest_task.task_revision != run.task_revision
                                or latest_task.basis_revision
                                != latest.brief.basis_revision
                                or latest.brief.input_revision != latest.input_revision
                                or latest_task.status in {"cancelled", "stale"}
                            ):
                                continue
                        if response.tool_calls or run.parent_id:
                            await tx.sessions.append_message(
                                Message(
                                    id=response.message_id,
                                    session_id=run.session_id,
                                    run_id=run.id,
                                    role="assistant",
                                    content=response.content
                                    if run.parent_id or not response.tool_calls
                                    else "",
                                    tool_calls=response.tool_calls,
                                    usage=response.usage,
                                    provider_response_id=response.provider_response_id,
                                    created_at=response.started_at,
                                    input_revision=run.input_revision,
                                    task_revision=run.task_revision,
                                )
                            )
                    if response.tool_calls:
                        await self._tools(
                            run, list(response.tool_calls), registry, budget
                        )
                        await self._wait_documents(run, budget)
                        continue
                    if not run.parent_id and (
                        run.brief.open_questions
                        or run.brief.input_revision != run.input_revision
                    ):
                        if not reminded and budget.remaining_seconds > 3:
                            reminded = True
                            async with self.uow() as tx:
                                await tx.sessions.append_message(
                                    Message(
                                        session_id=run.session_id,
                                        run_id=run.id,
                                        role="system",
                                        content="先同步最新 TaskBrief。正式研究请 record_findings 确认判断，再 submit_research；部分结果与缺口可以提交。",
                                    )
                                )
                            continue
                        return await self._budget_summary(
                            run, "尚未提交正式结果", budget
                        )
                    if not run.parent_id:
                        if not await self._publish_chat(run, response, budget):
                            continue
                    else:
                        if not await self._finish_worker(run, budget):
                            continue
                    return response.content
        except asyncio.CancelledError:
            await self._state(
                run, RunStatus.INTERRUPTED, budget, "执行中断，可从持久步骤续接"
            )
            raise
        except (DeadlineExceeded, TimeoutError, ModelUnavailable) as exc:
            if not run.parent_id:
                return await self._budget_summary(
                    run, str(exc) or "时间预算已用尽", budget
                )
            await self._state(run, RunStatus.WAITING, budget, str(exc))
            return ""
        except UncertainToolOutcome as exc:
            await self._state(run, RunStatus.WAITING, budget, str(exc))
            return str(exc)
        except Exception as exc:
            logger.exception("Agent run failed: %s", run.id)
            await self._state(run, RunStatus.FAILED, budget, str(exc)[:300])
            return ""
        finally:
            if coordinator:
                await coordinator.close()
                async with self.uow() as tx:
                    terminal = await tx.runs.get(run.id)
                if self.stop_documents and terminal.status in {
                    RunStatus.COMPLETED,
                    RunStatus.CANCELLED,
                }:
                    await self.stop_documents(run.id)
                await self._continue_pending_input(run)

    async def _continue_pending_input(self, executed):
        """A message accepted during a failed call still gets its own response."""
        async with self.uow() as tx:
            current = await tx.runs.get(executed.id)
            if (
                current.input_revision <= executed.input_revision
                or current.status
                not in {RunStatus.WAITING, RunStatus.FAILED, RunStatus.INTERRUPTED}
            ):
                return
            received_at = now()
            renew = (
                current.deadline_at is not None
                and current.deadline_at
                <= received_at
                + timedelta(
                    seconds=current.execution.limits.finalization_reserve_seconds
                )
            )
            current = await transition_run(
                tx,
                current.id,
                RunStatus.QUEUED,
                reason="pending_user_input",
                updates={
                    "deadline_at": received_at
                    + timedelta(seconds=current.execution.limits.deadline_seconds)
                    if renew
                    else current.deadline_at,
                    "budget_extensions": current.budget_extensions + int(renew),
                    "token_soft_target": current.token_soft_target
                    + (current.execution.limits.soft_token_target if renew else 0),
                    "waiting_document_ids": (),
                    "waiting_since": None,
                },
            )
            await tx.jobs.enqueue(
                Job(
                    id=f"run-{current.id}",
                    kind="run",
                    session_id=current.session_id,
                    target_id=current.id,
                )
            )

    async def _finish_worker(self, run, budget):
        async with self.uow() as tx:
            session = await tx.sessions.get(run.session_id)
            board = await tx.research.get(run.session_id)
            task = next(item for item in board.tasks if item.id == run.id)
            if (
                task.task_revision != run.task_revision
                or task.basis_revision != session.brief.basis_revision
                or session.brief.input_revision != session.input_revision
                or task.status in {"cancelled", "stale"}
            ):
                return False
            await transition_run(
                tx,
                run.id,
                RunStatus.COMPLETED,
                reason="worker_completed",
                updates={
                    "usage": budget.own_usage(run.id),
                    "own_usage": budget.own_usage(run.id),
                },
                cancellation_wins=True,
            )
            return True

    async def _finish_result(
        self, run, budget, *, status=RunStatus.COMPLETED, reason="research_submitted"
    ):
        async with self.uow() as tx:
            current = await tx.runs.get(run.id)
            session = await tx.sessions.get(run.session_id)
            board = await tx.research.get(run.session_id)
            if (
                current.status == RunStatus.CANCELLED
                or current.input_revision != session.input_revision
                or not valid_result(board.result, session.brief, current)
            ):
                return None
            message = Message(
                session_id=run.session_id,
                run_id=run.id,
                role="assistant",
                content=board.result.markdown,
                input_revision=current.input_revision,
                context_revision=session.context_state.revision,
            )
            await tx.sessions.append_message(message)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "message.completed",
                {"message": message.model_dump(mode="json")},
            )
            await transition_run(
                tx,
                run.id,
                status,
                reason=reason,
                updates={
                    "usage": budget.usage,
                    "own_usage": budget.own_usage(run.id),
                    "phase": "waiting" if status == RunStatus.WAITING else "finished",
                    "waiting_document_ids": (),
                    "waiting_since": None,
                },
                cancellation_wins=True,
            )
        return message.content

    async def _publish_chat(self, run, response, budget):
        async with self.uow() as tx:
            session = await tx.sessions.get(run.session_id)
            current = await tx.runs.get(run.id)
            if (
                current.status == RunStatus.CANCELLED
                or current.input_revision != run.input_revision
                or current.input_revision != session.input_revision
            ):
                return False
            message = Message(
                session_id=run.session_id,
                run_id=run.id,
                id=response.message_id,
                role="assistant",
                content=response.content,
                input_revision=run.input_revision,
                usage=response.usage,
                provider_response_id=response.provider_response_id,
            )
            await tx.sessions.append_message(message)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "message.completed",
                {"message": message.model_dump(mode="json")},
            )
            await transition_run(
                tx,
                run.id,
                RunStatus.COMPLETED,
                updates={"usage": budget.usage, "own_usage": budget.own_usage(run.id)},
                cancellation_wins=True,
            )
            return True

    async def _budget_summary(self, run, reason, budget):
        async with self.uow() as tx:
            current = await tx.runs.get(run.id)
            if current.deadline_at != budget.deadline_at:
                return ""
        try:
            await self.research.checkpoint_result(run, reason)
            content = await self._finish_result(
                run, budget, status=RunStatus.WAITING, reason=reason
            )
        except (ValueError, LookupError):
            content = None
        if content is None:
            await self._state(run, RunStatus.WAITING, budget, reason)
        return content or ""

    async def _wait_documents(self, run, budget):
        while True:
            async with self.uow() as tx:
                current = await tx.runs.get(run.id)
                if current.status == RunStatus.CANCELLED:
                    raise asyncio.CancelledError
                session = await tx.sessions.get(run.session_id)
                if session.input_revision != run.input_revision:
                    return
                documents = [
                    await tx.documents.get(key) for key in current.waiting_document_ids
                ]
                if all(
                    item.status in {"ready", "failed", "cancelled"}
                    for item in documents
                ):
                    await tx.runs.save(
                        current.model_copy(
                            update={"waiting_document_ids": (), "waiting_since": None}
                        )
                    )
                    return
            if budget.research_seconds <= 0:
                raise DeadlineExceeded("等待资料已到研究期限")
            await asyncio.sleep(0.25)

    async def _tools(
        self, run: Run, calls: list[ToolCall], registry: ToolRegistry, budget: RunMeter
    ):
        def planning_first(call: ToolCall) -> int:
            try:
                return 0 if registry.get(call.name).capability == "planning" else 1
            except ValueError:
                return 1

        calls = sorted(calls, key=planning_first)

        async def invoke(call: ToolCall):
            try:
                local_read = (
                    registry.get(call.name).capability == "read"
                    and not registry.get(call.name).external_schema
                )
            except ValueError:
                local_read = False
            delivery = local_read or call.name in {
                "update_task",
                "submit_research",
                "record_findings",
                "create_report",
                "load_skill",
                "read_skill_reference",
                "collect_tasks",
            }
            result = await self.executor.execute(
                run,
                call,
                registry,
                lambda: (
                    budget.delivery_tool(run.id) if delivery else budget.tool(run.id)
                ),
                timeout_seconds=(
                    budget.remaining_seconds if delivery else budget.research_seconds
                ),
            )
            if result.error_code == "deadline_reached":
                budget.finalization_reason = result.content
            return call, result

        async def settle(results: list[tuple[ToolCall, ToolResult]]):
            async with self.uow() as tx:
                for call, result in results:
                    message = Message(
                        id=f"tool-{run.id}-{call.id}",
                        session_id=run.session_id,
                        run_id=run.id,
                        role="tool",
                        content=result.content,
                        tool_call_id=call.id,
                        tool_result=result,
                        input_revision=run.input_revision,
                        task_revision=run.task_revision,
                    )
                    await tx.sessions.append_message(message)
                current = await tx.runs.get(run.id)
                await tx.runs.save(
                    current.model_copy(
                        update={
                            "usage": budget.own_usage(run.id)
                            if run.parent_id
                            else budget.usage,
                            "own_usage": budget.own_usage(run.id),
                        }
                    )
                )
            for call, _ in results:
                await self.executor.mark_yielded(run, call)

        async def parallel(calls):
            results = await asyncio.gather(
                *(invoke(item) for item in calls), return_exceptions=True
            )
            await settle(
                [item for item in results if not isinstance(item, BaseException)]
            )
            for item in results:
                if isinstance(item, BaseException):
                    raise item

        batch = []
        for call in calls:
            try:
                safe = registry.get(call.name).parallel_safe
            except ValueError:
                safe = False
            if not safe and batch:
                await parallel(batch)
                batch = []
            if safe:
                batch.append(call)
                if len(batch) == 4:
                    await parallel(batch)
                    batch = []
            else:
                await settle([await invoke(call)])
        if batch:
            await parallel(batch)
