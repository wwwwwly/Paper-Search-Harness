"""Deterministic context projections used before the LLM summary fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass

from paper_search.domain import Message, ModelMessage, ToolResult
from paper_search.harness.token_estimation import exactish_tokens

COLLAPSE_COMMIT_RATIO = 0.90
COLLAPSE_TARGET_RATIO = 0.80
COLLAPSE_BLOCK_RATIO = 0.95
HISTORY_SNIP_RATIO = 0.60
MICROCOMPACT_KEEP_RECENT = 5
MICROCOMPACT_WORKING_SET_RATIO = 0.20
AUTOCOMPACT_BUFFER_TOKENS = 13_000
AUTOCOMPACT_BUFFER_RATIO = 0.065
SUMMARY_OUTPUT_CAP = 20_000
MAX_AUTOCOMPACT_FAILURES = 3

_REPEATED_MARKER = "[重复段落已省略；以最近一次完整表述为准]"


@dataclass(frozen=True)
class ProjectionChange:
    messages: list[Message]
    changed_messages: int = 0
    estimated_tokens_saved: int = 0


def tool_key(run_id: str, call_id: str) -> str:
    return f"{run_id}:{call_id}"


def project_messages(
    messages: list[Message],
    states: dict[str, str] | None = None,
    *,
    active_run_id: str | None = None,
    cleared_tool_keys: set[str] | None = None,
    collapsed_tool_keys: set[str] | None = None,
    cleared_continuation_message_ids: set[str] | None = None,
    context_revision: int = 0,
) -> list[ModelMessage]:
    """Application messages stay provider-neutral, with complete tool exchanges."""
    states = states or {}
    cleared_tool_keys = cleared_tool_keys or set()
    collapsed_tool_keys = collapsed_tool_keys or set()
    cleared_continuation_message_ids = cleared_continuation_message_ids or set()
    results = {
        (item.run_id, item.tool_call_id): item
        for item in messages
        if item.role == "tool"
    }
    output = []
    for item in messages:
        if item.role == "tool":
            continue
        content = item.content
        if item.role == "user":
            content = (
                f"[message_id={item.id}, input_revision={item.input_revision}]\n"
                + content
            )
        if item.document_ids:
            content += "\n附加文档 ID：" + ", ".join(item.document_ids)
        calls = tuple(
            call.model_copy(update={"id": f"{item.run_id}_{call.id}"})
            for call in item.tool_calls
        )
        original_keys = [tool_key(item.run_id, call.id) for call in item.tool_calls]
        if original_keys and all(key in collapsed_tool_keys for key in original_keys):
            capsules = []
            for call, key in zip(item.tool_calls, original_keys, strict=True):
                result = results[(item.run_id, call.id)]
                value = result.tool_result or ToolResult(content=result.content)
                stable_ids = tuple(
                    dict.fromkeys(
                        re.findall(
                            r"(?:arxiv:)?\d{4}\.\d{4,5}(?:v\d+)?|doi:10\.\d{4,9}/[^\s\]\}\"']+",
                            result.content,
                            flags=re.IGNORECASE,
                        )
                    )
                )[:8]
                references = [
                    *(f"evidence:{item}" for item in value.evidence_ids),
                    *(f"artifact:{item}" for item in value.artifact_ids),
                    *(f"snapshot:{item}" for item in value.snapshot_ids),
                    *(f"paper:{item}" for item in stable_ids),
                ]
                preview = " ".join(result.content.split())[:120]
                capsules.append(
                    f"{call.name} status={'error' if value.is_error else 'ok'}"
                    + (" refs=" + ",".join(references) if references else "")
                    + (" preview=" + preview if preview else "")
                )
            output.append(
                ModelMessage(
                    role="assistant",
                    content=(
                        (" ".join(content.split())[:120] + "\n") if content else ""
                    )
                    + "[collapsed tool batch]\n"
                    + "\n".join(capsules),
                    context_revision=context_revision,
                )
            )
            continue
        output.append(
            ModelMessage(
                role=item.role,
                content=content,
                tool_calls=calls,
                continuation_ref=(
                    None
                    if item.id in cleared_continuation_message_ids
                    else states.get(item.id)
                ),
                usage=(
                    item.usage if item.context_revision == context_revision else None
                ),
                provider_response_id=item.provider_response_id,
                context_revision=item.context_revision,
            )
        )
        for call in item.tool_calls:
            result = results.get((item.run_id, call.id))
            if result is None:
                raise ValueError(
                    "Cannot build model context with an unsettled tool call"
                )
            tool_content = result.content
            key = tool_key(item.run_id, call.id)
            if key in cleared_tool_keys:
                tool_result = result.tool_result or ToolResult(content=result.content)
                references = []
                if tool_result.evidence_ids:
                    references.append("evidence=" + ",".join(tool_result.evidence_ids))
                if tool_result.artifact_ids:
                    references.append("artifact=" + ",".join(tool_result.artifact_ids))
                if tool_result.snapshot_ids:
                    references.append("snapshot=" + ",".join(tool_result.snapshot_ids))
                tool_content = "旧工具结果已清理"
                if references:
                    tool_content += "；" + "；".join(references)
            elif (
                active_run_id
                and result.run_id != active_run_id
                and result.tool_result
                and result.tool_result.artifact_ids
            ):
                artifacts = ", ".join(result.tool_result.artifact_ids)
                tool_content = (
                    "旧工具结果已从模型上下文清理；完整结果仍保存在 "
                    f"artifact_id={artifacts}，必要时使用 read_artifact 定点读取。"
                )
            output.append(
                ModelMessage(
                    role="tool",
                    tool_call_id=f"{item.run_id}_{call.id}",
                    content=tool_content,
                    context_revision=context_revision,
                )
            )
    return output


def effective_window(context_window: int, output_headroom: int) -> int:
    """Reserve context capacity without limiting the provider's output."""
    return max(1, context_window - output_headroom)


def autocompact_threshold(window: int) -> int:
    buffer = min(
        AUTOCOMPACT_BUFFER_TOKENS,
        max(1_024, round(window * AUTOCOMPACT_BUFFER_RATIO)),
    )
    return max(1, window - buffer)


def snip_repeated_history(messages: list[Message]) -> ProjectionChange:
    """Remove only exact repeated user paragraphs, preserving their latest copy."""
    occurrences: dict[str, list[tuple[int, int]]] = {}
    split: dict[int, list[str]] = {}
    for message_index, message in enumerate(messages):
        if message.role != "user":
            continue
        paragraphs = re.split(r"(\n\s*\n)", message.content)
        split[message_index] = paragraphs
        for paragraph_index, paragraph in enumerate(paragraphs):
            normalized = " ".join(paragraph.split())
            if len(normalized) < 80 or not normalized.strip():
                continue
            occurrences.setdefault(normalized, []).append(
                (message_index, paragraph_index)
            )
    replacements: dict[tuple[int, int], str] = {}
    for positions in occurrences.values():
        for position in positions[:-1]:
            replacements[position] = _REPEATED_MARKER
    if not replacements:
        return ProjectionChange(messages)
    changed = []
    saved = 0
    for index, message in enumerate(messages):
        paragraphs = split.get(index)
        if paragraphs is None:
            changed.append(message)
            continue
        revised = list(paragraphs)
        touched = False
        for paragraph_index in range(len(revised)):
            replacement = replacements.get((index, paragraph_index))
            if replacement is None:
                continue
            saved += max(0, exactish_tokens(revised[paragraph_index]) - 8)
            revised[paragraph_index] = replacement
            touched = True
        changed.append(
            message.model_copy(update={"content": "".join(revised)})
            if touched
            else message
        )
    return ProjectionChange(
        changed,
        changed_messages=len({position[0] for position in replacements}),
        estimated_tokens_saved=saved,
    )


def time_microcompact_selection(
    messages: list[Message],
    *,
    replay_safe_tool_keys: set[str],
    continuation_message_ids: set[str],
    window: int,
) -> tuple[set[str], set[str]]:
    """Keep a small recent working set after the prompt cache is known to be cold."""
    latest_batch: set[str] = set()
    for message in reversed(messages):
        if message.role == "assistant" and message.tool_calls:
            latest_batch = {
                tool_key(message.run_id, call.id) for call in message.tool_calls
            }
            break
    eligible: list[tuple[str, int]] = []
    result_tokens: dict[str, int] = {}
    for message in messages:
        if message.role != "tool" or not message.tool_call_id:
            continue
        key = tool_key(message.run_id, message.tool_call_id)
        result_tokens[key] = exactish_tokens(message.content)
        result = message.tool_result
        if (
            key in latest_batch
            or key not in replay_safe_tool_keys
            or (result and result.is_error)
        ):
            continue
        eligible.append((key, result_tokens[key]))
    keep = set(latest_batch)
    kept_tokens = sum(result_tokens.get(key, 0) for key in keep)
    budget = max(1, round(window * MICROCOMPACT_WORKING_SET_RATIO))
    for key, tokens in reversed(eligible):
        if not keep:
            keep.add(key)
            kept_tokens += tokens
            continue
        if len(keep) >= MICROCOMPACT_KEEP_RECENT or kept_tokens + tokens > budget:
            continue
        keep.add(key)
        kept_tokens += tokens
    clear_tools = {key for key, _ in eligible if key not in keep}
    ordered_continuations = [
        message.id
        for message in messages
        if message.id in continuation_message_ids
        and message.role == "assistant"
        and message.tool_calls
    ]
    clear_continuations = set(ordered_continuations[:-1])
    return clear_tools, clear_continuations


def collapse_batches(
    messages: list[Message],
    *,
    already_collapsed: set[str],
    tokens_to_reclaim: int,
) -> set[str]:
    """Choose complete old tool batches, leaving the three newest batches intact."""
    results = {
        tool_key(message.run_id, message.tool_call_id): message
        for message in messages
        if message.role == "tool" and message.tool_call_id
    }
    batches: list[tuple[list[str], int]] = []
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        keys = [tool_key(message.run_id, call.id) for call in message.tool_calls]
        if any(key not in results for key in keys):
            continue
        batch_tokens = exactish_tokens(message.content)
        batch_tokens += sum(exactish_tokens(results[key].content) for key in keys)
        batches.append((keys, batch_tokens))
    selected = set(already_collapsed)
    reclaimed = 0
    for keys, tokens in batches[:-3]:
        if all(key in selected for key in keys):
            continue
        selected.update(keys)
        reclaimed += tokens
        if reclaimed >= tokens_to_reclaim:
            break
    return selected
