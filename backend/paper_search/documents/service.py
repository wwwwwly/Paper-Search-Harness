from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

from paper_search.domain import (
    Conflict,
    Document,
    Evidence,
    IndexRecord,
    Job,
    MissingRecord,
    RunStatus,
    ToolInputError,
    now,
)
from paper_search.ports import (
    ArtifactStore,
    DocumentDownloader,
    DocumentParser,
    EmbeddingClient,
    SearchIndex,
    UowFactory,
)
from paper_search.run_lifecycle import wake_document_runs


class DocumentService:
    def __init__(
        self,
        *,
        uow: UowFactory,
        artifacts: ArtifactStore,
        parser: DocumentParser,
        index: SearchIndex,
        downloader: DocumentDownloader,
        embedding: EmbeddingClient | None,
        max_bytes: int = 50 * 1024 * 1024,
    ):
        self.uow, self.artifacts, self.parser, self.index = (
            uow,
            artifacts,
            parser,
            index,
        )
        self.interrupt_job = lambda _key: None
        self.downloader, self.embedding, self.max_bytes = (
            downloader,
            embedding,
            max_bytes,
        )

    async def upload(
        self, session_id: str, name: str, data: bytes, paper_id: str | None = None
    ) -> Document:
        self._validate_pdf(data)
        digest = hashlib.sha256(data).hexdigest()
        async with self.uow() as tx:
            await tx.sessions.get(session_id)
            if paper_id:
                await tx.papers.get(paper_id)
            duplicate = next(
                (
                    item
                    for item in await tx.documents.list(session_id)
                    if item.sha256 == digest and item.status != "failed"
                ),
                None,
            )
        if duplicate:
            return duplicate
        file_id = await self.artifacts.put(data)
        document = Document(
            session_id=session_id,
            paper_id=paper_id,
            name=name,
            file_id=file_id,
            sha256=digest,
        )
        await self._enqueue(document)
        return document

    async def download(
        self,
        session_id: str,
        url: str,
        name: str,
        paper_id: str | None = None,
        *,
        owner_run_id: str | None = None,
        research_deadline_at=None,
        basis_revision=None,
    ) -> Document:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ToolInputError("PDF URL 必须是无凭据的 HTTP(S) 地址")
        async with self.uow() as tx:
            await tx.sessions.get(session_id)
            if paper_id:
                await tx.papers.get(paper_id)
            previous = next(
                (
                    item
                    for item in await tx.documents.list(session_id)
                    if item.paper_id == paper_id and item.source_url == url
                ),
                None,
            )
        if previous and previous.status == "ready":
            return previous
        if previous and previous.status not in {"failed", "cancelled"}:
            return previous
        document = (
            previous.model_copy(
                update={
                    "status": "pending",
                    "error": None,
                    "owner_run_id": owner_run_id,
                    "research_deadline_at": research_deadline_at,
                    "basis_revision": basis_revision,
                }
            )
            if previous
            else Document(
                session_id=session_id,
                paper_id=paper_id,
                name=name,
                source_url=url,
                owner_run_id=owner_run_id,
                research_deadline_at=research_deadline_at,
                basis_revision=basis_revision,
            )
        )
        await self._enqueue(document)
        return document

    async def _enqueue(self, document: Document):
        async with self.uow() as tx:
            await tx.documents.save(document)
            await tx.jobs.enqueue(
                Job(
                    id=f"document-{document.id}",
                    kind="document",
                    session_id=document.session_id,
                    target_id=document.id,
                )
            )
            await tx.sessions.append_event(
                document.session_id,
                None,
                "document.updated",
                {"document": document.model_dump(mode="json", exclude={"parsed"})},
            )

    async def _save(self, document: Document):
        async with self.uow() as tx:
            if (await tx.documents.get(document.id)).status == "cancelled":
                raise asyncio.CancelledError
            await tx.documents.save(document)
            if document.status in {"ready", "failed", "cancelled"}:
                await wake_document_runs(tx, document.session_id)
            await tx.sessions.append_event(
                document.session_id,
                None,
                "document.updated",
                {"document": document.model_dump(mode="json", exclude={"parsed"})},
            )

    async def cancel(self, document_id: str) -> Document:
        async with self.uow() as tx:
            document = await tx.documents.get(document_id)
            if document.status == "ready":
                raise Conflict("已完成文档无需取消")
            document = document.model_copy(update={"status": "cancelled"})
            await tx.documents.save(document)
            job = await tx.jobs.get(f"document-{document.id}")
            await tx.jobs.save(job.model_copy(update={"status": "cancelled"}))
            await wake_document_runs(tx, document.session_id)
            await tx.sessions.append_event(
                document.session_id,
                None,
                "document.updated",
                {"document": document.model_dump(mode="json", exclude={"parsed"})},
            )
        return document

    async def retry(self, document_id: str) -> Document:
        async with self.uow() as tx:
            document = await tx.documents.get(document_id)
            if document.owner_run_id:
                owner = await tx.runs.get(document.owner_run_id)
                root = await tx.runs.get(owner.parent_id or owner.id)
                session = await tx.sessions.get(document.session_id)
                if (
                    root.status not in {RunStatus.QUEUED, RunStatus.RUNNING}
                    or session.brief.input_revision != session.input_revision
                    or document.basis_revision != session.brief.basis_revision
                ):
                    raise Conflict("先处理当前研究意图，再续接相关资料")
                document = document.model_copy(
                    update={
                        "research_deadline_at": root.deadline_at
                        - timedelta(
                            seconds=root.execution.limits.finalization_reserve_seconds
                        )
                        if root.deadline_at
                        else None,
                    }
                )
        if document.status not in {"failed", "cancelled"}:
            raise Conflict("只有失败或取消的文档可以重新处理")
        document = document.model_copy(update={"status": "pending", "error": None})
        await self._enqueue(document)
        return document

    def _validate_pdf(self, data: bytes):
        if len(data) > self.max_bytes:
            raise ValueError("PDF 超过配置的大小限制")
        if not data[:1024].lstrip().startswith(b"%PDF-"):
            raise ValueError("文件不是 PDF")

    async def cancel_owned(self, root_id):
        async with self.uow() as tx:
            root = await tx.runs.get(root_id)
            owned = {
                root.id,
                *(
                    item.id
                    for item in await tx.runs.list(root.session_id)
                    if item.parent_id == root.id
                ),
            }
            targets = [
                item
                for item in await tx.documents.list(root.session_id)
                if item.owner_run_id in owned
                and item.status not in {"ready", "failed", "cancelled"}
            ]
        for item in targets:
            try:
                await self.cancel(item.id)
                self.interrupt_job(item.id)
            except Conflict:
                pass  # The parser completed between the ownership snapshot and cancellation.

    async def _check_owner(self, document):
        if not document.owner_run_id:
            return
        while True:
            async with self.uow() as tx:
                owner = await tx.runs.get(document.owner_run_id)
                root = await tx.runs.get(owner.parent_id or owner.id)
                session = await tx.sessions.get(document.session_id)
            if (
                root.status
                in {
                    RunStatus.CANCELLED,
                    RunStatus.COMPLETED,
                    RunStatus.INTERRUPTED,
                    RunStatus.WAITING,
                    RunStatus.FAILED,
                }
                or owner.status == RunStatus.CANCELLED
            ):
                raise ToolInputError("研究任务已结束或暂停，保留文档步骤")
            if session.brief.input_revision != session.input_revision:
                await asyncio.sleep(0.2)
                continue
            if (
                document.basis_revision is not None
                and document.basis_revision != session.brief.basis_revision
            ):
                raise ToolInputError("文档任务研究依据已过期，保留原始资料")
            return

    async def process(self, document_id: str):
        async with self.uow() as tx:
            document = await tx.documents.get(document_id)
        timeout = (
            max(0, (document.research_deadline_at - now()).total_seconds())
            if document.research_deadline_at
            else None
        )
        try:
            async with asyncio.timeout(timeout):
                await self._process(document_id)
        except TimeoutError:
            async with self.uow() as tx:
                current = await tx.documents.get(document_id)
            await self._save(
                current.model_copy(
                    update={
                        "status": "failed",
                        "error": "研究期限已到，保留已有文件与解析步骤",
                    }
                )
            )

    async def _process(self, document_id: str):
        async with self.uow() as tx:
            document = await tx.documents.get(document_id)
        if document.status in {"ready", "cancelled"}:
            return
        try:
            await self._check_owner(document)
            if not document.file_id:
                document = document.model_copy(
                    update={"status": "downloading", "error": None}
                )
                await self._save(document)
                data = await self.downloader.fetch(document.source_url, self.max_bytes)
                self._validate_pdf(data)
                file_id = await self.artifacts.put(data)
                document = document.model_copy(
                    update={
                        "file_id": file_id,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            await self._check_owner(document)
            if document.parsed is None:
                document = document.model_copy(
                    update={"status": "parsing", "error": None}
                )
                await self._save(document)
                parsed = await self.parser.parse(
                    self.artifacts.local_path(document.file_id)
                )
                document = document.model_copy(update={"parsed": parsed})
            document = document.model_copy(update={"status": "indexing"})
            await self._save(document)
            blocks = list(document.parsed.blocks)
            for start in range(document.indexed_blocks, len(blocks), 64):
                await self._check_owner(document)
                batch = blocks[start : start + 64]
                if self.embedding:
                    # Four bounded requests, retaining the provider-safe batch size.
                    # Await every result so failure/cancellation cannot leak paid work.
                    groups = await asyncio.gather(
                        *(
                            self.embedding.embed(
                                [block.text for block in batch[i : i + 16]]
                            )
                            for i in range(0, len(batch), 16)
                        ),
                        return_exceptions=True,
                    )
                    for group in groups:
                        if isinstance(group, BaseException):
                            raise group
                    vectors = [vector for group in groups for vector in group]
                else:
                    vectors = [None] * len(batch)
                for block, vector in zip(batch, vectors, strict=True):
                    await self.index.upsert(
                        IndexRecord(
                            id=f"{document.id}:{block.id}",
                            kind="document",
                            scope=document.session_id,
                            text=block.text,
                            metadata={
                                "document_id": document.id,
                                "block_id": block.id,
                                "page": block.page,
                                "section": block.section,
                            },
                        ),
                        vector,
                    )
                document = document.model_copy(
                    update={"indexed_blocks": start + len(batch)}
                )
                await self._save(document)
            await self._save(
                document.model_copy(update={"status": "ready", "error": None})
            )
        except asyncio.CancelledError:
            # Keep the last durable phase for restart; explicit cancellation is applied by the caller.
            raise
        except Exception as exc:
            await self._save(
                document.model_copy(
                    update={
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {str(exc)[:1600]}",
                    }
                )
            )
            raise

    async def search(
        self,
        session_id: str,
        query: str,
        limit: int = 10,
        document_id: str | None = None,
        *,
        use_embedding=True,
        timeout_seconds=None,
    ) -> list[dict]:
        async with self.uow() as tx:
            target = await tx.documents.get(document_id) if document_id else None
            scope = target.session_id if target else session_id
            ready = {
                item.id
                for item in await tx.documents.list(scope)
                if item.status == "ready"
            }
        if not ready:
            return []
        async with asyncio.timeout(timeout_seconds):
            vector = (
                (await self.embedding.embed([query]))[0]
                if self.embedding and use_embedding
                else None
            )
        hits = await self.index.search(
            query,
            kind="document",
            scopes=(scope,),
            vector=vector,
            limit=limit,
            document_id=document_id,
        )
        return [
            {**hit.record.metadata, "text": hit.record.text, "score": hit.score}
            for hit in hits
            if hit.record.metadata["document_id"] in ready
            and (
                document_id is None or hit.record.metadata["document_id"] == document_id
            )
        ][:limit]

    async def evidence(
        self,
        session_id: str,
        document_id: str,
        block_id: str,
        excerpt: str | None = None,
        *,
        run_id: str | None = None,
    ) -> Evidence:
        async with self.uow() as tx:
            document = await tx.documents.get(document_id)
            await tx.sessions.get(session_id)
            if document.status != "ready" or not document.parsed:
                raise MissingRecord("document is not ready for evidence extraction")
            block = next(
                (item for item in document.parsed.blocks if item.id == block_id), None
            )
            if block is None:
                raise ToolInputError("文档中没有指定的 block_id")
            excerpt = block.text if excerpt is None else excerpt
            if excerpt not in block.text or not excerpt.strip():
                raise ToolInputError(
                    "证据必须是指定原文段落中的连续片段，请使用 read_document 返回的原文，不改写空格、标点或内容。"
                )
            evidence = Evidence(
                id=uuid5(
                    NAMESPACE_URL,
                    json.dumps(
                        [
                            session_id,
                            document_id,
                            document.sha256,
                            block_id,
                            excerpt,
                        ]
                    ),
                ).hex,
                session_id=session_id,
                run_id=run_id,
                paper_id=document.paper_id,
                document_id=document.id,
                document_sha256=document.sha256,
                block_id=block_id,
                page=block.page,
                section=block.section,
                excerpt=excerpt,
                source_url=document.source_url,
                level="fulltext",
            )
            try:
                return await tx.papers.evidence(evidence.id)
            except MissingRecord:
                pass
            await tx.papers.save_evidence(evidence)
            await tx.sessions.append_event(
                session_id,
                run_id,
                "evidence.created",
                {"evidence": evidence.model_dump(mode="json")},
            )
        return evidence
