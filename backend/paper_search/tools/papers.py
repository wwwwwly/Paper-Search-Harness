from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from paper_search.domain import (
    Evidence,
    MissingRecord,
    Run,
    SearchQuery,
    ToolInputError,
    ToolResult,
)
from paper_search.intent import require_research
from paper_search.ports import AgentCallbacks, UowFactory
from paper_search.retrieval.catalog import PaperCatalog
from paper_search.retrieval.engine import RetrievalService


class PaperTools:
    def __init__(
        self,
        run: Run,
        uow: UowFactory,
        retrieval: RetrievalService,
        catalog: PaperCatalog,
        callbacks: AgentCallbacks,
        crossref,
    ):
        self.crossref = crossref
        self.run, self.uow, self.retrieval, self.catalog, self.callbacks = (
            run,
            uow,
            retrieval,
            catalog,
            callbacks,
        )

    async def search_papers(
        self,
        queries: Annotated[list[SearchQuery], Field(max_length=4)],
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolResult:
        """召回融合候选；排名不是相关性或质量判断。question 是分面，日期是临时查询窗口；unknown 条件保留。queries=[] 读取已有候选。"""
        if queries:
            await require_research(
                self.uow, self.run, unavailable_message="请先确定研究问题"
            )
        result = await self.retrieval.search(
            self.run, queries, limit=limit, offset=offset
        )
        cards = [
            hit.paper.model_dump(mode="json", exclude={"source_records", "aliases"})
            for hit in result.hits
        ]
        return ToolResult(
            content=json.dumps(
                {
                    "papers": cards,
                    "sources": [
                        item.model_dump(mode="json") for item in result.diagnostics
                    ],
                    "new_candidates": result.new_candidates,
                    "next_offset": result.next_offset,
                },
                ensure_ascii=False,
            ),
            data=result.model_dump(mode="json"),
        )

    async def get_paper(self, paper_id: str) -> ToolResult:
        """从本地库读取论文元数据、完整摘要与引用，不联网。resolve_papers 已返回完整摘要时无需再读；paper_id 使用已发现的标识。新论文先网页搜索，再用 resolve_papers 读取链接。"""
        async with self.uow() as tx:
            try:
                paper = await tx.papers.get(paper_id)
            except MissingRecord:
                raise ToolInputError(
                    f"本地库未找到 {paper_id}。get_paper 不联网查询；请先用 "
                    "网页搜索找到论文链接，再用 resolve_papers 读取该链接。"
                ) from None
            record = await tx.research.get(self.run.session_id)
            await tx.research.save(
                record.model_copy(
                    update={
                        "candidates": tuple(
                            item.model_copy(
                                update={
                                    "read": True,
                                    "run_ids": tuple(
                                        dict.fromkeys((*item.run_ids, self.run.id))
                                    ),
                                }
                            )
                            if item.paper_id == paper.canonical_id
                            else item
                            for item in record.candidates
                        )
                    }
                )
            )
            documents = [
                item.model_dump(mode="json", exclude={"parsed"})
                for item in await tx.documents.list()
                if item.paper_id in {paper.canonical_id, *paper.aliases}
            ]
            evidence = None
            if paper.abstract and paper.abstract.strip():
                evidence = next(
                    (
                        item
                        for item in await tx.papers.list_evidence(self.run.session_id)
                        if item.paper_id == paper.canonical_id
                        and item.level == "abstract"
                        and " ".join(item.excerpt.split())
                        == " ".join(paper.abstract.split())
                    ),
                    None,
                )
                if evidence is None:
                    evidence = Evidence(
                        session_id=self.run.session_id,
                        run_id=self.run.id,
                        paper_id=paper.canonical_id,
                        excerpt=paper.abstract,
                        source_url=str(paper.primary_url)
                        if paper.primary_url
                        else None,
                        level="abstract",
                        section="Abstract",
                    )
                    await tx.papers.save_evidence(evidence)
                    await tx.sessions.append_event(
                        self.run.session_id,
                        self.run.id,
                        "evidence.created",
                        {"evidence": evidence.model_dump(mode="json")},
                    )
            metadata = json.dumps(
                paper.model_dump(mode="json", exclude={"abstract"}), ensure_ascii=False
            )
            metadata_evidence = next(
                (
                    item
                    for item in await tx.papers.list_evidence(self.run.session_id)
                    if item.paper_id == paper.canonical_id
                    and item.level == "metadata"
                    and item.excerpt == metadata
                ),
                None,
            )
            if metadata_evidence is None:
                metadata_evidence = Evidence(
                    session_id=self.run.session_id,
                    run_id=self.run.id,
                    paper_id=paper.canonical_id,
                    excerpt=metadata,
                    source_url=str(paper.primary_url) if paper.primary_url else None,
                    level="metadata",
                    section="Metadata",
                )
                await tx.papers.save_evidence(metadata_evidence)
                await tx.sessions.append_event(
                    self.run.session_id,
                    self.run.id,
                    "evidence.created",
                    {"evidence": metadata_evidence.model_dump(mode="json")},
                )
        data = {"paper": paper.model_dump(mode="json"), "documents": documents}
        content = json.dumps(
            {
                "paper": paper.model_dump(
                    mode="json",
                    exclude_none=True,
                    exclude_defaults=True,
                    exclude={"abstract", "source_records", "aliases", "source_urls"},
                ),
                "documents": documents,
            },
            ensure_ascii=False,
        )
        content += f"\n元数据依据：[evidence:{metadata_evidence.id}]"
        if evidence:
            content += f"\n摘要原文（已保存，可直接引用）：[evidence:{evidence.id}]\n{evidence.excerpt}"
        return ToolResult(
            content=content,
            data=data,
            evidence_ids=tuple(
                item.id for item in (evidence, metadata_evidence) if item
            ),
        )

    async def resolve_papers(
        self, identifiers: Annotated[list[str], Field(min_length=1, max_length=8)]
    ) -> ToolResult:
        """批量读取已找到的论文 URL、DOI 或 arXiv 编号，保存论文及原文引用；直接访问网页，不要求学术 API。只有标题时先网页搜索。网页/PDF 已有充分材料时无需重复调用。缺少作者、期刊等字段仍可继续，完整摘要无需再 get_paper。"""
        await require_research(
            self.uow,
            self.run,
            unavailable_message="本轮 research_questions 为空，不开放论文检索工具",
        )
        results = await self.catalog.resolve(self.run, identifiers)
        entries = []
        evidence_ids = []
        for result in results:
            if result.paper:
                loaded = await self.get_paper(result.paper.canonical_id)
                evidence_ids.extend(loaded.evidence_ids)
                paper = result.paper
                metadata = {
                    "canonical_id": paper.canonical_id,
                    "title": paper.title,
                    "authors": paper.authors[:12],
                    "year": paper.year,
                    "publication_date": paper.publication_date,
                    "updated_date": paper.updated_date,
                    "affiliations": [
                        item.model_dump(mode="json") for item in paper.affiliations[:8]
                    ],
                    "venue": paper.venue,
                    "code_urls": paper.code_urls,
                    "landing_page_url": paper.landing_page_url,
                    "pdf_url": paper.pdf_url,
                    "is_open_access": paper.is_open_access,
                    "provider": paper.provider,
                    "arxiv_id": paper.arxiv_id,
                }
                header = f"{result.identifier}: resolved\n" + json.dumps(
                    {
                        "paper": metadata,
                        "documents": loaded.data["documents"],
                    },
                    ensure_ascii=False,
                    default=str,
                )
                citation = (
                    f"[evidence:{loaded.evidence_ids[0]}]"
                    if loaded.evidence_ids
                    else ""
                )
                entries.append((header, paper.abstract or "", citation))
            else:
                lines = [
                    f"{result.identifier}: {result.status}",
                    result.reason or "尚未精确匹配，以下候选不是身份确认。",
                    *(
                        f"候选 [{p.canonical_id}] {p.title} {p.primary_url or ''}"
                        for p in result.candidates[:3]
                    ),
                ]
                if len(result.candidates) > 3:
                    lines.append(
                        f"另有 {len(result.candidates) - 3} 个候选保存在完整结果中。"
                    )
                entries.append(("\n".join(lines), "", ""))

        base_size = sum(
            len(header)
            + (
                len("\n摘要原文（已保存，可直接引用）：\n") + len(citation) + 45
                if abstract
                else 0
            )
            for header, abstract, citation in entries
        )
        remaining = max(0, 7000 - base_size)
        abstracts_left = sum(bool(abstract) for _, abstract, _ in entries)
        lines = []
        for header, abstract, citation in entries:
            lines.append(header)
            if not abstract:
                continue
            allowance = min(1800, remaining // max(1, abstracts_left))
            preview = abstract[:allowance]
            remaining -= len(preview)
            abstracts_left -= 1
            lines.append(
                f"摘要原文（已保存，可直接引用）：{citation}\n{preview}"
                + (
                    "\n（批量结果仅展示摘要节选；按需用 get_paper 读取该篇完整摘要。）"
                    if len(preview) < len(abstract)
                    else ""
                )
            )
        return ToolResult(
            content="\n".join(lines),
            data=[result.model_dump(mode="json") for result in results],
            evidence_ids=tuple(evidence_ids),
            sources=tuple(result.source for result in results if result.source),
        )

    async def enrich_papers(
        self, paper_ids: Annotated[list[str], Field(min_length=1, max_length=8)]
    ) -> ToolResult:
        """仅为 shortlist 中已有 DOI 的论文按需补 Crossref 出版元数据，不逐项补全全部候选。"""
        await require_research(
            self.uow, self.run, unavailable_message="请先确定研究问题"
        )
        async with self.uow() as tx:
            papers = [await tx.papers.get(key) for key in paper_ids]
        results = []
        for paper in papers:
            if paper.doi:
                result = await self.crossref.resolve(paper.doi)
                if result.paper:
                    from paper_search.retrieval.catalog import save_paper

                    async with self.uow() as tx:
                        await save_paper(tx, result.paper)
                results.append(result)
        return ToolResult(
            content=json.dumps(
                [item.model_dump(mode="json", exclude={"source"}) for item in results],
                ensure_ascii=False,
            ),
            sources=tuple(item.source for item in results if item.source),
        )
