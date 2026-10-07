from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Annotated

from pydantic import Field

from paper_search.documents.service import DocumentService
from paper_search.domain import (
    Run,
    ToolInputError,
    ToolResult,
    now,
)
from paper_search.intent import require_research
from paper_search.ports import UowFactory
from paper_search.retrieval.identity import normalize_title


class DocumentTools:
    def __init__(self, run: Run, uow: UowFactory, documents: DocumentService):
        self.run, self.uow, self.documents = run, uow, documents

    async def _previous_document_progress(self) -> tuple[set[str], set[str]]:
        async with self.uow() as tx:
            executions = [
                item
                for item in await tx.runs.tools(self.run.id)
                if item.call.name in {"read_document", "search_documents"}
                and item.result
                and isinstance(item.result.data, dict)
            ]
        executions.sort(key=lambda item: item.queued_at)
        block_ids = {
            str(block_id)
            for item in executions
            for block_id in item.result.data.get("block_ids", [])
        }
        evidence_ids = {
            str(evidence_id)
            for item in executions
            for evidence_id in item.result.evidence_ids
        }
        return block_ids, evidence_ids

    async def download_pdf(self, paper_id: str, url: str | None = None) -> dict:
        """下载论文全文并创建后台解析任务。返回文档 ID；调用 wait_for_documents 等待自动接续，完成后 read_document 读取。"""
        await require_research(
            self.uow, self.run, unavailable_message="请先确定研究问题"
        )
        async with self.uow() as tx:
            current = await tx.runs.get(self.run.id)
            paper = await tx.papers.get(paper_id)
        source = url or (str(paper.pdf_url) if paper.pdf_url else None)
        if not source:
            raise ToolInputError(
                "没有可访问的 PDF 地址，请通过网页工具查找开放全文或由用户上传"
            )
        document = await self.documents.download(
            self.run.session_id,
            source,
            paper.title + ".pdf",
            paper.canonical_id,
            owner_run_id=self.run.id,
            basis_revision=current.brief.basis_revision,
            research_deadline_at=current.deadline_at
            - timedelta(seconds=current.execution.limits.finalization_reserve_seconds)
            if current.deadline_at
            else None,
        )
        return document.model_dump(mode="json", exclude={"parsed"})

    async def bind_document_paper(
        self,
        document_id: str,
        paper_id: str,
        evidence_ids: Annotated[list[str], Field(min_length=1, max_length=12)],
        input_revision: int,
    ) -> dict:
        """用文档原文中的 DOI/arXiv 标识或标题加作者证据，将上传 PDF 绑定到已核验论文；文件名不参与判断。"""
        if self.run.parent_id:
            raise ToolInputError("Worker 不能绑定会话文档")
        async with self.uow() as tx:
            current_run = await tx.runs.get(self.run.id)
            if input_revision != current_run.input_revision:
                raise ToolInputError(
                    f"输入已变化，请基于 input_revision={current_run.input_revision} 绑定"
                )
            document = await tx.documents.get(document_id)
            if document.session_id != self.run.session_id:
                raise ToolInputError("文档不属于当前会话")
            paper = await tx.papers.get(paper_id)
            if not any(
                item.identity_status == "verified" for item in paper.source_records
            ):
                raise ToolInputError("目标论文身份尚未由原始来源核验")
            if document.paper_id and document.paper_id != paper.canonical_id:
                raise ToolInputError(f"文档已绑定到 {document.paper_id}，不能静默覆盖")
            evidence = [
                await tx.papers.evidence(key) for key in dict.fromkeys(evidence_ids)
            ]
            if any(
                item.session_id != self.run.session_id
                or item.document_id != document.id
                for item in evidence
            ):
                raise ToolInputError("绑定证据必须来自当前会话的目标文档")
            source_text = "\n".join(item.excerpt for item in evidence)
            lower = source_text.casefold()
            exact_identifier = bool(
                (paper.doi and paper.doi.casefold() in lower)
                or (
                    paper.arxiv_id
                    and re.sub(r"v\d+$", "", paper.arxiv_id.casefold()) in lower
                )
            )
            title_match = normalize_title(paper.title) in normalize_title(source_text)
            author_match = any(
                normalize_title(author) in normalize_title(source_text)
                for author in paper.authors
                if len(normalize_title(author)) >= 4
            )
            if not exact_identifier and not (title_match and author_match):
                raise ToolInputError(
                    "证据未包含精确 DOI/arXiv 标识，也未同时确认规范化标题和作者"
                )
            if document.paper_id is None:
                document = document.model_copy(update={"paper_id": paper.canonical_id})
                await tx.documents.save(document)
                await tx.sessions.append_event(
                    self.run.session_id,
                    self.run.id,
                    "document.updated",
                    {"document": document.model_dump(mode="json", exclude={"parsed"})},
                )
        return document.model_dump(mode="json", exclude={"parsed"})

    async def read_document(
        self,
        document_id: str,
        page: int | None = None,
        section: str | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=30)] = 5,
    ) -> ToolResult:
        """读取全文状态及指定页/章节的原文段落，返回物理页码和完整 [evidence:ID]。引用时逐个原样复制完整 citation，不缩写 ID、不合并多个 ID；无需重复保存。默认五段，按缺口定位，避免反复加载全文。"""
        previous_blocks, previous_evidence = await self._previous_document_progress()
        async with self.uow() as tx:
            document = await tx.documents.get(document_id)
            if document.session_id != self.run.session_id:
                raise ToolInputError("文档不属于当前会话")
            if document.status not in {"ready", "failed", "cancelled"}:
                run = await tx.runs.get(self.run.id)
                waiting = tuple(dict.fromkeys((*run.waiting_document_ids, document.id)))
                await tx.runs.save(
                    run.model_copy(update={"waiting_document_ids": waiting})
                )
        blocks = (
            [
                block
                for block in document.parsed.blocks
                if (page is None or block.page == page)
                and (section is None or section.casefold() in block.section.casefold())
            ]
            if document.parsed
            else []
        )
        selected = blocks[offset : offset + limit]
        evidence = (
            [
                await self.documents.evidence(
                    self.run.session_id, document_id, block.id, run_id=self.run.id
                )
                for block in selected
            ]
            if document.status == "ready"
            else []
        )
        citations = {item.block_id: f"[evidence:{item.id}]" for item in evidence}
        block_ids = [block.id for block in selected]
        evidence_ids = [item.id for item in evidence]
        new_block_ids = [key for key in block_ids if key not in previous_blocks]
        new_evidence_ids = [key for key in evidence_ids if key not in previous_evidence]
        data = {
            "document": document.model_dump(mode="json", exclude={"parsed"}),
            "total_blocks": len(blocks),
            "blocks": [
                {**block.model_dump(mode="json"), "citation": citations.get(block.id)}
                for block in selected
            ],
            "block_ids": block_ids,
            "new_block_ids": new_block_ids,
            "reused_block_ids": [key for key in block_ids if key in previous_blocks],
            "new_evidence_ids": new_evidence_ids,
            "reused_evidence_ids": [
                key for key in evidence_ids if key in previous_evidence
            ],
        }
        content = {
            "document_id": document.id,
            "paper_id": document.paper_id,
            "name": document.name,
            "status": document.status,
            "error": document.error,
            "total_blocks": len(blocks),
            "next_offset": offset + limit if offset + limit < len(blocks) else None,
            "blocks": [
                {
                    **block.model_dump(include={"id", "page", "section", "text"}),
                    "citation": citations.get(block.id),
                }
                for block in selected
            ],
            "progress": {
                "new_block_ids": new_block_ids,
                "reused_block_ids": [
                    key for key in block_ids if key in previous_blocks
                ],
                "new_evidence_ids": new_evidence_ids,
                "reused_evidence_ids": [
                    key for key in evidence_ids if key in previous_evidence
                ],
            },
        }
        return ToolResult(
            content=json.dumps(content, ensure_ascii=False),
            data=data,
            evidence_ids=tuple(item.id for item in evidence),
        )

    async def wait_for_documents(
        self, document_ids: Annotated[list[str], Field(min_length=1, max_length=8)]
    ) -> dict:
        """等待后台 PDF 任务完成，等待计入研究期限；Harness 自动等待，无需循环查询状态。"""
        async with self.uow() as tx:
            documents = [
                await tx.documents.get(key) for key in dict.fromkeys(document_ids)
            ]
            if any(item.session_id != self.run.session_id for item in documents):
                raise ToolInputError("文档不属于当前会话")
            run = await tx.runs.get(self.run.id)
            pending = tuple(
                item.id
                for item in documents
                if item.status not in {"ready", "failed", "cancelled"}
            )
            await tx.runs.save(run.model_copy(update={"waiting_document_ids": pending}))
        return {
            "waiting_for": pending,
            "documents": [
                {"id": item.id, "status": item.status, "error": item.error}
                for item in documents
            ],
        }

    async def search_documents(
        self,
        query: str,
        document_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> ToolResult:
        """在当前会话全文中定位原文段落，返回物理页码和完整 [evidence:ID]。引用时逐个原样复制完整 citation，不缩写 ID、不合并多个 ID；无需重复保存。默认五段，每次围绕一个待核对问题检索，避免近似查询反复搜索。"""
        previous_blocks, previous_evidence = await self._previous_document_progress()
        async with self.uow() as tx:
            current = await tx.runs.get(self.run.id)
        use_embedding = current.phase != "delivery" and (
            current.deadline_at is None
            or (current.deadline_at - now()).total_seconds()
            > current.execution.limits.finalization_reserve_seconds
        )
        hits = await self.documents.search(
            self.run.session_id,
            query,
            limit,
            document_id,
            use_embedding=use_embedding,
            timeout_seconds=max(
                0,
                (current.deadline_at - now()).total_seconds()
                - current.execution.limits.finalization_reserve_seconds,
            )
            if current.deadline_at and use_embedding
            else None,
        )
        evidence = [
            await self.documents.evidence(
                self.run.session_id,
                hit["document_id"],
                hit["block_id"],
                hit["text"],
                run_id=self.run.id,
            )
            for hit in hits
        ]
        data = [
            {**hit, "citation": f"[evidence:{item.id}]"}
            for hit, item in zip(hits, evidence, strict=True)
        ]
        block_ids = [str(hit["block_id"]) for hit in hits]
        evidence_ids = [item.id for item in evidence]
        progress = {
            "block_ids": block_ids,
            "new_block_ids": [key for key in block_ids if key not in previous_blocks],
            "reused_block_ids": [key for key in block_ids if key in previous_blocks],
            "new_evidence_ids": [
                key for key in evidence_ids if key not in previous_evidence
            ],
            "reused_evidence_ids": [
                key for key in evidence_ids if key in previous_evidence
            ],
        }
        return ToolResult(
            content=json.dumps(
                {"hits": data, "progress": progress}, ensure_ascii=False
            ),
            data={"hits": data, **progress},
            evidence_ids=tuple(item.id for item in evidence),
        )
