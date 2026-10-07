"""Evidence-grounded research state and the single business delivery boundary."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Literal

from pydantic import Field

from paper_search.diagnostics import current_tool_call
from paper_search.domain import (
    ConstraintCheck,
    Evidence,
    Finding,
    Paper,
    PaperAssessment,
    ResearchBoard,
    ResearchQuestion,
    ResearchResult,
    ResearchSelection,
    Run,
    RunStatus,
    SearchProgress,
    TaskBrief,
    ToolInputError,
    Value,
)
from paper_search.ports import UnitOfWork, UowFactory


class Submission(Value):
    input_revision: int
    basis_revision: int
    selected_paper_ids: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    summary: str = ""
    needs_input: bool = False


class TopicSubmission(Submission):
    mode: Literal["topic_search"]


class RelatedSubmission(Submission):
    mode: Literal["related_work_delta"]


class BaselineSubmission(Submission):
    mode: Literal["baseline_selection"]


ResearchSubmission = Annotated[
    TopicSubmission | RelatedSubmission | BaselineSubmission,
    Field(discriminator="mode"),
]


def verified(paper: Paper) -> bool:
    return any(source.identity_status == "verified" for source in paper.source_records)


async def current_basis(tx: UnitOfWork, run: Run) -> TaskBrief:
    current = await tx.runs.get(run.id)
    session = await tx.sessions.get(run.session_id)
    if current.status == RunStatus.CANCELLED:
        raise ToolInputError("任务已取消")
    if session.brief.input_revision != session.input_revision:
        raise ToolInputError("请先 update_task 处理最新用户输入")
    if run.parent_id:
        root = await tx.runs.get(run.parent_id)
        if root.status != RunStatus.RUNNING:
            raise ToolInputError("根任务已暂停或结束")
        board = await tx.research.get(run.session_id)
        task = next((item for item in board.tasks if item.id == run.id), None)
        if not task or task.status in {"cancelled", "stale"}:
            raise ToolInputError("Worker 任务已失效")
        if (
            task.basis_revision != session.brief.basis_revision
            or task.task_revision != run.task_revision
        ):
            raise ToolInputError(
                "研究依据或分工已改变，旧 Worker 不能继续探索或提交判断"
            )
    elif current.input_revision != session.input_revision:
        raise ToolInputError("输入版本已变化")
    return session.brief


def structural_constraints(
    paper: Paper, brief: TaskBrief
) -> tuple[ConstraintCheck, ...]:
    bounds = brief.hard_constraints
    checks = []
    if bounds.date_from or bounds.date_to:
        start = paper.publication_date or (
            date(paper.year, 1, 1) if paper.year else None
        )
        end = paper.publication_date or (
            date(paper.year, 12, 31) if paper.year else None
        )
        lower, upper = bounds.date_from or date.min, bounds.date_to or date.max
        status = "unknown"
        if start and end:
            status = (
                "pass"
                if lower <= start and end <= upper
                else "fail"
                if end < lower or start > upper
                else "unknown"
            )
        checks.append(
            ConstraintCheck(
                requirement="publication_window",
                status=status,
                reason=f"要求 {bounds.date_from or '不限'} 至 {bounds.date_to or '不限'}；来源首次发表 {paper.publication_date or paper.year or '未知'}",
            )
        )
    if bounds.open_access_only:
        checks.append(
            ConstraintCheck(
                requirement="open_access",
                status=paper.open_access_status,
                reason="开放获取状态来自论文来源，不代表代码开源",
            )
        )
    if {paper.canonical_id, *paper.aliases} & set(bounds.excluded_ids):
        checks.append(
            ConstraintCheck(
                requirement="excluded_paper", status="fail", reason="用户已排除该论文"
            )
        )
    return tuple(checks)


def referenced_evidence(assessment: PaperAssessment) -> set[str]:
    ids = set(assessment.evidence_ids)
    for check in assessment.constraints:
        ids.update(check.evidence_ids)
    for signal in assessment.quality_signals:
        ids.update(signal.evidence_ids)
    for comparison in assessment.comparisons:
        ids.update(comparison.shared_problem.evidence_ids)
        for difference in comparison.differences:
            ids.update(difference.evidence_ids)
    if assessment.baseline:
        ids.update(assessment.baseline.route.evidence_ids)
        for dimension in assessment.baseline.dimensions:
            ids.update(dimension.evidence_ids)
    return ids


def check_sources(
    ids: set[str], evidence: dict[str, Evidence], allowed_papers: set[str]
) -> None:
    for key in ids:
        source = evidence.get(key)
        if not source:
            raise ToolInputError(f"当前会话没有证据 {key}")
        if not source.paper_id or source.paper_id not in allowed_papers:
            raise ToolInputError(f"证据 {key} 未绑定当前论文或比较种子")


class ResearchService:
    def __init__(self, uow: UowFactory):
        self.uow = uow

    async def record(
        self,
        run: Run,
        findings: tuple[Finding, ...],
        questions: tuple[ResearchQuestion, ...],
    ) -> dict:
        async with self.uow() as tx:
            brief = await current_basis(tx, run)
            board = await tx.research.get(run.session_id)
            evidence = {
                item.id: item for item in await tx.papers.list_evidence(run.session_id)
            }
            seed_ids = set()
            for seed in brief.seeds:
                if seed.paper_id:
                    try:
                        seed_ids.add((await tx.papers.get(seed.paper_id)).canonical_id)
                    except LookupError:
                        pass
            accepted, rejected = [], []
            assessments = {item.paper_id: item for item in board.assessments}
            task = next((item for item in board.tasks if item.id == run.id), None)
            cursor = board.cursor
            for index, finding in enumerate(findings):
                call_id = current_tool_call.get()
                finding_id = (
                    f"finding-{run.id}-{call_id}-{index}" if call_id else finding.id
                )
                existing = next(
                    (item for item in board.findings if item.id == finding_id), None
                )
                if existing:
                    accepted.append(existing)
                    continue
                try:
                    if finding.basis_revision != brief.basis_revision:
                        raise ToolInputError("Finding 的 basis_revision 已过期")
                    if task and finding.task_revision != task.task_revision:
                        raise ToolInputError(
                            "Worker 分工已更新，请按最新 task_revision 提交"
                        )
                    paper = (
                        await tx.papers.get(finding.paper_id)
                        if finding.paper_id
                        else None
                    )
                    allowed = (
                        {paper.canonical_id, *paper.aliases, *seed_ids}
                        if paper
                        else seed_ids
                    )
                    if finding.aspect != "gap" and not finding.evidence_ids:
                        raise ToolInputError("事实发现必须引用证据")
                    check_sources(set(finding.evidence_ids), evidence, allowed)
                    assessment = finding.assessment
                    if assessment:
                        if (
                            not paper
                            or assessment.basis_revision != brief.basis_revision
                        ):
                            raise ToolInputError("判断必须对应当前论文与研究依据")
                        assessed_paper = await tx.papers.get(assessment.paper_id)
                        if assessed_paper.canonical_id != paper.canonical_id:
                            raise ToolInputError("Finding 与 assessment 的论文不一致")
                        previous = assessments.get(paper.canonical_id)
                        if previous and previous.basis_revision == brief.basis_revision:
                            assessment = previous.model_copy(
                                update={
                                    name: getattr(assessment, name)
                                    for name in assessment.model_fields_set
                                }
                            )
                        check_sources(
                            referenced_evidence(assessment), evidence, allowed
                        )
                        local_ids = set(assessment.evidence_ids)
                        for check in assessment.constraints:
                            local_ids.update(check.evidence_ids)
                        for signal in assessment.quality_signals:
                            local_ids.update(signal.evidence_ids)
                        if assessment.baseline:
                            local_ids.update(assessment.baseline.route.evidence_ids)
                            for check in assessment.baseline.dimensions:
                                local_ids.update(check.evidence_ids)
                        check_sources(
                            local_ids, evidence, {paper.canonical_id, *paper.aliases}
                        )
                        if not any(
                            evidence[key].paper_id
                            in {paper.canonical_id, *paper.aliases}
                            for key in assessment.evidence_ids
                        ):
                            raise ToolInputError("任务匹配判断缺少该论文证据")
                        previous = assessments.get(paper.canonical_id)
                        assessment = assessment.model_copy(
                            update={
                                "paper_id": paper.canonical_id,
                                "author_run_id": run.id,
                                "assessment_revision": (
                                    previous.assessment_revision + 1
                                )
                                if previous and not run.parent_id
                                else 1,
                            }
                        )
                        if not run.parent_id:
                            assessments[paper.canonical_id] = assessment
                    cursor += 1
                    accepted.append(
                        finding.model_copy(
                            update={
                                "id": finding_id,
                                "cursor": cursor,
                                "run_id": run.id,
                                "paper_id": paper.canonical_id if paper else None,
                                "assessment": assessment,
                            }
                        )
                    )
                except (ToolInputError, LookupError) as exc:
                    rejected.append({"paper_id": finding.paper_id, "reason": str(exc)})
            question_map = {item.id: item for item in board.questions}
            for question in questions:
                if question.id not in question_map:
                    rejected.append(
                        {
                            "question_id": question.id,
                            "reason": "问题不存在，请先 update_task",
                        }
                    )
                    continue
                if run.parent_id:
                    rejected.append(
                        {
                            "question_id": question.id,
                            "reason": "Worker 发布发现，主 Agent 决定问题是否解决",
                        }
                    )
                    continue
                if question.status == "satisfied" and not question.evidence_ids:
                    rejected.append(
                        {
                            "question_id": question.id,
                            "reason": "解决研究问题需要证据引用",
                        }
                    )
                    continue
                if any(key not in evidence for key in question.evidence_ids):
                    rejected.append(
                        {"question_id": question.id, "reason": "问题引用了未知证据"}
                    )
                    continue
                original = question_map[question.id]
                question_map[question.id] = original.model_copy(
                    update=question.model_dump(exclude={"id", "text", "core"})
                )
            progress = {
                (item.basis_revision, item.facet_key): item for item in board.progress
            }
            facet = task.assignment.facet_key if task else "lead_evidence"
            key = (brief.basis_revision, facet)
            previous = progress.get(key) or SearchProgress(
                basis_revision=brief.basis_revision, facet_key=facet
            )
            meaningful = [
                item
                for item in accepted
                if item.cursor > board.cursor and item.aspect != "gap"
            ]
            if meaningful:
                ids = {
                    eid
                    for item in (*board.findings, *meaningful)
                    if item.basis_revision == brief.basis_revision
                    and item.run_id == run.id
                    for eid in item.evidence_ids
                }
                progress[key] = previous.model_copy(
                    update={"stagnant_rounds": 0, "evidence_count": len(ids)}
                )
            board = board.model_copy(
                update={
                    "findings": (
                        *board.findings,
                        *(item for item in accepted if item.cursor > board.cursor),
                    ),
                    "cursor": cursor,
                    "progress": tuple(progress.values()),
                    "assessments": tuple(assessments.values()),
                    "questions": tuple(question_map.values()),
                }
            )
            await tx.research.save(board)
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "research.updated",
                {"research": board.model_dump(mode="json")},
            )
        return {
            "accepted": [item.id for item in accepted],
            "rejected": rejected,
            "cursor": cursor,
        }

    async def submit(self, run: Run, submission: ResearchSubmission) -> ResearchResult:
        if run.parent_id:
            raise ToolInputError("只有主 Agent 可以提交研究结果")
        async with self.uow() as tx:
            brief = await current_basis(tx, run)
            if (
                submission.input_revision,
                submission.basis_revision,
                submission.mode,
            ) != (brief.input_revision, brief.basis_revision, brief.research_mode):
                raise ToolInputError("提交的输入、研究依据或业务模式已变化")
            board = await tx.research.get(run.session_id)
            call_id = current_tool_call.get()
            if (
                call_id
                and valid_result(board.result, brief, run)
                and board.result.submission_call_id == call_id
            ):
                return board.result
            result = await self.compose(tx, run, brief, board, submission)
            await tx.research.save(
                board.model_copy(
                    update={"result": result, "result_revision": result.result_revision}
                )
            )
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "research.updated",
                {
                    "research": board.model_copy(
                        update={
                            "result": result,
                            "result_revision": result.result_revision,
                        }
                    ).model_dump(mode="json")
                },
            )
        return result

    async def compose(
        self,
        tx: UnitOfWork,
        run: Run,
        brief: TaskBrief,
        board: ResearchBoard,
        submission: Submission,
    ) -> ResearchResult:
        assessments = {
            item.paper_id: item
            for item in board.assessments
            if item.basis_revision == brief.basis_revision
        }
        evidence = {
            item.id: item for item in await tx.papers.list_evidence(run.session_id)
        }
        selected, pending, gaps = [], [], list(submission.gaps)
        seeds = set()
        for seed in brief.seeds:
            if seed.paper_id:
                try:
                    seed_paper = await tx.papers.get(seed.paper_id)
                    if not verified(seed_paper):
                        raise ToolInputError("种子身份未确认")
                    seeds.add(seed_paper.canonical_id)
                except (LookupError, ToolInputError):
                    gaps.append(f"种子尚未确认：{seed.original}")
            else:
                gaps.append(f"种子尚未确认：{seed.original}")
        covered = set()
        if brief.research_mode == "related_work_delta" and not brief.seeds:
            gaps.append("未指定种子论文")
        seen_papers = set()
        for key in dict.fromkeys(submission.selected_paper_ids):
            try:
                paper = await tx.papers.get(key)
                if paper.canonical_id in seen_papers:
                    continue
                seen_papers.add(paper.canonical_id)
                assessment = assessments.get(paper.canonical_id)
                if not verified(paper):
                    raise ToolInputError("身份未由可靠来源确认")
                if not assessment:
                    raise ToolInputError(
                        "缺少主 Agent 在当前研究依据下的判断，请 record_findings"
                    )
                if (
                    assessment.fit != "direct"
                    or assessment.disposition != "shortlisted"
                ):
                    raise ToolInputError("不属于已确认的直接匹配推荐")
                check_sources(
                    referenced_evidence(assessment),
                    evidence,
                    {paper.canonical_id, *paper.aliases, *seeds},
                )
                checks = list(structural_constraints(paper, brief))
                provided = {item.requirement: item for item in assessment.constraints}
                for requirement in brief.constraints:
                    check = provided.get(
                        requirement,
                        ConstraintCheck(
                            requirement=requirement, status="unknown", reason="尚未判断"
                        ),
                    )
                    if check.status == "pass" and not check.evidence_ids:
                        raise ToolInputError(f"硬约束通过但无证据：{requirement}")
                    checks.append(check)
                if any(item.status != "pass" for item in checks):
                    raise ToolInputError(
                        "；".join(
                            f"{item.requirement}: {item.status} ({item.reason})"
                            for item in checks
                            if item.status != "pass"
                        )
                    )
                if brief.research_mode == "related_work_delta":
                    if paper.canonical_id in seeds:
                        raise ToolInputError("种子不能作为自身的替代方法")
                    matched = set()
                    accepted_comparisons = []
                    for comparison in assessment.comparisons:
                        seed_key = (
                            await tx.papers.get(comparison.seed_paper_id)
                        ).canonical_id
                        if seed_key not in seeds or comparison.relation not in {
                            "different_mechanism",
                            "complementary",
                        }:
                            continue
                        if not comparison.differences:
                            continue
                        for claim in (
                            comparison.shared_problem,
                            *comparison.differences,
                        ):
                            supported = {
                                evidence[item].paper_id for item in claim.evidence_ids
                            }
                            if not {paper.canonical_id, seed_key}.issubset(supported):
                                raise ToolInputError(
                                    "共享问题和机制差异需要候选与种子的双边证据"
                                )
                        matched.add(seed_key)
                        accepted_comparisons.append(
                            comparison.model_copy(update={"seed_paper_id": seed_key})
                        )
                    if not matched:
                        raise ToolInputError("没有证据支持的同问题不同机制关系")
                    covered.update(matched)
                    assessment = assessment.model_copy(
                        update={"comparisons": tuple(accepted_comparisons)}
                    )
                if brief.research_mode == "baseline_selection":
                    baseline = assessment.baseline
                    if not baseline or baseline.status != "aligned":
                        raise ToolInputError("实验可比性尚未确认，列为条件性候选")
                    context = brief.experimental_context
                    requested = {
                        name
                        for name in (
                            "task",
                            "datasets",
                            "base_models",
                            "budget",
                            "metrics",
                        )
                        if getattr(context, name)
                    }
                    dimensions = {
                        item.requirement: item for item in baseline.dimensions
                    }
                    for name in requested:
                        dimension = dimensions.get(name)
                        if (
                            not dimension
                            or dimension.status != "pass"
                            or not dimension.evidence_ids
                        ):
                            raise ToolInputError(
                                f"实验条件 {name} 尚未有来源支持的对齐判断"
                            )
                selected.append(
                    ResearchSelection(
                        paper_id=paper.canonical_id,
                        title=paper.title,
                        url=str(paper.primary_url) if paper.primary_url else None,
                        assessment=assessment.model_copy(
                            update={"constraints": tuple(checks)}
                        ),
                    )
                )
            except (ToolInputError, LookupError, KeyError) as exc:
                pending.append(key)
                gaps.append(f"{key}：{exc}")
        gaps.extend(
            item.text + (f"：{item.reason}" if item.reason else "")
            for item in board.questions
            if item.core and item.status != "satisfied"
        )
        count = brief.delivery.count
        maximum = count or 5
        if len(selected) > maximum:
            selected = selected[:maximum]
        if brief.research_mode == "related_work_delta":
            covered = {
                comparison.seed_paper_id
                for item in selected
                for comparison in item.assessment.comparisons
                if comparison.relation in {"different_mechanism", "complementary"}
                and comparison.differences
            }
            gaps.extend(f"尚未覆盖种子 {key}" for key in sorted(seeds - covered))
        if count and brief.delivery.count_mode == "exact" and len(selected) < count:
            gaps.append(
                f"要求 {count} 篇，已有直接相关且满足条件的论文 {len(selected)} 篇，未凑数"
            )
        if not selected and not gaps:
            gaps.append("尚未确认可推荐论文")
        if submission.needs_input and not gaps:
            raise ToolInputError("needs_input 必须说明需要用户补充的具体缺口")
        result = ResearchResult(
            mode=brief.research_mode,
            input_revision=brief.input_revision,
            basis_revision=brief.basis_revision,
            result_revision=board.result_revision + 1,
            submission_call_id=current_tool_call.get() or "",
            run_id=run.id,
            status="needs_input"
            if submission.needs_input
            else "partial"
            if gaps
            else "complete",
            selected=tuple(selected),
            pending_paper_ids=tuple(pending),
            gaps=tuple(dict.fromkeys(gaps)),
            summary=submission.summary,
        )
        return result.model_copy(update={"markdown": render_result(result, brief)})

    async def checkpoint_result(self, run: Run, reason: str) -> ResearchResult | None:
        """Finalize only lead-confirmed assessments, never infer selections from discovery scores."""
        async with self.uow() as tx:
            brief = await current_basis(tx, run)
            board = await tx.research.get(run.session_id)
            if board.result and valid_result(board.result, brief, run):
                return board.result
            ids = tuple(
                item.paper_id
                for item in board.assessments
                if item.basis_revision == brief.basis_revision
                and item.fit == "direct"
                and item.disposition == "shortlisted"
            )
            result = await self.compose(
                tx,
                run,
                brief,
                board,
                Submission(
                    input_revision=brief.input_revision,
                    basis_revision=brief.basis_revision,
                    selected_paper_ids=ids,
                    gaps=(reason,),
                ),
            )
            await tx.research.save(
                board.model_copy(
                    update={"result": result, "result_revision": result.result_revision}
                )
            )
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "research.updated",
                {
                    "research": board.model_copy(
                        update={
                            "result": result,
                            "result_revision": result.result_revision,
                        }
                    ).model_dump(mode="json")
                },
            )
            return result


def valid_result(result: ResearchResult | None, brief: TaskBrief, run: Run) -> bool:
    return bool(
        result
        and result.run_id == run.id
        and result.input_revision == run.input_revision == brief.input_revision
        and result.basis_revision == brief.basis_revision
        and result.mode == brief.research_mode
    )


def render_result(result: ResearchResult, brief: TaskBrief) -> str:
    if brief.delivery.format == "json":
        return json.dumps(
            result.model_dump(mode="json", exclude={"markdown"}),
            ensure_ascii=False,
            indent=2,
        )
    labels = {
        "topic_search": "论文推荐",
        "related_work_delta": "相关工作与机制差异",
        "baseline_selection": "实验 Baseline",
    }
    states = {"complete": "完成", "partial": "部分完成", "needs_input": "需要补充信息"}
    lines = [f"## {labels[result.mode]} · {states[result.status]}", result.summary]
    for index, selection in enumerate(result.selected, 1):
        assessment = selection.assessment
        title = (
            f"[{selection.title}]({selection.url})"
            if selection.url
            else selection.title
        )
        lines.extend(
            [
                f"\n### {index}. {title}",
                assessment.reason,
                "依据："
                + " ".join(f"[evidence:{key}]" for key in assessment.evidence_ids),
            ]
        )
        if assessment.quality_signals:
            lines.append(
                "质量信号："
                + "；".join(
                    signal.value
                    + " "
                    + " ".join(f"[evidence:{key}]" for key in signal.evidence_ids)
                    for signal in assessment.quality_signals
                )
            )
        for comparison in assessment.comparisons:
            lines.append(
                f"与 {comparison.seed_paper_id} 的共享问题：{claim_text(comparison.shared_problem)}"
            )
            lines.extend(
                "机制差异：" + claim_text(claim) for claim in comparison.differences
            )
        if assessment.baseline:
            lines.append("路线：" + claim_text(assessment.baseline.route))
            lines.extend(
                f"实验 {check.requirement}：{check.reason} "
                + " ".join(f"[evidence:{key}]" for key in check.evidence_ids)
                for check in assessment.baseline.dimensions
            )
            lines.extend(
                "调整条件：" + item for item in assessment.baseline.adjustments
            )
        lines.extend("局限：" + item for item in assessment.limitations)
    if result.gaps:
        lines.extend(["\n### 未解决问题", *(f"- {gap}" for gap in result.gaps)])
    if result.pending_paper_ids:
        lines.append("待确认候选：" + "、".join(result.pending_paper_ids))
    return "\n".join(item for item in lines if item)


def claim_text(claim) -> str:
    return (
        claim.statement
        + " "
        + " ".join(f"[evidence:{key}]" for key in claim.evidence_ids)
    )
