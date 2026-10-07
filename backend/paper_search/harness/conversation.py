from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from paper_search.domain import (
    ContextCompressionState,
    Message,
    ModelMessage,
    ModelUnavailable,
    Run,
    RunStatus,
    ToolResult,
    WorkingSummary,
)
from paper_search.harness.context_compression import (
    COLLAPSE_BLOCK_RATIO,
    COLLAPSE_COMMIT_RATIO,
    COLLAPSE_TARGET_RATIO,
    HISTORY_SNIP_RATIO,
    MAX_AUTOCOMPACT_FAILURES,
    autocompact_threshold,
    collapse_batches,
    effective_window,
    project_messages,
    snip_repeated_history,
    time_microcompact_selection,
    tool_key,
)
from paper_search.harness.token_estimation import estimate_messages, exactish_tokens
from paper_search.intent import read_intent
from paper_search.ports import ArtifactStore, UowFactory


class ContextChanged(RuntimeError):
    """Input changed during compaction; rebuild before making a model request."""


class ContextBuilder:
    def __init__(
        self,
        uow: UowFactory,
        root_prompt: str,
        artifacts: ArtifactStore | None = None,
        *,
        worker_prompt: str | None = None,
        skills=None,
        memory=None,
        summary_prompt: str = "",
        strategy: str = "collapse",
        idle_ttl_seconds: int = 3600,
        clock: Callable[[], datetime] | None = None,
    ):
        self.uow, self.root_prompt = uow, root_prompt
        self.worker_prompt = worker_prompt or root_prompt
        self.skills, self.memory = skills, memory
        self.summary_prompt = summary_prompt
        self.artifacts = artifacts
        if strategy not in {"collapse", "autocompact"}:
            raise ValueError("unknown context compression strategy")
        self.strategy = strategy
        self.idle_ttl_seconds = idle_ttl_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    async def history(self, run: Run) -> list[Message]:
        async with self.uow() as tx:
            messages = await tx.sessions.messages(run.session_id, limit=10000)
            runs = {item.id: item for item in await tx.runs.list(run.session_id)}
        messages.sort(key=lambda item: item.created_at)
        if run.parent_id:
            return [item for item in messages if item.run_id == run.id]
        visible = [
            item
            for item in messages
            if item.run_id in runs
            and runs[item.run_id].parent_id is None
            and (
                item.run_id == run.id
                or runs[item.run_id].status
                not in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING}
            )
        ]
        settled = {
            (item.run_id, item.tool_call_id) for item in visible if item.role == "tool"
        }
        # Earlier aborted runs are historical facts, never work to replay in this run.
        async with self.uow() as tx:
            for item in list(visible):
                if item.run_id == run.id:
                    continue
                for call in item.tool_calls:
                    if (item.run_id, call.id) in settled:
                        continue
                    recorded = await tx.runs.tool(item.run_id, call.id)
                    result = (
                        recorded.result
                        if recorded and recorded.state == "completed"
                        else ToolResult(
                            content="此历史调用未取得确定结果，不得假设已成功执行。",
                            is_error=True,
                            error_code="interrupted",
                        )
                    )
                    visible.append(
                        Message(
                            id=f"context-{item.run_id}-{call.id}",
                            session_id=run.session_id,
                            run_id=item.run_id,
                            role="tool",
                            tool_call_id=call.id,
                            tool_result=result,
                            content=result.content,
                            created_at=item.created_at,
                        )
                    )
        return sorted(visible, key=lambda item: item.created_at)

    async def build(
        self,
        run: Run,
        *,
        context_window: int,
        output_tokens: int,
        tool_schema_tokens: int,
        summarize: Callable[[str], Awaitable[WorkingSummary]],
        budget_state: dict | None = None,
        force_compact: bool = False,
    ) -> tuple[ModelMessage, ...]:
        system_prompt = self.worker_prompt
        if not run.parent_id:
            system_prompt = self.root_prompt
        messages = await self.history(run)
        async with self.uow() as tx:
            session = await tx.sessions.get(run.session_id)
            memories = await tx.memories.list(run.session_id, include_deleted=True)
            documents = await tx.documents.list(run.session_id)
            evidence = await tx.papers.list_evidence(run.session_id)
            research = await tx.research.get(run.session_id)
        memory_revision = sum(item.revision for item in memories)
        if self.skills and run.brief and run.brief.objective:
            name = {
                "topic_search": "topic-search",
                "related_work_delta": "related-work-delta",
                "baseline_selection": "baseline-selection",
            }[run.brief.research_mode]
            if run.parent_id:
                task = next(
                    (item for item in research.tasks if item.id == run.id), None
                )
                if task and task.assignment.kind == "evidence":
                    name = "paper-evidence"
            if not any(item.name == name for item in run.skills):
                await self.skills.load(run, name)
        async with self.uow() as tx:
            current_run = await tx.runs.get(run.id)
            intent = await read_intent(tx, run)
            states = {
                item.id: ref
                for item in messages
                if (ref := await tx.runs.model_state(item.id))
            }
            tool_executions = {
                tool_key(item.run_id, item.tool_call_id): execution
                for item in messages
                if item.role == "tool"
                and item.tool_call_id
                and (execution := await tx.runs.tool(item.run_id, item.tool_call_id))
            }
        pinned = {
            # Raw updates remain in the transcript/working summary. Copying all of
            # them into an uncompressible system block defeats long-session compaction.
            **{key: value for key, value in intent.items() if key != "user_updates"},
            "session_title": session.title,
            "user_updates_in_history": len(intent["user_updates"]),
            "current_input_revision": current_run.input_revision,
            "delivery_spec": session.brief.delivery.model_dump(mode="json"),
            "documents": [
                {
                    "id": item.id,
                    "name": item.name,
                    "paper_id": item.paper_id,
                    "status": item.status,
                    "error": item.error,
                }
                for item in documents
            ],
            "document_identity_rule": "documents.paper_id 是附件已绑定的论文记录，可用于 get_paper 和 submit_research；document.id 只用于全文工具。已有绑定不要求编号再次印在 PDF 正文中。绑定不支持额外版本历史、方法或实验断言。",
            "current_preferences": [
                item.model_dump(mode="json")
                for item in memories
                if item.kind == "preference" and not item.deleted
            ],
            "deleted_memory_keys": [item.key for item in memories if item.deleted],
            "research_state": {
                "questions": [
                    item.model_dump(mode="json") for item in research.questions
                ],
                "tasks": [
                    item.model_dump(mode="json")
                    for item in research.tasks
                    if item.root_run_id == (run.parent_id or run.id)
                ],
                "assessments": [
                    item.model_dump(mode="json")
                    for item in research.assessments
                    if item.basis_revision == session.brief.basis_revision
                ],
                "findings": [
                    item.model_dump(mode="json")
                    for item in research.findings[-12:]
                    if item.basis_revision == session.brief.basis_revision
                ],
                "progress": [
                    item.model_dump(mode="json")
                    for item in research.progress
                    if item.basis_revision == session.brief.basis_revision
                ],
            },
            "skill_catalog": self.skills.catalog() if self.skills else [],
        }
        if run.parent_id:
            task = next((item for item in research.tasks if item.id == run.id), None)
            if task:
                paper_ids = set(task.assignment.paper_ids)
                pinned["documents"] = [
                    item
                    for item in pinned["documents"]
                    if item["paper_id"] in paper_ids
                ]
                pinned["research_state"] = {
                    "task": task.model_dump(mode="json"),
                    "questions": [
                        item.model_dump(mode="json")
                        for item in research.questions
                        if item.id in task.assignment.question_ids
                    ],
                    "findings": [
                        item.model_dump(mode="json")
                        for item in research.findings[-12:]
                        if item.paper_id in task.assignment.paper_ids
                        or item.run_id in task.assignment.depends_on
                    ],
                }
        if self.memory:
            pinned["recalled_memories"] = await self.memory.recall(
                run.brief or session.brief,
                run.session_id,
                run_id=run.id,
                use_embedding=(budget_state or {}).get("phase") != "delivery",
                timeout_seconds=(budget_state or {}).get("research_seconds")
                if (budget_state or {}).get("phase") != "delivery"
                else None,
            )
        if self.artifacts:
            pinned["active_skills"] = [
                {
                    "name": item.name,
                    "version": item.version,
                    "content": (await self.artifacts.read(item.file_id)).decode(),
                    "references": {
                        key: (await self.artifacts.read(item.references[key])).decode()
                        for key in item.loaded_references
                    },
                }
                for item in current_run.skills
            ]
        system = "当前任务与用户确认的记忆：\n" + json.dumps(pinned, ensure_ascii=False)
        runtime_state = ModelMessage(
            role="system",
            content="本轮易变运行状态：\n"
            + json.dumps(budget_state or {}, ensure_ascii=False),
        )
        post_compact_restore = {
            "task": pinned.get("task"),
            "constraint_updates": intent["user_updates"],
            "deleted_memory_keys": pinned["deleted_memory_keys"],
            "delivery_spec": pinned["delivery_spec"],
            "documents": pinned["documents"][-5:],
            "research_state": pinned["research_state"],
            "active_skills": pinned.get("active_skills", []),
            "recent_evidence": [
                {
                    "id": item.id,
                    "paper_id": item.paper_id,
                    "page": item.page,
                    "excerpt": item.excerpt[:1000],
                }
                for item in evidence[-5:]
            ],
            "capabilities": "current tool schema, root/worker prompt and flat Worker policy are reattached by the caller",
        }
        summary = current_run.summary if run.parent_id else session.summary
        state = current_run.context_state if run.parent_id else session.context_state
        tail = messages
        if summary.through_message_id:
            cutoff = next(
                (
                    i
                    for i, item in enumerate(messages)
                    if item.id == summary.through_message_id
                ),
                -1,
            )
            if cutoff >= 0:
                tail = messages[cutoff + 1 :]
            else:
                summary = WorkingSummary()
        window = effective_window(context_window, output_tokens)
        cleared_tools = set(state.cleared_tool_keys)
        collapsed_tools = set(state.collapsed_tool_keys)
        cleared_continuations = set(state.cleared_continuation_message_ids)

        def project(source: list[Message]) -> list[ModelMessage]:
            return project_messages(
                source,
                states,
                active_run_id=run.id,
                cleared_tool_keys=cleared_tools,
                collapsed_tool_keys=collapsed_tools,
                cleared_continuation_message_ids=cleared_continuations,
                context_revision=state.revision,
            )

        async def count(
            projected_messages: list[ModelMessage], summary_text: str
        ) -> int:
            full = [
                ModelMessage(role="system", content=system_prompt),
                ModelMessage(role="system", content=summary_text),
                *projected_messages,
                ModelMessage(role="system", content=system),
                runtime_state,
            ]
            token_estimate = estimate_messages(
                full, tool_schema_tokens=tool_schema_tokens
            )
            continuation = await self._continuation_tokens(
                full,
                after_index=token_estimate.anchor_index,
            )
            return token_estimate.tokens + continuation

        projected = project(tail)
        estimate = await count(projected, summary.text)
        before_cleanup = estimate
        last_response = next(
            (
                item
                for item in reversed(messages)
                if item.role == "assistant" and item.usage is not None
            ),
            None,
        )
        idle_expired = bool(
            not run.parent_id
            and last_response
            and (self.clock() - last_response.created_at).total_seconds()
            >= self.idle_ttl_seconds
        )

        snipped = snip_repeated_history(tail)
        should_snip = (
            state.history_snipped
            or idle_expired
            or estimate >= int(window * HISTORY_SNIP_RATIO)
        )
        snipped_ids = tuple(
            item.id
            for before, item in zip(tail, snipped.messages, strict=True)
            if before.content != item.content
        )
        state_changed = False
        if should_snip and snipped.changed_messages:
            tail = snipped.messages
            if snipped_ids != state.snipped_message_ids or not state.history_snipped:
                state = state.model_copy(
                    update={
                        "history_snipped": True,
                        "snipped_message_ids": snipped_ids,
                    }
                )
                state_changed = True

        if idle_expired:
            replay_safe = {
                key
                for key, execution in tool_executions.items()
                if execution.replay_safe
                and execution.capability not in {"write", "delegation"}
            }
            new_tools, new_continuations = time_microcompact_selection(
                tail,
                replay_safe_tool_keys=replay_safe,
                continuation_message_ids=set(states),
                window=window,
            )
            combined_tools = cleared_tools | new_tools
            combined_continuations = cleared_continuations | new_continuations
            if (
                combined_tools != cleared_tools
                or combined_continuations != cleared_continuations
            ):
                cleared_tools = combined_tools
                cleared_continuations = combined_continuations
                state = state.model_copy(
                    update={
                        "cleared_tool_keys": tuple(sorted(cleared_tools)),
                        "cleared_continuation_message_ids": tuple(
                            sorted(cleared_continuations)
                        ),
                    }
                )
                state_changed = True

        if state_changed:
            state = state.model_copy(update={"revision": state.revision + 1})
            await self._save_context_state(run, state)
            projected = project(tail)
            estimate = await count(projected, summary.text)
            await self._record_compaction(
                run,
                state,
                stage="microcompact" if idle_expired else "history_snip",
                before=before_cleanup,
                after=estimate,
                trigger_reason="idle_ttl" if idle_expired else "context_threshold",
                cleaned_tool_results=len(cleared_tools),
                retained_messages=len(tail),
                summary=summary,
            )

        cleanup_estimate = estimate
        cleaned_tool_results = len(cleared_tools)
        collapse_trigger = int(window * COLLAPSE_COMMIT_RATIO)
        collapse_block = int(window * COLLAPSE_BLOCK_RATIO)
        if self.strategy == "collapse" and estimate >= collapse_trigger:
            target = int(window * COLLAPSE_TARGET_RATIO)
            selected = collapse_batches(
                tail,
                already_collapsed=collapsed_tools,
                tokens_to_reclaim=max(1, estimate - target),
            )
            if selected != collapsed_tools:
                collapsed_tools = selected
                state = state.model_copy(
                    update={
                        "collapsed_tool_keys": tuple(sorted(collapsed_tools)),
                        "revision": state.revision + 1,
                    }
                )
                await self._save_context_state(run, state)
                projected = project(tail)
                cleanup_estimate = await count(projected, summary.text)
                await self._record_compaction(
                    run,
                    state,
                    stage="context_collapse",
                    before=estimate,
                    after=cleanup_estimate,
                    trigger_reason="collapse_commit",
                    cleaned_tool_results=len(collapsed_tools),
                    retained_messages=len(tail),
                    summary=summary,
                )

        summary_required = (
            force_compact
            or (
                self.strategy == "autocompact"
                and cleanup_estimate >= autocompact_threshold(window)
            )
            or (self.strategy == "collapse" and cleanup_estimate >= collapse_block)
        )
        if summary_required and len(tail) > 1:
            compaction_started = time.monotonic()
            if state.consecutive_autocompact_failures >= MAX_AUTOCOMPACT_FAILURES:
                raise ValueError("上下文全量压缩已连续失败三次，停止重复调用")
            # Full compact replaces every completed round before the latest user
            # request. It does not repeatedly nibble at a small old prefix.
            cutoff = len(tail) - 1 if tail[-1].role == "user" else len(tail)
            if tail[-1].role == "tool":
                latest_batch = next(
                    (
                        index
                        for index in range(len(tail) - 1, -1, -1)
                        if tail[index].role == "assistant" and tail[index].tool_calls
                    ),
                    None,
                )
                if latest_batch is not None and all(
                    item.role == "tool" for item in tail[latest_batch + 1 :]
                ):
                    cutoff = latest_batch
                    if latest_batch and tail[latest_batch - 1].role == "user":
                        cutoff -= 1
            if cutoff > 0:
                older = tail[:cutoff]
                previous_summary = summary
                summary_source = older
                compressed = None
                prompt = ""
                last_error: Exception | None = None
                for attempt in range(3):
                    prompt = json.dumps(
                        {
                            "query_source": "compact",
                            "prior_summary": summary.model_dump(mode="json"),
                            "pinned": pinned,
                            "messages": [
                                item.model_dump(
                                    mode="json",
                                    exclude={
                                        "continuation_ref",
                                        "usage",
                                        "provider_response_id",
                                    },
                                )
                                for item in project(summary_source)
                            ],
                            "post_compact_restore": post_compact_restore,
                        },
                        ensure_ascii=False,
                    )
                    try:
                        compressed = await summarize(prompt)
                        break
                    except (RuntimeError, ValueError) as exc:
                        last_error = exc
                        prompt_too_long = isinstance(exc, ModelUnavailable) and (
                            exc.status == 400
                            or exc.code
                            in {"context_length_exceeded", "prompt_too_long"}
                        )
                        if not prompt_too_long or attempt == 2:
                            break
                        next_user = next(
                            (
                                index
                                for index, item in enumerate(summary_source[1:], 1)
                                if item.role == "user"
                            ),
                            len(summary_source),
                        )
                        summary_source = summary_source[next_user:]
                        if not summary_source:
                            break
                if compressed is None:
                    state = state.model_copy(
                        update={
                            "consecutive_autocompact_failures": min(
                                MAX_AUTOCOMPACT_FAILURES,
                                state.consecutive_autocompact_failures + 1,
                            )
                        }
                    )
                    await self._save_context_state(run, state)
                    await self._record_compaction_failure(
                        run,
                        stage="autocompact",
                        trigger_reason=(
                            "collapse_blocked"
                            if self.strategy == "collapse"
                            else "autocompact_threshold"
                        ),
                        failure_reason=f"summary_error:{type(last_error).__name__}",
                        before=estimate,
                        after_cleanup=cleanup_estimate,
                        prompt_tokens=exactish_tokens(prompt),
                        duration_ms=int((time.monotonic() - compaction_started) * 1000),
                        consecutive_failures=state.consecutive_autocompact_failures,
                    )
                    raise last_error
                compaction_failed = False
                async with self.uow() as tx:
                    known = {
                        item.id
                        for item in await tx.papers.list_evidence(run.session_id)
                    }
                    required = tuple(
                        dict.fromkeys(
                            item
                            for item in (
                                *summary.evidence_ids,
                                *re.findall(r"\[evidence:([a-zA-Z0-9_-]+)\]", prompt),
                            )
                            if item in known
                        )
                    )
                    allowed = set(required)
                    text = re.sub(
                        r"\[evidence:([a-zA-Z0-9_-]+)\]",
                        lambda match: (
                            match.group(0) if match.group(1) in allowed else ""
                        ),
                        compressed.text,
                    )
                    compressed = compressed.model_copy(
                        update={
                            "text": text,
                            "evidence_ids": required,
                        }
                    )
                    if not compressed.text.strip():
                        raise ValueError("压缩摘要不能为空，原始上下文保持不变")
                    current = await tx.sessions.get(run.session_id)
                    latest_run = await tx.runs.get(run.id)
                    latest_memories = await tx.memories.list(
                        run.session_id, include_deleted=True
                    )
                    if (
                        latest_run.input_revision != current_run.input_revision
                        or sum(item.revision for item in latest_memories)
                        != memory_revision
                    ):
                        raise ContextChanged("输入或记忆在压缩期间变化")
                    summary = compressed.model_copy(
                        update={
                            "through_message_id": older[-1].id,
                            "memory_revision": memory_revision,
                            "input_revision": current_run.input_revision,
                        }
                    )
                    compacted_state = ContextCompressionState(
                        revision=state.revision + 1,
                        consecutive_autocompact_failures=0,
                    )
                    projected_after = project_messages(
                        tail[cutoff:],
                        states,
                        active_run_id=run.id,
                        context_revision=compacted_state.revision,
                    )
                    estimated_after = await count(projected_after, summary.text)
                    if estimated_after >= cleanup_estimate or estimated_after > int(
                        window * 0.5
                    ):
                        compaction_failed = True
                        summary = previous_summary
                        failed_state = state.model_copy(
                            update={
                                "consecutive_autocompact_failures": min(
                                    MAX_AUTOCOMPACT_FAILURES,
                                    state.consecutive_autocompact_failures + 1,
                                )
                            }
                        )
                        if run.parent_id:
                            await tx.runs.save(
                                latest_run.model_copy(
                                    update={"context_state": failed_state}
                                )
                            )
                        else:
                            await tx.sessions.save(
                                current.model_copy(
                                    update={"context_state": failed_state}
                                )
                            )
                        state = failed_state
                        await tx.sessions.append_event(
                            run.session_id,
                            run.id,
                            "context.compaction_failed",
                            {
                                "scope": "worker" if run.parent_id else "root",
                                "trigger_reason": "context_threshold",
                                "failure_reason": "no_net_reclamation"
                                if estimated_after >= cleanup_estimate
                                else "still_over_capacity",
                                "estimated_tokens_before": estimate,
                                "estimated_tokens_after_cleanup": cleanup_estimate,
                                "summary_input_estimated_tokens": exactish_tokens(
                                    prompt
                                ),
                                "summary_output_estimated_tokens": exactish_tokens(
                                    compressed.text
                                ),
                                "duration_ms": int(
                                    (time.monotonic() - compaction_started) * 1000
                                ),
                                "stage": "autocompact",
                                "consecutive_failures": (
                                    state.consecutive_autocompact_failures
                                ),
                            },
                        )
                    elif run.parent_id:
                        await tx.runs.save(
                            latest_run.model_copy(
                                update={
                                    "summary": summary,
                                    "context_state": compacted_state,
                                }
                            )
                        )
                    elif not run.parent_id:
                        await tx.sessions.save(
                            current.model_copy(
                                update={
                                    "summary": summary,
                                    "context_state": compacted_state,
                                }
                            )
                        )
                    if not compaction_failed:
                        state = compacted_state
                        await tx.sessions.append_event(
                            run.session_id,
                            run.id,
                            "context.compacted",
                            {
                                "method": "summary",
                                "stage": "autocompact",
                                "through_message_id": older[-1].id,
                                "evidence_ids": summary.evidence_ids,
                                "estimated_tokens_before": estimate,
                                "estimated_tokens_after": estimated_after,
                                "summary_tokens": exactish_tokens(summary.text),
                                "summarized_messages": len(older),
                                "retained_messages": len(tail) - cutoff,
                                "scope": "worker" if run.parent_id else "root",
                                "trigger_reason": "context_threshold",
                                "cleaned_tool_results": cleaned_tool_results,
                                "estimated_tokens_after_cleanup": cleanup_estimate,
                                "summary_input_estimated_tokens": exactish_tokens(
                                    prompt
                                ),
                                "summary_output_estimated_tokens": exactish_tokens(
                                    summary.text
                                ),
                                "net_reclaimed_tokens": estimate - estimated_after,
                                "duration_ms": int(
                                    (time.monotonic() - compaction_started) * 1000
                                ),
                            },
                        )
                if not compaction_failed:
                    tail = tail[cutoff:]
                    projected = project_messages(
                        tail,
                        states,
                        active_run_id=run.id,
                        context_revision=state.revision,
                    )
        if summary.text:
            system += (
                "\n工作摘要（不替代原文证据）：\n"
                + summary.text
                + "\n证据 ID："
                + ", ".join(summary.evidence_ids)
                + "\n压缩后恢复状态：\n"
                + json.dumps(post_compact_restore, ensure_ascii=False)
            )
        final_messages = [
            ModelMessage(role="system", content=system_prompt),
            *projected,
            ModelMessage(role="system", content=system),
            runtime_state,
        ]
        final_estimate = estimate_messages(
            final_messages, tool_schema_tokens=tool_schema_tokens
        )
        final_tokens = final_estimate.tokens + await self._continuation_tokens(
            final_messages,
            after_index=final_estimate.anchor_index,
        )
        if final_tokens > window:
            raise ValueError("上下文仍超出模型容量；请缩小本次输入或选择更大上下文模型")
        return tuple(final_messages)

    async def _continuation_tokens(self, messages, *, after_index: int | None = None):
        total = 0
        start = 0 if after_index is None else after_index + 1
        for message in messages[start:]:
            if message.continuation_ref:
                if self.artifacts is None:
                    raise ValueError("缺少模型续接状态存储")
                total += exactish_tokens(
                    (await self.artifacts.read(message.continuation_ref)).decode()
                )
        return total

    async def _save_context_state(
        self, run: Run, state: ContextCompressionState
    ) -> None:
        async with self.uow() as tx:
            if run.parent_id:
                current = await tx.runs.get(run.id)
                await tx.runs.save(current.model_copy(update={"context_state": state}))
            else:
                session = await tx.sessions.get(run.session_id)
                await tx.sessions.save(
                    session.model_copy(update={"context_state": state})
                )

    async def _record_compaction(
        self,
        run: Run,
        state: ContextCompressionState,
        *,
        stage: str,
        before: int,
        after: int,
        trigger_reason: str,
        cleaned_tool_results: int,
        retained_messages: int,
        summary: WorkingSummary,
    ) -> None:
        if after >= before:
            return
        async with self.uow() as tx:
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "context.compacted",
                {
                    "method": "cleanup",
                    "stage": stage,
                    "evidence_ids": summary.evidence_ids,
                    "estimated_tokens_before": before,
                    "estimated_tokens_after": after,
                    "summary_tokens": exactish_tokens(summary.text)
                    if summary.text
                    else 0,
                    "summarized_messages": 0,
                    "retained_messages": retained_messages,
                    "scope": "worker" if run.parent_id else "root",
                    "trigger_reason": trigger_reason,
                    "cleaned_tool_results": cleaned_tool_results,
                    "estimated_tokens_after_cleanup": after,
                    "net_reclaimed_tokens": before - after,
                    "context_revision": state.revision,
                },
            )

    async def _record_compaction_failure(
        self,
        run: Run,
        *,
        stage: str,
        trigger_reason: str,
        failure_reason: str,
        before: int,
        after_cleanup: int,
        prompt_tokens: int,
        duration_ms: int,
        consecutive_failures: int,
    ) -> None:
        async with self.uow() as tx:
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "context.compaction_failed",
                {
                    "scope": "worker" if run.parent_id else "root",
                    "stage": stage,
                    "trigger_reason": trigger_reason,
                    "failure_reason": failure_reason,
                    "estimated_tokens_before": before,
                    "estimated_tokens_after_cleanup": after_cleanup,
                    "summary_input_estimated_tokens": prompt_tokens,
                    "summary_output_estimated_tokens": 0,
                    "duration_ms": duration_ms,
                    "consecutive_failures": consecutive_failures,
                },
            )
