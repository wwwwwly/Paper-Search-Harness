"""Persistent paper identity consolidation, independent of a database adapter."""

import asyncio

from paper_search.domain import Candidate, Paper, PaperResolution, Run
from paper_search.ports import PaperResolver, UnitOfWork, UowFactory
from paper_search.retrieval.identity import identity_keys, merge_papers
from paper_search.retrieval.singleflight import SingleFlight


async def save_paper(tx: UnitOfWork, paper: Paper) -> Paper:
    keys = {paper.canonical_id, *identity_keys(paper)}
    replaced = set()
    for item in await tx.papers.find_by_identifiers(keys):
        if keys & {item.canonical_id, *identity_keys(item)}:
            paper = merge_papers(paper, item)
            replaced.add(item.canonical_id)
    for key in replaced - {paper.canonical_id}:
        await tx.papers.remap(key, paper.canonical_id)
        await tx.papers.remove(key)
    await tx.papers.save(paper)
    if replaced - {paper.canonical_id}:
        await _consolidate_boards(tx, paper)
    return paper


async def _consolidate_boards(tx, paper):
    from paper_search.research import render_result

    for session in await tx.sessions.list():
        board = await tx.research.get(session.id)
        if not (
            any(item.paper_id == paper.canonical_id for item in board.candidates)
            or any(item.paper_id == paper.canonical_id for item in board.assessments)
            or (
                board.result
                and any(
                    item.paper_id == paper.canonical_id
                    for item in board.result.selected
                )
            )
        ):
            continue
        candidates = {}
        for item in board.candidates:
            previous = candidates.get(item.paper_id)
            if previous:
                item = item.model_copy(
                    update={
                        key: tuple(
                            dict.fromkeys(
                                (*getattr(previous, key), *getattr(item, key))
                            )
                        )
                        for key in ("basis_revisions", "sources", "run_ids", "queries")
                    }
                    | {
                        "read": previous.read or item.read,
                        "fusion_score": max(previous.fusion_score, item.fusion_score),
                    }
                )
            candidates[item.paper_id] = item
        assessments = {}
        for item in sorted(
            board.assessments,
            key=lambda item: (item.basis_revision, item.assessment_revision),
        ):
            assessments[item.paper_id] = item
        result = board.result
        if result:
            selections = {}
            for item in result.selected:
                if item.paper_id == paper.canonical_id:
                    item = item.model_copy(
                        update={
                            "title": paper.title,
                            "url": str(paper.primary_url)
                            if paper.primary_url
                            else None,
                        }
                    )
                selections.setdefault(item.paper_id, item)
            gaps = result.gaps
            if (
                session.brief.delivery.count
                and session.brief.delivery.count_mode == "exact"
                and len(selections) < session.brief.delivery.count
            ):
                gaps = tuple(
                    dict.fromkeys((*gaps, "身份合并后独立论文数量不足，请补充研究"))
                )
            result = result.model_copy(
                update={
                    "selected": tuple(selections.values()),
                    "pending_paper_ids": tuple(dict.fromkeys(result.pending_paper_ids)),
                    "gaps": gaps,
                    "status": "partial"
                    if gaps and result.status == "complete"
                    else result.status,
                    "result_revision": board.result_revision + 1,
                }
            )
            result = result.model_copy(
                update={"markdown": render_result(result, session.brief)}
            )
        await tx.research.save(
            board.model_copy(
                update={
                    "candidates": tuple(candidates.values()),
                    "assessments": tuple(assessments.values()),
                    "result": result,
                    "result_revision": result.result_revision
                    if result
                    else board.result_revision,
                }
            )
        )


async def attach_candidate(
    tx: UnitOfWork,
    session_id: str,
    paper: Paper,
    source: str,
    run_id: str,
    basis_revision: int | None = None,
):
    session = await tx.sessions.get(session_id)
    record = await tx.research.get(session_id)
    candidates = {item.paper_id: item for item in record.candidates}
    previous = candidates.get(
        paper.canonical_id, Candidate(paper_id=paper.canonical_id)
    )
    candidates[paper.canonical_id] = previous.model_copy(
        update={
            "basis_revisions": tuple(
                dict.fromkeys(
                    (
                        *previous.basis_revisions,
                        basis_revision
                        if basis_revision is not None
                        else session.brief.basis_revision,
                    )
                )
            ),
            "sources": tuple(dict.fromkeys((*previous.sources, source))),
            "run_ids": tuple(dict.fromkeys((*previous.run_ids, run_id))),
        }
    )
    await tx.research.save(
        record.model_copy(update={"candidates": tuple(candidates.values())})
    )


class PaperCatalog:
    """Read paper sources concurrently, then consolidate them in one transaction."""

    def __init__(
        self,
        uow: UowFactory,
        resolvers: tuple[PaperResolver, ...],
    ):
        self.uow, self.resolvers = uow, resolvers
        self.inflight = SingleFlight()

    async def resolve(
        self, run: Run, identifiers: list[str]
    ) -> tuple[PaperResolution, ...]:
        async with self.uow() as tx:
            current = await tx.runs.get(run.id)
            brief = (
                current.brief
                if run.parent_id
                else (await tx.sessions.get(run.session_id)).brief
            )

        async def read(identifier):
            # get_paper owns local reads. Resolution explicitly consults the source;
            # a cached unversioned alias cannot answer a latest-version request.
            resolver = next(
                (item for item in self.resolvers if item.accepts(identifier)), None
            )
            return (
                await resolver.resolve(identifier)
                if resolver
                else PaperResolution(
                    identifier=identifier,
                    status="unavailable",
                    reason="没有配置支持此标识的论文解析能力",
                )
            )

        async def shared(key):
            scope = (run.parent_id or run.id, key)
            return await self.inflight.run(scope, lambda: read(key))

        resolved = await asyncio.gather(
            *(shared(key) for key in dict.fromkeys(identifiers))
        )
        results = []
        async with self.uow() as tx:
            for result in resolved:
                if result.paper:
                    paper = await save_paper(tx, result.paper)
                    await attach_candidate(
                        tx,
                        run.session_id,
                        paper,
                        "resolution",
                        run.id,
                        brief.basis_revision,
                    )
                    result = result.model_copy(update={"paper": paper})
                    await tx.sessions.append_event(
                        run.session_id,
                        run.id,
                        "paper.resolved",
                        {
                            "paper": paper.model_dump(mode="json"),
                            "identifier": result.identifier,
                            "source": "resolution",
                        },
                    )
                results.append(result)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "research.updated",
                {
                    "research": (await tx.research.get(run.session_id)).model_dump(
                        mode="json"
                    ),
                },
            )
        return tuple(results)
