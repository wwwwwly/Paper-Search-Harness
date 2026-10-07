"""Authoritative Run state transitions and their durable public event."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from paper_search.domain import Conflict, Job, Run, RunStatus, now
from paper_search.ports import UnitOfWork

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
            RunStatus.QUEUED,
        }
    ),
    RunStatus.WAITING: frozenset(
        {RunStatus.QUEUED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.FAILED: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED}),
    RunStatus.INTERRUPTED: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset({RunStatus.QUEUED}),
    RunStatus.CANCELLED: frozenset({RunStatus.QUEUED}),
}


async def transition_run(
    tx: UnitOfWork,
    run_id: str,
    status: RunStatus,
    *,
    reason: str | None = None,
    updates: Mapping[str, Any] | None = None,
    cancellation_wins: bool = False,
) -> Run:
    current = await tx.runs.get(run_id)
    if current.status == RunStatus.CANCELLED and cancellation_wins:
        return current
    if (
        status != current.status
        and status not in ALLOWED_RUN_TRANSITIONS[current.status]
    ):
        raise Conflict(f"非法任务状态转换：{current.status.value} -> {status.value}")
    values = dict(updates or {})
    if "status" in values:
        raise ValueError("status must be passed explicitly")
    default_phase = {
        RunStatus.QUEUED: "queued",
        RunStatus.RUNNING: "research",
        RunStatus.WAITING: "waiting",
        RunStatus.COMPLETED: "finished",
        RunStatus.FAILED: "finished",
        RunStatus.CANCELLED: "finished",
        RunStatus.INTERRUPTED: "waiting",
    }[status]
    values.update(
        status=status,
        phase=values.get("phase", default_phase),
        reason=reason,
        updated_at=now(),
    )
    changed = current.model_copy(update=values)
    await tx.runs.save(changed)
    await tx.sessions.append_event(
        changed.session_id,
        changed.id,
        "run.updated",
        {"run": changed.model_dump(mode="json")},
    )
    return changed


async def wake_document_runs(tx: UnitOfWork, session_id: str):
    for run in await tx.runs.list(session_id):
        if (
            run.parent_id
            or run.status != RunStatus.WAITING
            or not run.waiting_document_ids
        ):
            continue
        documents = [await tx.documents.get(key) for key in run.waiting_document_ids]
        if any(
            item.status not in {"ready", "failed", "cancelled"} for item in documents
        ):
            continue
        elapsed = (
            max(0, (now() - run.waiting_since).total_seconds())
            if run.waiting_since
            else 0
        )
        run = await transition_run(
            tx,
            run.id,
            RunStatus.QUEUED,
            updates={
                "waiting_document_ids": (),
                "waiting_since": None,
                "usage": run.usage.model_copy(
                    update={"waiting_seconds": run.usage.waiting_seconds + elapsed}
                ),
            },
        )
        await tx.jobs.enqueue(
            Job(
                id=f"run-{run.id}",
                kind="run",
                session_id=session_id,
                target_id=run.id,
            )
        )
