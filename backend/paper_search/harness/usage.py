"""Reconstruct usage from unique persisted model calls, including uncertain requests."""

from paper_search.domain import Usage


async def restore_usage(tx, run, related):
    owners = {
        item.id: Usage(
            tool_calls=sum(tool.attempts for tool in await tx.runs.tools(item.id))
        )
        for item in related
    }
    calls, cursor = {}, 0
    while events := await tx.sessions.events(run.session_id, cursor, 1000):
        cursor = events[-1].seq
        for event in events:
            if event.run_id not in owners:
                continue
            if event.type == "model.started":
                calls[event.data.stream_id] = (
                    event.run_id,
                    Usage(
                        estimated=True,
                        estimated_input_tokens=event.data.reserved_input_tokens
                        or event.data.estimated_input_tokens,
                        estimated_output_tokens=event.data.reserved_output_tokens,
                    ),
                )
            elif event.type == "model.finished":
                calls[event.data.stream_id] = (event.run_id, event.data.usage)
    for owner, usage in calls.values():
        if usage.estimated:
            usage = usage.model_copy(
                update={
                    "estimated_input_tokens": usage.estimated_input_tokens
                    + usage.input_tokens,
                    "estimated_output_tokens": usage.estimated_output_tokens
                    + usage.output_tokens,
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
            )
        previous = owners[owner]
        fields = {
            name: getattr(previous, name) + getattr(usage, name)
            for name in (
                "input_tokens",
                "output_tokens",
                "estimated_input_tokens",
                "estimated_output_tokens",
                "reasoning_tokens",
                "cached_input_tokens",
                "cost_cny",
            )
        }
        owners[owner] = previous.model_copy(
            update={
                **fields,
                "model_calls": previous.model_calls + 1,
                "estimated": previous.estimated or usage.estimated,
            }
        )
    fields = {
        name: sum(getattr(item, name) for item in owners.values())
        for name in (
            "input_tokens",
            "output_tokens",
            "estimated_input_tokens",
            "estimated_output_tokens",
            "model_calls",
            "tool_calls",
            "reasoning_tokens",
            "cached_input_tokens",
            "cost_cny",
        )
    }
    return Usage(
        **fields,
        estimated=any(item.estimated for item in owners.values()),
        active_seconds=run.usage.active_seconds,
    ), owners
