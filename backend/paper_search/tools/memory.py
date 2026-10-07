from __future__ import annotations

from paper_search.domain import (
    Memory,
    Run,
    ToolInputError,
    now,
)
from paper_search.memory import MemoryService
from paper_search.ports import UowFactory


class MemoryTools:
    def __init__(self, run: Run, uow: UowFactory, memory: MemoryService):
        self.run, self.uow, self.memory = run, uow, memory

    async def remember_preference(
        self, key: str, quote: str, source_message_id: str, global_scope: bool = False
    ) -> Memory:
        """仅在用户明确表达长期偏好时使用；单次条件留在 TaskBrief。quote 必须是 source_message_id 所指用户消息中的原话。"""
        async with self.uow() as tx:
            messages = await tx.sessions.messages(self.run.session_id)
            source = next(
                (
                    message
                    for message in messages
                    if message.id == source_message_id and message.role == "user"
                ),
                None,
            )
        if source is None or not quote.strip() or quote not in source.content:
            raise ToolInputError("偏好必须引用真实用户消息中的原话")
        return await self.memory.save(
            Memory(
                key=key,
                content=quote,
                source_message_id=source_message_id,
                kind="preference",
                scope="global" if global_scope else "session",
                session_id=None if global_scope else self.run.session_id,
            )
        )

    async def search_memory(self, query: str) -> list[Memory]:
        """按需检索用户确认的偏好和研究笔记；记忆不是论文事实的证据。"""
        async with self.uow() as tx:
            current = await tx.runs.get(self.run.id)
        use_embedding = current.phase != "delivery" and (
            current.deadline_at is None
            or (current.deadline_at - now()).total_seconds()
            > current.execution.limits.finalization_reserve_seconds
        )
        return await self.memory.search(
            query,
            self.run.session_id,
            use_embedding=use_embedding,
            timeout_seconds=max(
                0,
                (current.deadline_at - now()).total_seconds()
                - current.execution.limits.finalization_reserve_seconds,
            )
            if current.deadline_at and use_embedding
            else None,
        )

    async def read_memory(self, memory_id: str) -> dict:
        """读取有效记忆并将原始证据关联到当前会话，保留原会话来源，复用 Artifact。"""
        return await self.memory.read(self.run, memory_id)
