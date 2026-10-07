"""Fast context estimates anchored to the provider's latest exact usage."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import tiktoken

from paper_search.domain import ModelMessage, ModelRequest


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    method: str
    anchor_message_id: str | None = None
    anchor_index: int | None = None


_ENCODING = tiktoken.get_encoding("o200k_base")


def exactish_tokens(text: str) -> int:
    """Local tokenizer fallback; it never adds network latency."""
    return max(1, len(_ENCODING.encode(text, disallowed_special=())))


def rough_tokens(text: str, *, dense_json: bool = False) -> int:
    """Estimate only the small suffix added after an exact server usage anchor."""
    if not text:
        return 0
    ascii_count = sum(ord(char) < 128 for char in text)
    non_ascii = len(text) - ascii_count
    divisor = 2 if dense_json else 4
    return max(1, math.ceil(ascii_count / divisor + non_ascii))


def _message_payload(message: ModelMessage) -> dict:
    value: dict = {"role": message.role, "content": message.content or None}
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
    return value


def _rough_message(message: ModelMessage) -> int:
    payload = _message_payload(message)
    dense = message.role == "tool" or bool(message.tool_calls)
    return rough_tokens(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        dense_json=dense,
    )


def estimate_messages(
    messages: tuple[ModelMessage, ...] | list[ModelMessage],
    *,
    tool_schema_tokens: int = 0,
) -> TokenEstimate:
    """Use exact provider usage plus a small suffix, falling back to local tokenization."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        usage = message.usage
        if usage and not usage.estimated and usage.input_tokens > 0:
            suffix = sum(_rough_message(item) for item in messages[index + 1 :])
            # OpenAI-compatible prompt_tokens already includes its cached subset.
            return TokenEstimate(
                tokens=max(1, usage.input_tokens + usage.output_tokens + suffix),
                method="provider_usage_anchor",
                anchor_message_id=message.provider_response_id,
                anchor_index=index,
            )
    payload = json.dumps(
        [_message_payload(message) for message in messages],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return TokenEstimate(
        tokens=exactish_tokens(payload) + tool_schema_tokens,
        method="o200k_full",
    )


def estimate_request(request: ModelRequest) -> TokenEstimate:
    tools = json.dumps(request.tools, ensure_ascii=False, separators=(",", ":"))
    estimate = estimate_messages(
        request.messages,
        tool_schema_tokens=exactish_tokens(tools) if request.tools else 0,
    )
    return estimate
