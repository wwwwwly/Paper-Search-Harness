from __future__ import annotations

import asyncio
import json

from paper_search.domain import Conflict, IndexRecord, Job, Memory, now
from paper_search.ports import EmbeddingClient, SearchIndex, UowFactory


def _index_job(memory: Memory) -> Job:
    return Job(
        id=f"memory-{memory.id}-{memory.revision}",
        kind="memory",
        session_id=memory.session_id or "global",
        target_id=memory.id,
    )


class MemoryService:
    def __init__(
        self, uow: UowFactory, index: SearchIndex, embedding: EmbeddingClient | None
    ):
        self.uow, self.index, self.embedding = uow, index, embedding
        self.locks: dict[str, asyncio.Lock] = {}
        self.recall_cache = {}
        self.recall_views = {}

    async def save(self, memory: Memory, *, restore: bool = False) -> Memory:
        async with self.uow() as tx:
            previous = next(
                (
                    item
                    for item in await tx.memories.list(include_deleted=True)
                    if item.key == memory.key
                    and item.scope == memory.scope
                    and item.session_id == memory.session_id
                ),
                None,
            )
            if previous and previous.deleted and not restore:
                raise Conflict("该记忆已被删除，只能由用户明确恢复")
            if previous:
                memory = memory.model_copy(
                    update={
                        "id": previous.id,
                        "revision": previous.revision + 1,
                        "updated_at": now(),
                    }
                )
            await tx.memories.save(memory)
            await tx.jobs.enqueue(_index_job(memory))
            if memory.session_id:
                await tx.sessions.append_event(
                    memory.session_id,
                    None,
                    "memory.updated",
                    {"memory": memory.model_dump(mode="json")},
                )
        self.recall_cache.clear()
        return memory

    async def process(self, memory_id: str):
        async with self.uow() as tx:
            memory = await tx.memories.get(memory_id)
        if memory.deleted:
            async with (
                self.locks.setdefault(memory.id, asyncio.Lock()),
                self.uow() as tx,
            ):
                current = await tx.memories.get(memory.id)
                if current.deleted and current.revision == memory.revision:
                    await self.index.delete(memory.id)
            return
        vector = (
            (await self.embedding.embed([memory.content]))[0]
            if self.embedding
            else None
        )
        async with self.locks.setdefault(memory_id, asyncio.Lock()), self.uow() as tx:
            latest = await tx.memories.get(memory_id)
            if latest.deleted:
                await self.index.delete(memory_id)
                return
            if latest.revision != memory.revision:
                return
            await self.index.upsert(
                IndexRecord(
                    id=memory.id,
                    kind="memory",
                    scope="global" if memory.scope == "global" else memory.session_id,
                    text=memory.content,
                    metadata={"revision": memory.revision},
                ),
                vector,
            )

    async def reindex(self, memory_id: str):
        async with self.uow() as tx:
            memory = await tx.memories.get(memory_id)
            await tx.jobs.enqueue(_index_job(memory))

    async def delete(self, memory_id: str):
        async with self.uow() as tx:
            memory = await tx.memories.get(memory_id)
            memory = memory.model_copy(
                update={
                    "deleted": True,
                    "revision": memory.revision + 1,
                    "updated_at": now(),
                }
            )
            await tx.memories.save(memory)
            await tx.jobs.enqueue(_index_job(memory))
        self.recall_cache.clear()
        async with self.locks.setdefault(memory_id, asyncio.Lock()), self.uow() as tx:
            current = await tx.memories.get(memory_id)
            if current.deleted and current.revision == memory.revision:
                await self.index.delete(memory_id)

    async def search(
        self,
        query: str,
        session_id: str,
        limit: int = 8,
        *,
        use_embedding=True,
        timeout_seconds=None,
    ) -> list[Memory]:
        async with self.uow() as tx:
            visible = {item.id: item for item in await tx.memories.list(session_id)}
        if not visible:
            return []
        async with asyncio.timeout(timeout_seconds):
            vector = (
                (await self.embedding.embed([query]))[0]
                if self.embedding and use_embedding
                else None
            )
        hits = await self.index.search(
            query,
            kind="memory",
            scopes=("global", session_id),
            vector=vector,
            limit=limit,
        )
        async with self.uow() as tx:
            visible = {item.id: item for item in await tx.memories.list(session_id)}
        return [
            visible[hit.record.id]
            for hit in hits
            if hit.record.id in visible
            and hit.record.metadata.get("revision") == visible[hit.record.id].revision
        ]

    async def _save_observed(self, memory):
        async with self.uow() as tx:
            previous = next(
                (
                    item
                    for item in await tx.memories.list(include_deleted=True)
                    if item.key == memory.key
                    and item.session_id == memory.session_id
                    and item.scope == memory.scope
                ),
                None,
            )
        if previous and previous.deleted:
            return None
        if (
            previous
            and previous.content == memory.content
            and set(previous.evidence_ids) == set(memory.evidence_ids)
        ):
            return previous
        try:
            return await self.save(memory)
        except Conflict:
            return None  # Automatic accumulation must respect an explicit deletion.

    async def capture_result(self, run, result):
        """Save typed research and observed execution, without another model call."""
        from paper_search.research import referenced_evidence

        evidence_ids = tuple(
            dict.fromkeys(
                key
                for item in result.selected
                for key in referenced_evidence(item.assessment)
            )
        )
        async with self.uow() as tx:
            brief = (await tx.sessions.get(run.session_id)).brief
        await self._save_observed(
            Memory(
                key=f"research:{run.id}:{result.basis_revision}",
                kind="research",
                scope="global",
                session_id=run.session_id,
                content=json.dumps(
                    result.model_dump(mode="json", exclude={"markdown"}),
                    ensure_ascii=False,
                ),
                evidence_ids=evidence_ids,
                paper_ids=tuple(item.paper_id for item in result.selected),
                source_run_id=run.id,
                support="observed",
                outcome=result.status,
                applicability=(result.mode, brief.objective),
            )
        )
        async with self.uow() as tx:
            tools = [
                item
                for item in await tx.runs.tools(run.id)
                if item.input_revision == result.input_revision
            ]
            board = await tx.research.get(run.session_id)
            current = await tx.runs.get(run.id)
            child_tools = [
                tool
                for task in board.tasks
                if task.root_run_id == run.id
                and task.basis_revision == result.basis_revision
                for tool in await tx.runs.tools(task.id)
            ]
        observations = [
            {
                "run_id": item.run_id,
                "call_id": item.call.id,
                "tool": item.call.name,
                "state": item.state,
                "error": item.result.error_code
                if item.result and item.result.is_error
                else None,
            }
            for item in (*tools, *child_tools)
        ]
        if observations:
            await self._save_observed(
                Memory(
                    key=f"experience:{run.id}:{result.basis_revision}",
                    kind="experience",
                    scope="global",
                    session_id=run.session_id,
                    source_run_id=run.id,
                    support="observed",
                    applicability=(result.mode, brief.objective),
                    outcome=result.status,
                    content=json.dumps(
                        {
                            "context": brief.objective,
                            "strategy": [
                                item.call.arguments
                                for item in tools
                                if item.call.name in {"search_papers", "resolve_papers"}
                            ],
                            "observations": observations,
                            "tasks": [
                                task.model_dump(mode="json")
                                for task in board.tasks
                                if task.root_run_id == run.id
                                and task.basis_revision == result.basis_revision
                            ],
                            "skills": [
                                {"name": skill.name, "version": skill.version}
                                for skill in current.skills
                            ],
                            "selected": [item.paper_id for item in result.selected],
                            "gaps": result.gaps,
                            "limits": "单次运行的可查执行记录；成功提交不证明策略的统计收益，失败也不证明其他场景不适用",
                        },
                        ensure_ascii=False,
                    ),
                )
            )

    async def recall(
        self,
        brief,
        session_id,
        *,
        run_id=None,
        use_embedding=True,
        timeout_seconds=None,
    ):
        query = " ".join(
            (
                brief.research_mode,
                brief.objective,
                *brief.focus,
                *brief.open_questions,
                *(seed.original for seed in brief.seeds),
            )
        )
        cache_key = (session_id, brief.revision, query, use_embedding)
        if cache_key not in self.recall_cache:
            memories = (
                [
                    item
                    for item in await self.search(
                        query,
                        session_id,
                        limit=12,
                        use_embedding=use_embedding,
                        timeout_seconds=timeout_seconds,
                    )
                    if item.kind in {"research", "experience"}
                ][:4]
                if query.strip()
                else []
            )
            self.recall_cache[cache_key] = tuple(item.id for item in memories)
            if len(self.recall_cache) > 128:
                del self.recall_cache[next(iter(self.recall_cache))]
        async with self.uow() as tx:
            memories = [
                await tx.memories.get(key) for key in self.recall_cache[cache_key]
            ]
        memories = [item for item in memories if not item.deleted]
        from paper_search.harness.limits import estimate_tokens

        remaining, result = 2000, []
        for item in memories:
            content = item.content
            if estimate_tokens(content) > remaining:
                left, right = 0, len(content)
                while left < right:
                    mid = (left + right + 1) // 2
                    if estimate_tokens(content[:mid]) <= remaining:
                        left = mid
                    else:
                        right = mid - 1
                content = content[:left]
            remaining -= estimate_tokens(content)
            result.append(
                {
                    "id": item.id,
                    "kind": item.kind,
                    "revision": item.revision,
                    "content": content,
                    "source_run_id": item.source_run_id,
                    "evidence_ids": item.evidence_ids,
                    "support": item.support,
                    "applicability": item.applicability,
                }
            )
            if remaining <= 0:
                break
        if run_id:
            revisions = {item["id"]: item["revision"] for item in result}
            if self.recall_views.get(run_id) != revisions:
                self.recall_views[run_id] = revisions
                async with self.uow() as tx:
                    await tx.sessions.append_event(
                        session_id,
                        run_id,
                        "memory.recalled",
                        {
                            "query": query,
                            "revisions": revisions,
                        },
                    )
        return result

    async def read(self, run, memory_id):
        from paper_search.domain import ToolInputError

        reused_documents = {}
        async with self.uow() as tx:
            item = await tx.memories.get(memory_id)
            if item.deleted or (
                item.scope != "global" and item.session_id != run.session_id
            ):
                raise ToolInputError("记忆不存在、已删除或不可访问")
            evidence_ids = []
            for key in item.evidence_ids:
                original = await tx.papers.evidence(key)
                if original.session_id == run.session_id:
                    evidence_ids.append(key)
                    continue
                document_id, snapshot_id = original.document_id, original.snapshot_id
                if document_id:
                    document = await tx.documents.get(document_id)
                    linked_doc = document.model_copy(
                        update={
                            "id": f"memory-{run.session_id}-{document.id}",
                            "session_id": run.session_id,
                            "owner_run_id": None,
                            "research_deadline_at": None,
                            "basis_revision": None,
                        }
                    )
                    await tx.documents.save(linked_doc)
                    document_id = linked_doc.id
                    reused_documents[linked_doc.id] = linked_doc
                    await tx.sessions.append_event(
                        run.session_id,
                        run.id,
                        "document.updated",
                        {
                            "document": linked_doc.model_dump(
                                mode="json", exclude={"parsed"}
                            )
                        },
                    )
                if snapshot_id:
                    snapshot = await tx.research.snapshot(snapshot_id)
                    artifact = await tx.artifacts.get(snapshot.artifact_id)
                    linked_artifact = artifact.model_copy(
                        update={
                            "id": f"memory-{run.session_id}-{artifact.id}",
                            "session_id": run.session_id,
                            "run_id": run.id,
                        }
                    )
                    await tx.artifacts.save(linked_artifact)
                    linked_snapshot = snapshot.model_copy(
                        update={
                            "id": f"memory-{run.session_id}-{snapshot.id}",
                            "session_id": run.session_id,
                            "run_id": run.id,
                            "artifact_id": linked_artifact.id,
                        }
                    )
                    await tx.research.save_snapshot(linked_snapshot)
                    snapshot_id = linked_snapshot.id
                linked = original.model_copy(
                    update={
                        "id": f"memory-{run.session_id}-{original.id}",
                        "session_id": run.session_id,
                        "run_id": run.id,
                        "origin_evidence_id": original.id,
                        "document_id": document_id,
                        "snapshot_id": snapshot_id,
                    }
                )
                await tx.papers.save_evidence(linked)
                evidence_ids.append(linked.id)
                await tx.sessions.append_event(
                    run.session_id,
                    run.id,
                    "evidence.created",
                    {"evidence": linked.model_dump(mode="json")},
                )
        for document in reused_documents.values():
            if document.parsed and document.status == "ready":
                for block in document.parsed.blocks:
                    await self.index.upsert(
                        IndexRecord(
                            id=f"{document.id}:{block.id}",
                            kind="document",
                            scope=run.session_id,
                            text=block.text,
                            metadata={
                                "document_id": document.id,
                                "block_id": block.id,
                                "page": block.page,
                                "section": block.section,
                            },
                        )
                    )
        return {
            "memory": item.model_dump(mode="json"),
            "current_evidence_ids": evidence_ids,
            "evidence_mapping": dict(zip(item.evidence_ids, evidence_ids)),
        }
