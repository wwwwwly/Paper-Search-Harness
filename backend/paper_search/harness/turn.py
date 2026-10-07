from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ValidationError

from paper_search.diagnostics import current_tool_call, execution_error
from paper_search.domain import (
    DeadlineExceeded,
    ModelMessage,
    ModelRequest,
    ModelUnavailable,
    Run,
    RunStatus,
    ToolCall,
    Usage,
    WorkingSummary,
    new_id,
    now,
)
from paper_search.harness.context_compression import SUMMARY_OUTPUT_CAP
from paper_search.harness.limits import RunMeter
from paper_search.harness.token_estimation import (
    TokenEstimate,
    estimate_request,
    exactish_tokens,
    rough_tokens,
)
from paper_search.ports import ModelClient
from paper_search.run_lifecycle import transition_run


class InvalidStructuredOutput(ValueError):
    def __init__(self, text: str, detail: str):
        super().__init__(detail)
        self.text = text


@dataclass(frozen=True)
class TurnResponse:
    content: str
    tool_calls: tuple[ToolCall, ...]
    message_id: str
    started_at: datetime
    usage: Usage
    provider_response_id: str | None


class TurnMixin:
    async def complete_structured(
        self,
        run: Run,
        client: ModelClient,
        profile,
        budget: RunMeter,
        prompt: str,
        output_type: type[BaseModel],
        *,
        purpose: str = "structured",
    ):
        options = profile
        timeout = None
        if purpose == "summary":
            options = profile.model_copy(update={"reasoning_effort": "low"})
        messages = (
            {
                "role": "system",
                "content": (
                    "Return only a JSON object matching this schema: "
                    + json.dumps(output_type.model_json_schema(), ensure_ascii=False)
                ),
            },
            {"role": "user", "content": prompt},
        )
        for attempt in range(2):
            request = ModelRequest(
                messages=messages,
                max_output_tokens=None,
                options=options,
                purpose=purpose if purpose != "structured" else output_type.__name__,
                timeout_seconds=timeout,
                response_format="json_object",
            )
            try:
                response = await self._respond(
                    run,
                    client,
                    request,
                    budget,
                    publish=False,
                    response_type=output_type,
                )
                text = response.content
                break
            except InvalidStructuredOutput as exc:
                if attempt:
                    raise
                messages = (
                    *messages,
                    {"role": "assistant", "content": exc.text},
                    {
                        "role": "user",
                        "content": "Repair only the JSON schema errors; preserve the evidence-based judgment. "
                        + str(exc)[:1500],
                    },
                )
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return output_type.model_validate_json(text)

    async def complete_compaction(
        self,
        run: Run,
        client: ModelClient,
        profile,
        budget: RunMeter,
        prompt: str,
    ) -> WorkingSummary:
        """One isolated, text-only compaction call; it never enters the agent loop."""
        options = profile.model_copy(update={"reasoning_effort": "low"})
        request = ModelRequest(
            messages=(
                ModelMessage(
                    role="system",
                    content=(
                        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. "
                        "Return <analysis> followed by <summary>."
                    ),
                ),
                ModelMessage(role="user", content=prompt),
            ),
            tools=(),
            max_output_tokens=SUMMARY_OUTPUT_CAP,
            options=options,
            purpose="compact",
        )
        response = await self._respond(
            run,
            client,
            request,
            budget,
            publish=False,
        )
        match = re.search(r"<summary>(.*?)</summary>", response.content, re.DOTALL)
        if not match or not match.group(1).strip():
            raise InvalidStructuredOutput(
                response.content,
                "compact response must contain a non-empty <summary> block",
            )
        return WorkingSummary(text=match.group(1).strip())

    async def _estimate_request_input(
        self, request: ModelRequest
    ) -> tuple[int, TokenEstimate]:
        estimate = estimate_request(request)
        start = 0 if estimate.anchor_index is None else estimate.anchor_index + 1
        continuation_tokens = 0
        for message in request.messages[start:]:
            if message.continuation_ref:
                continuation_tokens += exactish_tokens(
                    (await self.artifacts.read(message.continuation_ref)).decode()
                )
        return estimate.tokens + continuation_tokens, estimate

    async def _respond(
        self,
        run: Run,
        client: ModelClient,
        request: ModelRequest,
        budget: RunMeter,
        *,
        publish: bool = True,
        final: bool = False,
        response_type: type[BaseModel] | None = None,
    ):
        network_retries = 0
        continuation_retries = 0
        native_limit_retried = False
        accumulated = ""
        while True:
            try:
                response = await self._respond_once(
                    run,
                    client,
                    request,
                    budget,
                    publish=publish,
                    final=final,
                    response_type=response_type,
                )
                if not accumulated:
                    return response
                return TurnResponse(
                    content=accumulated + response.content,
                    tool_calls=response.tool_calls,
                    message_id=response.message_id,
                    started_at=response.started_at,
                    usage=response.usage,
                    provider_response_id=response.provider_response_id,
                )
            except ModelUnavailable as exc:
                options = request.options or run.execution.model
                recover_hidden_stream = not publish and exc.code == "stream_interrupted"
                if exc.code == "output_limit":
                    if request.max_output_tokens is None and not native_limit_retried:
                        native_limit_retried = True
                        request = request.model_copy(
                            update={
                                "max_output_tokens": options.max_output_tokens_supported
                            }
                        )
                    elif exc.partial_content and continuation_retries < 3:
                        continuation_retries += 1
                        accumulated += exc.partial_content
                        request = request.model_copy(
                            update={
                                "max_output_tokens": options.max_output_tokens_supported,
                                "messages": (
                                    *request.messages,
                                    ModelMessage(
                                        role="assistant", content=exc.partial_content
                                    ),
                                    ModelMessage(
                                        role="user",
                                        content=(
                                            "Continue exactly where the previous response was cut off. "
                                            "Do not repeat completed text and preserve the requested format."
                                        ),
                                    ),
                                ),
                            }
                        )
                    else:
                        raise
                elif exc.code == "invalid_tool_arguments" and network_retries < 2:
                    network_retries += 1
                    request = request.model_copy(
                        update={
                            "messages": (
                                *request.messages,
                                ModelMessage(
                                    role="system",
                                    content=(
                                        "上次响应的工具参数不是有效 JSON，本轮工具均未执行。"
                                        "请重新生成有效 JSON 对象；调用 ID 不得重复。"
                                    ),
                                ),
                            )
                        }
                    )
                elif (exc.retryable or recover_hidden_stream) and network_retries < 2:
                    network_retries += 1
                else:
                    raise
                await self.emit(
                    run,
                    "model.retry",
                    {
                        "attempt": network_retries
                        + continuation_retries
                        + int(native_limit_retried),
                        "network_retry": network_retries,
                        "continuation_retry": continuation_retries,
                        "explicit_native_limit": native_limit_retried,
                        "reason": str(exc),
                    },
                )
                if budget.research_seconds > 0:
                    retry_timeout = budget.research_seconds
                    request = request.model_copy(
                        update={
                            "timeout_seconds": min(
                                request.timeout_seconds or retry_timeout,
                                retry_timeout,
                            )
                        }
                    )
                if network_retries:
                    await asyncio.sleep(1)

    async def _respond_once(
        self,
        run: Run,
        client: ModelClient,
        request: ModelRequest,
        budget: RunMeter,
        *,
        publish: bool = True,
        final: bool = False,
        response_type: type[BaseModel] | None = None,
    ):
        request = request.model_copy(
            update={"options": request.options or run.execution.model}
        )
        if not request.tools:
            request = request.model_copy(
                update={
                    "messages": tuple(
                        message.model_copy(update={"continuation_ref": None})
                        for message in request.messages
                    )
                }
            )
        from paper_search.domain import ToolInputError
        from paper_search.harness.conversation import ContextChanged
        from paper_search.research import current_basis

        async with self.uow() as tx:
            if run.parent_id:
                try:
                    await current_basis(tx, run)
                except ToolInputError as exc:
                    raise ContextChanged(str(exc)) from exc
            else:
                current = await tx.runs.get(run.id)
                session = await tx.sessions.get(run.session_id)
                if (
                    current.status != RunStatus.RUNNING
                    or current.input_revision != run.input_revision
                    or current.input_revision != session.input_revision
                    or current.deadline_at != budget.deadline_at
                ):
                    raise ContextChanged("新输入或预算段已接管当前 Run")
        estimated_input, token_estimate = await self._estimate_request_input(request)
        reserved_input = math.ceil(estimated_input * 1.10)
        budget_purpose = (
            "final"
            if final
            else "compression"
            if request.purpose in {"summary", "compact"}
            else "research"
            if request.purpose == "agent"
            else "auxiliary"
        )
        reserved = budget.model(
            reserved_input,
            request.max_output_tokens or 4096,
            final=final,
            purpose=budget_purpose,
            owner=run.id,
        )
        if final:
            async with self.uow() as tx:
                current = await tx.runs.get(run.id)
                if current.deadline_at != budget.deadline_at:
                    budget.release_unissued(reserved)
                    raise ContextChanged("新预算段已接管当前 Run")
                current = await transition_run(
                    tx,
                    run.id,
                    RunStatus.RUNNING,
                    reason=budget.finalization_reason,
                    updates={"phase": "delivery", "usage": budget.usage},
                    cancellation_wins=True,
                )
                if current.status == RunStatus.CANCELLED:
                    budget.release_unissued(reserved)
                    raise asyncio.CancelledError
        request_file = await self.artifacts.put(request.model_dump_json().encode())
        stream_id = new_id()
        started_at = now()
        async with self.uow() as tx:
            root = await tx.runs.get(run.parent_id or run.id)
            await tx.runs.save(
                root.model_copy(
                    update={
                        "usage": budget.durable_usage(),
                    }
                )
            )
        await self.emit(
            run,
            "model.started",
            {
                "stream_id": stream_id,
                "request_file": request_file,
                "estimated_input_tokens": estimated_input,
                "reserved_input_tokens": reserved_input,
                "reserved_output_tokens": reserved.output_tokens,
                "token_estimate_method": token_estimate.method,
                "publish": publish,
                "created_at": started_at.isoformat(),
                "purpose": request.purpose,
                "parent_tool_call_id": current_tool_call.get(),
                "model": request.options.model,
                "reasoning_effort": request.options.reasoning_effort,
                "max_output_tokens": request.max_output_tokens,
            },
        )
        content = ""
        calls: tuple[ToolCall, ...] = ()
        usage = None
        done = False
        continuation_ref = None
        error = None
        provider_request_id = None
        finish_reason = None
        first_text_ms = None
        started_clock = time.monotonic()
        try:
            timeout = min(
                request.timeout_seconds or budget.remaining_seconds,
                max(
                    0,
                    (budget.remaining_seconds if final else budget.research_seconds)
                    - 1,
                ),
            )
            # Every issued call is bounded by the research deadline or delivery reserve.
            async with asyncio.timeout(timeout):
                async for event in client.stream(request):
                    if event.type == "text":
                        if first_text_ms is None:
                            first_text_ms = int(
                                (time.monotonic() - started_clock) * 1000
                            )
                        content += event.text
                        if publish:
                            await self.emit(
                                run,
                                "message.delta",
                                {"stream_id": stream_id, "text": event.text},
                            )
                    elif event.type == "usage":
                        usage = event.usage
                        provider_request_id = event.provider_request_id
                    elif event.type == "done":
                        calls, usage, done = event.tool_calls, event.usage, True
                        continuation_ref = event.continuation_ref
                        provider_request_id = event.provider_request_id
                        finish_reason = event.finish_reason
            if not done:
                raise RuntimeError("模型响应流缺少完成事件")
            if response_type:
                value = content
                if value.startswith("```"):
                    value = value.split("\n", 1)[1].rsplit("```", 1)[0]
                try:
                    response_type.model_validate_json(value)
                except ValidationError as exc:
                    raise InvalidStructuredOutput(content, str(exc)) from exc
            if continuation_ref:
                async with self.uow() as tx:
                    await tx.runs.save_model_state(stream_id, continuation_ref)
        except ModelUnavailable as exc:
            exc.partial_content = content
            error = execution_error(exc)
            raise
        except BaseException as exc:
            if isinstance(exc, DeadlineExceeded) and usage is None:
                # Admission was denied before the provider request. Do not charge
                # a fabricated full response against the final-answer reserve.
                usage = Usage()
            error = execution_error(exc)
            raise
        finally:
            actual = usage or Usage(
                input_tokens=estimated_input,
                output_tokens=rough_tokens(content),
                estimated=True,
            )
            budget.settle(reserved, actual)
            durable_usage = budget.durable_usage()
            async with self.uow() as tx:
                current = await tx.runs.get(run.id)
                await tx.runs.save(
                    current.model_copy(
                        update={
                            "usage": budget.own_usage(run.id)
                            if run.parent_id
                            else durable_usage,
                            "own_usage": budget.own_usage(run.id),
                        }
                    )
                )
                if run.parent_id:
                    parent = await tx.runs.get(run.parent_id)
                    await tx.runs.save(
                        parent.model_copy(update={"usage": durable_usage})
                    )
                await tx.sessions.append_event(
                    run.session_id,
                    run.id,
                    "model.finished",
                    {
                        "stream_id": stream_id,
                        "usage": actual.model_dump(mode="json"),
                        "completed": done and error is None,
                        "duration_ms": int((time.monotonic() - started_clock) * 1000),
                        "first_text_ms": first_text_ms,
                        "error": error.model_dump(mode="json") if error else None,
                        "provider_request_id": provider_request_id
                        or (error.provider_request_id if error else None),
                        "finish_reason": finish_reason
                        or (error.finish_reason if error else None),
                        "response_file": await self.artifacts.put(content.encode())
                        if content
                        else None,
                    },
                )
                root = await tx.runs.get(run.parent_id or run.id)
                await tx.sessions.append_event(
                    run.session_id,
                    root.id,
                    "run.updated",
                    {
                        "run": root.model_copy(
                            update={"usage": budget.usage}
                        ).model_dump(mode="json")
                    },
                )
        return TurnResponse(
            content=content,
            tool_calls=calls,
            message_id=stream_id,
            started_at=started_at,
            usage=actual,
            provider_response_id=provider_request_id,
        )
