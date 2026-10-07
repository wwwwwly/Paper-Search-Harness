from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from paper_search.domain import Job, RunStatus, new_id, now
from paper_search.ports import DeliveryQueue, UowFactory
from paper_search.run_lifecycle import transition_run, wake_document_runs

logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(
        self,
        uow: UowFactory,
        queue: DeliveryQueue,
        handlers: dict[str, Callable[[str], Awaitable]],
    ):
        self.uow, self.queue, self.handlers = uow, queue, handlers
        self.owner = new_id()
        self.tasks: list[asyncio.Task] = []
        self.active: dict[str, asyncio.Task] = {}
        self.stopping = False

    async def start(self):
        # Bootstrap holds the single-instance lock before recovery.
        async with self.uow() as tx:
            for session in await tx.sessions.list():
                await wake_document_runs(tx, session.id)
            for run in await tx.runs.list():
                if run.status == RunStatus.RUNNING:
                    await transition_run(
                        tx,
                        run.id,
                        RunStatus.INTERRUPTED,
                        reason="进程重启，等待显式接续",
                    )
            for job in await tx.jobs.list():
                if job.status == "running":
                    await tx.jobs.enqueue(
                        job.model_copy(
                            update={
                                "status": "pending",
                                "owner": None,
                                "lease_until": None,
                            }
                        )
                    )
        self.tasks = [asyncio.create_task(self._dispatch(), name="outbox-dispatcher")]
        self.tasks.extend(
            asyncio.create_task(self._consume(kind), name=f"worker-{kind}")
            for kind in self.handlers
        )

    async def _dispatch(self):
        while True:
            try:
                async with self.uow() as tx:
                    pending = await tx.jobs.pending_outbox()
                for job in pending:
                    await self.queue.publish(job)
                    async with self.uow() as tx:
                        await tx.jobs.mark_published(job.delivery_id)
            except Exception:
                logger.exception(
                    "Outbox dispatch failed; durable records will be retried"
                )
            await asyncio.sleep(0.2)

    async def _consume(self, kind: str):
        while True:
            delivery = None
            try:
                delivery = await self.queue.receive(kind)
                if delivery is None:
                    await asyncio.sleep(0.2)
                    continue
                async with self.uow() as tx:
                    current = await tx.jobs.get(delivery.job_id)
                    job = (
                        await tx.jobs.claim(delivery.job_id, self.owner, 60)
                        if current.delivery_id == delivery.id
                        else None
                    )
                if job is None:
                    if current.delivery_id != delivery.id or current.status in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        await self.queue.ack(delivery)
                    else:
                        await self.queue.retry(delivery, 5)
                    continue
                task = asyncio.create_task(
                    self.handlers[kind](job.target_id), name=job.id
                )
                self.active[job.target_id] = task
                heartbeat = asyncio.create_task(self._heartbeat(job))
                try:
                    await task
                    status, error = "completed", None
                except asyncio.CancelledError:
                    if self.stopping:
                        raise
                    status, error = "cancelled", "用户取消"
                except Exception as exc:
                    logger.exception("Background job failed: %s", job.id)
                    status, error = "failed", type(exc).__name__
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    self.active.pop(job.target_id, None)
                async with self.uow() as tx:
                    current = await tx.jobs.get(job.id)
                    if (
                        current.owner == self.owner
                        and current.delivery_id == delivery.id
                    ):
                        await tx.jobs.save(
                            current.model_copy(
                                update={
                                    "status": status,
                                    "error": error,
                                    "lease_until": None,
                                }
                            )
                        )
                await self.queue.ack(delivery)
            except asyncio.CancelledError:
                if delivery:
                    await self.queue.retry(delivery, 0)
                raise
            except Exception:
                logger.exception("Delivery processing failed")
                if delivery:
                    await self.queue.retry(delivery, 2)
                await asyncio.sleep(1)

    async def _heartbeat(self, job: Job):
        while True:
            await asyncio.sleep(15)
            async with self.uow() as tx:
                current = await tx.jobs.get(job.id)
                if current.status != "running" or current.owner != self.owner:
                    return
                await tx.jobs.save(
                    current.model_copy(
                        update={"lease_until": now() + timedelta(seconds=60)}
                    )
                )

    def cancel(self, target_id: str):
        task = self.active.get(target_id)
        if task:
            task.cancel()

    async def close(self):
        self.stopping = True
        for task in self.tasks:
            task.cancel()
        for task in self.active.values():
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
