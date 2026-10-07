from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from jsonschema import ValidationError as JsonValidationError
from pydantic import ValidationError

from paper_search.diagnostics import current_tool_call, execution_error
from paper_search.domain import (
    Artifact,
    Conflict,
    DeadlineExceeded,
    Evidence,
    ExecutionError,
    MissingRecord,
    ModelUnavailable,
    Run,
    RunStatus,
    SourceSnapshot,
    ToolCall,
    ToolExecution,
    ToolInputError,
    ToolResult,
    now,
)
from paper_search.harness.token_estimation import exactish_tokens
from paper_search.intent import require_research
from paper_search.ports import ArtifactStore, UowFactory
from paper_search.retrieval.catalog import attach_candidate, save_paper
from paper_search.retrieval.identity import normalize_url
from paper_search.retrieval.web import paper_from_card
from paper_search.tools.functions import ToolRegistry, external_validator

logger = logging.getLogger(__name__)


def _utf8_preview(text: str, max_bytes: int = 2000) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class UncertainToolOutcome(RuntimeError):
    pass


class ToolExecutor:
    def __init__(
        self,
        uow: UowFactory,
        artifacts: ArtifactStore,
        *,
        max_inline_tokens: int = 10_000,
    ):
        self.uow, self.artifacts = uow, artifacts
        self.max_inline_tokens = max_inline_tokens
        self.read_slots = asyncio.Semaphore(4)

    async def execute(
        self,
        run: Run,
        call: ToolCall,
        registry: ToolRegistry,
        admit: Callable[[], None],
        *,
        timeout_seconds: float | None = None,
        model_stream_id: str | None = None,
    ) -> ToolResult:
        async with self.uow() as tx:
            existing = await tx.runs.tool(run.id, call.id)
            previous_calls = await tx.runs.tools(run.id)
        if existing and existing.call != call:
            raise ValueError("同一工具调用 ID 不能重复用于不同调用")
        if existing and existing.state in {"completed", "yielded"} and existing.result:
            return existing.result
        async with self.uow() as tx:
            current = await tx.runs.get(run.id)
            session = await tx.sessions.get(run.session_id)
            stale = current.status == RunStatus.CANCELLED
            if run.parent_id:
                board = await tx.research.get(run.session_id)
                task = next((item for item in board.tasks if item.id == run.id), None)
                stale = (
                    stale
                    or not task
                    or task.task_revision != run.task_revision
                    or task.status in {"cancelled", "stale"}
                )
            else:
                stale = (
                    stale
                    or run.input_revision != current.input_revision
                    or current.input_revision != session.input_revision
                )
                run = run.model_copy(update={"brief": session.brief})
        if stale:
            result = ToolResult(
                content="调用属于旧输入或分工，未执行；请根据当前状态重新决定下一步。",
                is_error=True,
                error_code="stale_revision",
            )
            if not existing:
                await self._save(run, call, result, True, attempts=0)
            return result
        if existing and not existing.replay_safe:
            raise UncertainToolOutcome(
                f"工具 {call.name} 的上一次调用结果不确定，不能自动重复执行。"
            )
        try:
            spec = registry.get(call.name)
            if spec.external_schema:
                external_validator(spec.input_schema).validate(call.arguments)
        except (ValueError, JsonValidationError) as exc:
            result = ToolResult(
                content=f"工具参数或名称错误：{str(exc).splitlines()[0][:400]}",
                is_error=True,
                error_code="invalid_arguments",
            )
            await self._save(run, call, result, False)
            return result
        reusable = next(
            (
                item
                for item in sorted(
                    previous_calls, key=lambda item: item.queued_at, reverse=True
                )
                if item.id != call.id
                and spec.cacheable
                and item.replay_safe
                and item.capability in {"search", "read", "other"}
                and item.state in {"completed", "yielded"}
                and item.result is not None
                and not item.result.is_error
                and item.input_revision == run.input_revision
                and item.call.name == call.name
                and item.call.arguments == call.arguments
            ),
            None,
        )
        if reusable:
            if spec.capability in {"search", "read"}:
                try:
                    await require_research(
                        self.uow,
                        run,
                        unavailable_message=(
                            "本轮 research_questions 为空，不复用旧检索结果"
                        ),
                    )
                except ToolInputError as exc:
                    result = ToolResult(
                        content=str(exc),
                        is_error=True,
                        error_code="invalid_request",
                    )
                    await self._save(run, call, result, spec.replay_safe)
                    return result
            await self._save(
                run,
                call,
                reusable.result,
                True,
                attempts=0,
                capability=reusable.capability,
                model_stream_id=model_stream_id,
            )
            return reusable.result
        try:
            admit()
        except DeadlineExceeded as exc:
            result = ToolResult(
                content=f"{exc}；此调用未执行，请用已有证据回答。",
                is_error=True,
                error_code="deadline_reached",
            )
            await self._save(run, call, result, True, attempts=0)
            return result
        if timeout_seconds is not None and timeout_seconds <= 0:
            result = ToolResult(
                content="剩余时间保留给总结，此工具未执行。",
                is_error=True,
                error_code="deadline_reached",
            )
            await self._save(run, call, result, spec.replay_safe)
            return result
        execution = ToolExecution(
            id=call.id,
            run_id=run.id,
            call=call,
            capability=spec.capability,
            replay_safe=spec.replay_safe,
            input_revision=run.input_revision,
            model_stream_id=model_stream_id
            or (existing.model_stream_id if existing else None),
            queued_at=existing.queued_at if existing else now(),
            state="executing",
            started_at=now(),
            attempts=(
                existing.attempts
                if existing and existing.state == "queued"
                else existing.attempts + 1
                if existing
                else 1
            ),
        )
        async with self.uow() as tx:
            await tx.runs.save_tool(execution)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "tool.started",
                {"execution": execution.model_dump(mode="json")},
            )
        error = None
        context_token = current_tool_call.set(call.id)
        try:
            async with asyncio.timeout(
                min(spec.timeout_seconds, timeout_seconds)
                if timeout_seconds is not None
                else spec.timeout_seconds
            ):
                # Delegation owns its child limit. It must not occupy a read slot
                # while waiting for children that need those same read slots.
                if spec.parallel_safe and spec.replay_safe:
                    async with self.read_slots:
                        result = await spec.invoke(call.arguments)
                else:
                    result = await spec.invoke(call.arguments)
        except (asyncio.CancelledError, DeadlineExceeded) as exc:
            # The ledger intentionally stays 'started': unsafe calls require reconciliation.
            await self._interrupted(run, execution, execution_error(exc))
            raise
        except TimeoutError as exc:
            error = execution_error(exc)
            result = ToolResult(
                content=f"工具 {call.name} 超时。",
                is_error=True,
                error_code="timeout",
                retryable=spec.replay_safe,
            )
            if not spec.replay_safe:
                await self._interrupted(
                    run,
                    execution.model_copy(
                        update={"state": "uncertain", "result": result}
                    ),
                    error,
                )
                raise UncertainToolOutcome(f"工具 {call.name} 超时，结果需人工确认。")
        except (ToolInputError, Conflict, MissingRecord) as exc:
            error = execution_error(exc)
            result = ToolResult(
                content=str(exc), is_error=True, error_code="invalid_request"
            )
        except ValidationError as exc:
            error = execution_error(exc)
            if not spec.replay_safe:
                await self._interrupted(run, execution, error)
                raise UncertainToolOutcome(
                    f"工具 {call.name} 返回值无效，外部执行结果需确认。"
                ) from exc
            result = ToolResult(
                content=f"工具输入或输出不符合声明：{str(exc)[:600]}",
                is_error=True,
                error_code="validation",
            )
        except ModelUnavailable as exc:
            error = execution_error(exc)
            if not spec.replay_safe:
                await self._interrupted(run, execution, error)
                raise UncertainToolOutcome(str(exc)) from exc
            result = ToolResult(
                content=error.message,
                is_error=True,
                error_code=error.code,
                retryable=error.retryable,
            )
        except Exception as exc:
            error = execution_error(exc)
            logger.exception("Tool execution failed: %s", call.name)
            if not spec.replay_safe:
                await self._interrupted(run, execution, error)
                raise UncertainToolOutcome(
                    f"工具 {call.name} 执行异常，外部执行结果需确认。"
                ) from None
            result = ToolResult(
                content=f"工具 {call.name} 执行失败：{error.exception_type}: {error.message}",
                is_error=True,
                error_code="execution_error",
            )
        finally:
            current_tool_call.reset(context_token)
        cards = (
            result.data.get("paper_cards", []) if isinstance(result.data, dict) else []
        )
        if cards:
            saved_cards = []
            async with self.uow() as tx:
                for card in cards:
                    paper = paper_from_card(card)
                    if paper is None:
                        continue
                    paper = await save_paper(tx, paper)
                    await attach_candidate(
                        tx,
                        run.session_id,
                        paper,
                        "web",
                        run.id,
                        run.brief.basis_revision if run.brief else None,
                    )
                    saved_cards.append(
                        {
                            "id": paper.canonical_id,
                            "title": paper.title,
                            "url": str(paper.primary_url or ""),
                            "search_snippet": str(card.get("snippet") or "")[:1200],
                            "search_snippet_notice": (
                                "仅供发现，未经论文原始页面核验；不能作为正式摘要或引用证据。"
                            ),
                            "authors": list(paper.authors[:6]),
                            "year": paper.year,
                            "venue": paper.venue,
                            "quality_signals": {
                                "affiliations": [
                                    affiliation.name
                                    for affiliation in paper.affiliations[:4]
                                ],
                            },
                        }
                    )
                    await tx.sessions.append_event(
                        run.session_id,
                        run.id,
                        "paper.resolved",
                        {
                            "paper": paper.model_dump(mode="json"),
                            "identifier": card["url"],
                            "source": "web_search",
                        },
                    )
            result = result.model_copy(
                update={
                    "content": json.dumps(
                        {"paper_cards": saved_cards}, ensure_ascii=False
                    ),
                    "data": {**result.data, "paper_cards": saved_cards},
                }
            )
        source_previews = []
        snapshot_ids = []
        source_artifacts = []
        source_evidence = []
        preview_size = max(
            500, min(3000, (6500 - len(result.content)) // max(1, len(result.sources)))
        )
        for source in result.sources:
            file_id = await self.artifacts.put(source.text.encode())
            artifact = Artifact(
                session_id=run.session_id,
                run_id=run.id,
                kind="web_source",
                title=str(source.url),
                file_id=file_id,
            )
            snapshot = SourceSnapshot(
                session_id=run.session_id,
                run_id=run.id,
                url=str(source.url),
                artifact_id=artifact.id,
                tool_call_id=call.id,
            )
            async with self.uow() as tx:
                source_url = normalize_url(snapshot.url)
                matches = await tx.papers.find_by_identifiers(
                    {snapshot.url, "url:" + source_url}
                    if source_url
                    else {snapshot.url}
                )
                matches = [
                    paper
                    for paper in matches
                    if any(
                        item.identity_status == "verified"
                        for item in paper.source_records
                    )
                ]
                evidence = Evidence(
                    session_id=run.session_id,
                    run_id=run.id,
                    snapshot_id=snapshot.id,
                    paper_id=matches[0].canonical_id if len(matches) == 1 else None,
                    source_url=snapshot.url,
                    excerpt=source.text,
                    level="web",
                )
                await tx.artifacts.save(artifact)
                await tx.research.save_snapshot(snapshot)
                await tx.papers.save_evidence(evidence)
                await tx.sessions.append_event(
                    run.session_id,
                    run.id,
                    "evidence.created",
                    {"evidence": evidence.model_dump(mode="json")},
                )
            snapshot_ids.append(snapshot.id)
            source_artifacts.append(artifact.id)
            source_evidence.append(evidence.id)
            source_previews.append(
                f"来源 {snapshot.url} · 完整原文 artifact_id={artifact.id}\n"
                + f"[evidence:{evidence.id}]\n{source.text[:preview_size]}"
                + (
                    f"\n（原文共 {len(source.text)} 字符；按需 read_artifact 续读，同一引用适用。）"
                    if len(source.text) > preview_size
                    else ""
                )
            )
        if source_previews:
            result = result.model_copy(
                update={
                    "content": result.content + "\n" + "\n".join(source_previews),
                    "sources": (),
                    "snapshot_ids": tuple(snapshot_ids),
                    "evidence_ids": (*result.evidence_ids, *source_evidence),
                    "artifact_ids": (*result.artifact_ids, *source_artifacts),
                }
            )
        encoded = result.model_dump_json()
        if exactish_tokens(result.content) > self.max_inline_tokens:
            file_id = await self.artifacts.put(encoded.encode())
            artifact = Artifact(
                session_id=run.session_id,
                run_id=run.id,
                kind="tool_result",
                title=call.name,
                file_id=file_id,
                mime_type="application/json",
            )
            async with self.uow() as tx:
                await tx.artifacts.save(artifact)
            result = result.model_copy(
                update={
                    "content": (
                        result.content[: int(self.max_inline_tokens * 2)]
                        + (
                            f"\n（正文截断，共 {len(result.content)} 字符；请通过 artifact 分页续读）"
                            if len(result.content) > self.max_inline_tokens * 2
                            else ""
                        )
                    )
                    + f"\n完整结果保存在 artifact_id={artifact.id}，请通过 read_artifact 分页读取。",
                    "data": {"artifact_id": artifact.id, "characters": len(encoded)},
                    "artifact_ids": (*result.artifact_ids, artifact.id),
                }
            )
        await self._save(run, call, result, spec.replay_safe, error)
        return result

    async def _interrupted(
        self, run: Run, execution: ToolExecution, error: ExecutionError
    ):
        execution = execution.model_copy(
            update={
                "error": error,
                "finished_at": now(),
                "duration_ms": int(
                    (
                        now() - (execution.started_at or execution.queued_at)
                    ).total_seconds()
                    * 1000
                ),
            }
        )
        async with self.uow() as tx:
            await tx.runs.save_tool(execution)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "tool.completed",
                {
                    "execution": execution.model_dump(mode="json"),
                },
            )

    async def _save(
        self,
        run: Run,
        call: ToolCall,
        result: ToolResult,
        replay_safe: bool,
        error: ExecutionError | None = None,
        *,
        attempts: int | None = None,
        capability: str | None = None,
        model_stream_id: str | None = None,
    ) -> None:
        async with self.uow() as tx:
            previous = await tx.runs.tool(run.id, call.id)
            await tx.runs.save_tool(
                ToolExecution(
                    id=call.id,
                    run_id=run.id,
                    call=call,
                    capability=capability
                    or (previous.capability if previous else "other"),
                    state="completed",
                    replay_safe=replay_safe,
                    input_revision=run.input_revision,
                    model_stream_id=model_stream_id
                    or (previous.model_stream_id if previous else None),
                    result=result,
                    queued_at=previous.queued_at if previous else now(),
                    started_at=previous.started_at if previous else now(),
                    finished_at=now(),
                    attempts=(
                        attempts
                        if attempts is not None
                        else previous.attempts
                        if previous
                        else 1
                    ),
                    error=error,
                    duration_ms=int(
                        (
                            now() - (previous.started_at or previous.queued_at)
                        ).total_seconds()
                        * 1000
                    )
                    if previous
                    else 0,
                )
            )
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "tool.completed",
                {
                    "execution": (await tx.runs.tool(run.id, call.id)).model_dump(
                        mode="json"
                    ),
                },
            )

    async def mark_yielded(self, run: Run, call: ToolCall) -> None:
        async with self.uow() as tx:
            execution = await tx.runs.tool(run.id, call.id)
            if execution is None or execution.state != "completed":
                return
            execution = execution.model_copy(
                update={"state": "yielded", "yielded_at": now()}
            )
            await tx.runs.save_tool(execution)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "tool.yielded",
                {"execution": execution.model_dump(mode="json")},
            )
