from __future__ import annotations

import asyncio
import json
import time

from paper_search.domain import (
    Candidate,
    Run,
    SearchConstraints,
    SearchHit,
    SearchProgress,
    SearchQuery,
    SearchResult,
    SourceDiagnostic,
    ToolInputError,
)
from paper_search.ports import SearchProvider, UowFactory
from paper_search.research import structural_constraints
from paper_search.retrieval.catalog import save_paper
from paper_search.retrieval.singleflight import SingleFlight


class RetrievalService:
    """Recall and provenance only. Business judgments belong to ResearchBoard."""

    def __init__(
        self, providers: dict[str, SearchProvider], uow: UowFactory, timeout=30
    ):
        self.providers, self.uow, self.timeout = providers, uow, timeout
        self.cache, self.cooldown = {}, {}
        self.inflight = SingleFlight()
        self.limits = {key: asyncio.Semaphore(2) for key in providers}

    async def _shared_fetch(self, root, query, constraints):
        key = (
            root,
            query.model_dump_json(exclude={"question"}),
            constraints.model_dump_json(exclude={"excluded_ids"}),
        )
        return await self.inflight.run(key, lambda: self._fetch(query, constraints))

    async def search(self, run: Run, queries: list[SearchQuery], *, limit=10, offset=0):
        start = time.monotonic()
        async with self.uow() as tx:
            session = await tx.sessions.get(run.session_id)
            brief = session.brief
            if run.parent_id:
                board = await tx.research.get(run.session_id)
                task = next(item for item in board.tasks if item.id == run.id)
                queries = [
                    query.model_copy(
                        update={"question": query.question or task.assignment.facet_key}
                    )
                    for query in queries
                ]
        fetched = await asyncio.gather(
            *(
                self._shared_fetch(
                    run.parent_id or run.id,
                    query,
                    self._query_scope(query, brief.hard_constraints),
                )
                for query in queries
            )
        )
        diagnostics = tuple(item[1] for item in fetched)
        async with self.uow() as tx:
            resolved = []
            for query, (found, diagnostic) in zip(queries, fetched):
                saved = [await save_paper(tx, paper) for paper in found]
                resolved.append((query, saved))
            board = await tx.research.get(run.session_id)
            candidates = {item.paper_id: item for item in board.candidates}
            before = set(candidates)
            gains = {}
            page_ids, scores = [], {}
            for query, found in resolved:
                seen = set()
                for rank, paper in enumerate(found):
                    paper = await tx.papers.get(paper.canonical_id)
                    key = paper.canonical_id
                    if key in seen:
                        continue
                    seen.add(key)
                    scores[key] = scores.get(key, 0) + 1 / (60 + rank + 1)
                    old = candidates.get(key) or Candidate(paper_id=key)
                    facet = query.question or "general"
                    if brief.basis_revision not in old.basis_revisions:
                        gains[facet] = gains.get(facet, 0) + 1
                    candidates[key] = old.model_copy(
                        update={
                            "basis_revisions": tuple(
                                dict.fromkeys(
                                    (*old.basis_revisions, brief.basis_revision)
                                )
                            ),
                            "run_ids": tuple(dict.fromkeys((*old.run_ids, run.id))),
                            "queries": tuple(dict.fromkeys((*old.queries, query.text))),
                            "sources": tuple(
                                dict.fromkeys((*old.sources, query.source))
                            ),
                            "question": query.question or old.question,
                            "fusion_score": old.fusion_score
                            + (0 if query.text in old.queries else 1 / (60 + rank + 1)),
                        }
                    )
            ordered = sorted(
                candidates.values(),
                key=lambda item: -scores.get(item.paper_id, item.fusion_score),
            )
            papers = {}
            stored = await tx.papers.get_many(
                {
                    item.paper_id
                    for item in ordered
                    if brief.basis_revision in item.basis_revisions
                }
            )
            for item in ordered:
                if brief.basis_revision not in item.basis_revisions or (
                    queries and item.paper_id not in scores
                ):
                    continue
                paper = stored[item.paper_id]
                if not any(
                    check.status == "fail"
                    for check in structural_constraints(paper, brief)
                ):
                    papers[item.paper_id] = paper
                    page_ids.append(item.paper_id)
            new = len(set(candidates) - before)
            progress = {
                (item.basis_revision, item.facet_key): item for item in board.progress
            }
            for facet in {query.question or "general" for query in queries}:
                key = (brief.basis_revision, facet)
                previous = progress.get(key) or SearchProgress(
                    basis_revision=brief.basis_revision, facet_key=facet
                )
                progress[key] = previous.model_copy(
                    update={
                        "stagnant_rounds": 0
                        if gains.get(facet)
                        else previous.stagnant_rounds + 1,
                    }
                )
            board = board.model_copy(
                update={
                    "candidates": tuple(candidates.values()),
                    "queries": (*board.queries, *queries),
                    "progress": tuple(progress.values()),
                }
            )
            await tx.research.save(board)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "research.updated",
                {"research": board.model_dump(mode="json")},
            )
        hits = tuple(
            SearchHit(
                paper=papers[key],
                fusion_score=scores.get(key, candidates[key].fusion_score),
                matched_queries=candidates[key].queries,
                evidence_level="abstract" if papers[key].abstract else "metadata",
            )
            for key in page_ids[offset : offset + limit]
        )
        result = SearchResult(
            hits=hits,
            diagnostics=diagnostics,
            candidate_count=len(candidates),
            new_candidates=new,
            eligible_count=len(page_ids),
            duplicate_ratio=1 - new / max(1, sum(len(item[0]) for item in fetched)),
            next_offset=offset + limit if offset + limit < len(page_ids) else None,
            retrieval_ms=int((time.monotonic() - start) * 1000),
        )
        from paper_search.intent import read_intent

        async with self.uow() as tx:
            intent = await read_intent(tx, run)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "retrieval.completed",
                {"intent": intent, "result": result.model_dump(mode="json")},
            )
        return result

    async def _fetch(self, query: SearchQuery, constraints: SearchConstraints):
        name, start = query.source, time.monotonic()

        def diagnostic(status, **kwargs):
            return SourceDiagnostic(
                provider=name,
                query=query.text,
                mode=query.mode,
                sort=query.sort,
                date_from=constraints.date_from,
                date_to=constraints.date_to,
                status=status,
                duration_ms=int((time.monotonic() - start) * 1000),
                **kwargs,
            )

        if name not in self.providers:
            return (), diagnostic("unavailable", error="来源未启用")
        # The question explains intent; it does not change the provider request.
        key = (
            query.model_dump_json(exclude={"question"}),
            constraints.model_dump_json(exclude={"excluded_ids"}),
        )
        cached = self.cache.get(key)
        if cached and start - cached[0] < 600:
            return cached[1], diagnostic("ok", count=len(cached[1]), cached=True)
        async with self.limits[name]:
            if self.cooldown.get(name, 0) > time.monotonic():
                return (), diagnostic(
                    "unavailable",
                    error="来源请求失败后暂时冷却中，请选择其他来源或稍后继续",
                )
            for attempt in range(2):
                try:
                    async with asyncio.timeout(self.timeout):
                        kwargs = {
                            "date_from": constraints.date_from,
                            "date_to": constraints.date_to,
                            "open_access_only": constraints.open_access_only,
                            "limit": 30,
                        }
                        papers = await self.providers[name].search(
                            query.text,
                            mode=query.mode,
                            offset=query.offset,
                            sort=query.sort,
                            **kwargs,
                        )
                    self.cache[key] = (time.monotonic(), papers)
                    if len(self.cache) > 256:
                        del self.cache[next(iter(self.cache))]
                    return papers, diagnostic("ok", count=len(papers))
                except Exception as exc:  # noqa: BLE001 - isolate and report provider failures
                    response = getattr(exc, "response", None)
                    status = getattr(exc, "status_code", None) or getattr(
                        response, "status_code", None
                    )
                    if status in {401, 403, 406}:
                        self.cooldown[name] = time.monotonic() + 60
                        return (), diagnostic(
                            "error",
                            error=f"HTTP {status}，来源暂不可用，冷却 60 秒；请切换来源，错误不等于无论文",
                        )
                    if status == 429:
                        delay = 30.0
                        raw_delay = (
                            response.headers.get("Retry-After", "")
                            if response is not None
                            else ""
                        )
                        if raw_delay.isdigit():
                            delay = max(1, float(raw_delay))
                        self.cooldown[name] = time.monotonic() + delay
                        return (), diagnostic(
                            "error", error=f"HTTP 429，{delay:g} 秒后可重试"
                        )
                    if attempt == 0 and (
                        isinstance(exc, TimeoutError) or status in {500, 502, 503, 504}
                    ):
                        await asyncio.sleep(1)
                        continue
                    detail = (
                        str(exc)
                        if isinstance(exc, ToolInputError)
                        else type(exc).__name__
                    )
                    if isinstance(exc, json.JSONDecodeError):
                        detail = "来源返回非 JSON 响应，不能当作零结果"
                    return (), diagnostic(
                        "error", error=detail + (f" HTTP {status}" if status else "")
                    )

    @staticmethod
    def _query_scope(query: SearchQuery, hard: SearchConstraints) -> SearchConstraints:
        lower = [value for value in (query.date_from, hard.date_from) if value]
        upper = [value for value in (query.date_to, hard.date_to) if value]
        start, end = max(lower, default=None), min(upper, default=None)
        if start and end and start > end:
            raise ToolInputError(
                "查询时间范围与用户约束没有交集，请修正查询；不能忽略用户约束。"
            )
        return hard.model_copy(update={"date_from": start, "date_to": end})
