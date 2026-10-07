from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from paper_search.config.settings import EmbeddingProfile, ModelProfile
from paper_search.domain import (
    ModelEvent,
    ModelRequest,
    ModelUnavailable,
    ToolCall,
    Usage,
)
from paper_search.ports import ArtifactStore


class OpenAIModelClient:
    """OpenAI-compatible streaming. Provider types never leave this adapter."""

    def __init__(self, profile: ModelProfile, artifacts: ArtifactStore):
        self.profile = profile
        self.artifacts = artifacts
        self.client = AsyncOpenAI(
            # The workbench can open before credentials are configured.
            # stream() checks the real key before issuing any request.
            api_key=profile.api_key or "not-configured",
            base_url=profile.base_url,
            timeout=profile.timeout_seconds,
            max_retries=0,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if not self.profile.api_key:
            raise ValueError(f"模型凭据未配置：{self.profile.model} 的 api_key")
        options = request.options
        model = options.model if options else self.profile.model
        endpoint = options.base_url if options else self.profile.base_url
        messages = []
        for message in request.messages:
            value: dict[str, Any] = {
                "role": message.role,
                "content": message.content or None,
            }
            if message.tool_calls:
                value["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            if message.tool_call_id:
                value["tool_call_id"] = message.tool_call_id
            if message.continuation_ref:
                state = json.loads(await self.artifacts.read(message.continuation_ref))
                if state["model"] == model and state["endpoint"] == endpoint:
                    value["reasoning_content"] = state["reasoning_content"]
            messages.append(value)
        arguments: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.max_output_tokens is not None:
            arguments["max_tokens"] = request.max_output_tokens
        elif options and options.force_max_output_tokens:
            arguments["max_tokens"] = options.max_output_tokens_supported
        if request.tools:
            arguments["tools"] = list(request.tools)
        if request.response_format == "json_object":
            arguments["response_format"] = {"type": "json_object"}
        extra_body = options.extra_body if options else self.profile.extra_body
        if extra_body:
            arguments["extra_body"] = extra_body
        if options and options.reasoning_effort:
            arguments["reasoning_effort"] = options.reasoning_effort
        try:
            response = await self.client.with_options(
                base_url=endpoint
            ).chat.completions.create(**arguments)
        except APIStatusError as exc:
            raise ModelUnavailable(
                f"模型服务 HTTP {exc.status_code}",
                retryable=exc.status_code in {429, 500, 502, 503, 504},
                code="http_error",
                status=exc.status_code,
                request_id=exc.request_id,
            ) from None
        except APIConnectionError:
            raise ModelUnavailable(
                "模型连接失败或超时", retryable=True, code="connection_error"
            ) from None
        calls: dict[int, dict[str, str]] = {}
        emitted_calls: dict[int, ToolCall] = {}
        usage = None
        finish_reason = None
        reasoning = ""
        request_id = None
        try:
            async for chunk in response:
                request_id = chunk.id or request_id
                if chunk.usage:
                    usage = Usage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                        model_calls=1,
                        reasoning_tokens=getattr(
                            chunk.usage.completion_tokens_details, "reasoning_tokens", 0
                        )
                        or 0,
                        cached_input_tokens=getattr(
                            chunk.usage.prompt_tokens_details, "cached_tokens", 0
                        )
                        or 0,
                    )
                    yield ModelEvent(
                        type="usage", usage=usage, provider_request_id=request_id
                    )
                for choice in chunk.choices:
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    reasoning += getattr(delta, "reasoning_content", None) or ""
                    if delta.content:
                        yield ModelEvent(type="text", text=delta.content)
                    for part in delta.tool_calls or []:
                        if part.index in emitted_calls and (
                            part.id
                            or (
                                part.function
                                and (
                                    part.function.name
                                    or (part.function.arguments or "").strip()
                                )
                            )
                        ):
                            raise ModelUnavailable(
                                "工具调用在完整 JSON 事件后仍发生变化",
                                code="invalid_tool_arguments",
                                retryable=True,
                                request_id=request_id,
                            )
                        call = calls.setdefault(
                            part.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if part.id:
                            call["id"] = part.id
                        if part.function:
                            call["name"] += part.function.name or ""
                            call["arguments"] += part.function.arguments or ""
                        if not call["arguments"].strip():
                            continue
                        try:
                            parsed_arguments = json.loads(call["arguments"])
                        except json.JSONDecodeError:
                            continue
                        if (
                            call["id"]
                            and call["name"]
                            and isinstance(parsed_arguments, dict)
                            and part.index not in emitted_calls
                        ):
                            complete = ToolCall(
                                id=call["id"],
                                name=call["name"],
                                arguments=parsed_arguments,
                            )
                            emitted_calls[part.index] = complete
                            yield ModelEvent(type="tool_call", tool_call=complete)
            if finish_reason not in {"stop", "tool_calls"}:
                raise ModelUnavailable(
                    f"模型响应未正常结束：{finish_reason or 'stream interrupted'}",
                    code="output_limit"
                    if finish_reason == "length"
                    else "incomplete_response",
                    finish_reason=finish_reason,
                    request_id=request_id,
                )
            parsed = []
            try:
                for _, call in sorted(calls.items()):
                    arguments = json.loads(call["arguments"] or "{}")
                    if (
                        not call["id"]
                        or not call["name"]
                        or not isinstance(arguments, dict)
                    ):
                        raise ValueError("工具调用须包含 ID、名称和 JSON 对象参数")
                    parsed.append(
                        ToolCall(id=call["id"], name=call["name"], arguments=arguments)
                    )
                if len({call.id for call in parsed}) != len(parsed):
                    raise ValueError("工具调用 ID 重复")
            except ValueError as exc:
                raw = await self.artifacts.put(
                    json.dumps(
                        {
                            "calls": calls,
                            "request_id": request_id,
                            "finish_reason": finish_reason,
                        },
                        ensure_ascii=False,
                    ).encode()
                )
                raise ModelUnavailable(
                    f"模型工具参数无效：{exc}；原始响应 file_id={raw}。只有此前已发出的完整只读调用可能已执行。",
                    code="invalid_tool_arguments",
                    retryable=True,
                    request_id=request_id,
                    finish_reason=finish_reason,
                ) from None
            continuation = None
            if reasoning and parsed:
                continuation = await self.artifacts.put(
                    json.dumps(
                        {
                            "model": model,
                            "endpoint": endpoint,
                            "reasoning_content": reasoning,
                        },
                        ensure_ascii=False,
                    ).encode()
                )
            yield ModelEvent(
                type="done",
                tool_calls=tuple(parsed),
                usage=usage,
                continuation_ref=continuation,
                provider_request_id=request_id,
                finish_reason=finish_reason,
            )
        except (APIConnectionError, APIStatusError, httpx.RequestError):
            raise ModelUnavailable(
                "模型响应流中断，已保存执行记录；请接续执行",
                code="stream_interrupted",
                request_id=request_id,
            ) from None
        finally:
            await response.close()

    async def close(self):
        await self.client.close()


class OpenAIEmbeddingClient:
    def __init__(self, profile: EmbeddingProfile):
        self.profile = profile
        self.client = AsyncOpenAI(
            # embed() rejects missing credentials before issuing any request.
            api_key=profile.api_key or "not-configured",
            base_url=profile.base_url,
            max_retries=0,
            timeout=60,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.profile.api_key:
            raise ValueError("向量模型已启用，但未配置凭据")
        response = await self.client.embeddings.create(
            model=self.profile.model, input=texts, dimensions=self.profile.dimensions
        )
        return [
            item.embedding
            for item in sorted(response.data, key=lambda item: item.index)
        ]

    async def close(self):
        await self.client.close()
