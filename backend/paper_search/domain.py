"""Validated application values. No transport, provider or database dependencies."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


class Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_serialization_defaults_required=True
    )


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


Effort = Literal["quick", "deep"]
ResearchMode = Literal["topic_search", "related_work_delta", "baseline_selection"]


class ComparisonSeed(Value):
    original: str
    paper_id: str | None = None
    document_id: str | None = None


class ExperimentalContext(Value):
    task: str | None = None
    datasets: tuple[str, ...] = ()
    base_models: tuple[str, ...] = ()
    budget: str | None = None
    metrics: tuple[str, ...] = ()


class DeliverySpec(Value):
    count: int | None = Field(default=None, ge=1, le=30)
    count_mode: Literal["exact", "maximum"] = "maximum"
    format: Literal["markdown", "json", "report"] = "markdown"


class ExecutionError(Value):
    code: str
    message: str
    exception_type: str
    retryable: bool = False
    http_status: int | None = None
    finish_reason: str | None = None
    provider_request_id: str | None = None
    stack: tuple[str, ...] = ()


class RunLimits(Value):
    target_seconds: int = Field(default=90, ge=1)
    deadline_seconds: int = Field(default=180, ge=30)
    finalization_reserve_seconds: int = Field(default=30, ge=5)
    max_concurrent_workers: int = Field(default=0, ge=0, le=3)
    soft_token_target: int = Field(default=64000, ge=1000)

    @model_validator(mode="after")
    def ordered_deadlines(self):
        if self.target_seconds > self.deadline_seconds:
            raise ValueError("target_seconds must not exceed deadline_seconds")
        if self.finalization_reserve_seconds >= self.deadline_seconds:
            raise ValueError(
                "finalization_reserve_seconds must be below deadline_seconds"
            )
        return self


class ModelOptions(Value):
    model: str = "test"
    base_url: str = "http://localhost"
    context_window: int = 128000
    max_output_tokens_supported: int = 128000
    context_output_headroom: int = 64000
    force_max_output_tokens: bool = False
    capability_source: str = "configured"
    supported_reasoning_efforts: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    reasoning_effort: str = "high"
    extra_body: dict[str, Any] = Field(default_factory=dict)
    input_cny_per_million: float = Field(default=12, ge=0)
    output_cny_per_million: float = Field(default=27, ge=0)
    cached_input_cny_per_million: float = Field(default=12, ge=0)


class ExecutionSettings(Value):
    effort: Effort = "quick"
    profile: str = "default"
    model: ModelOptions = Field(default_factory=ModelOptions)
    limits: RunLimits = Field(default_factory=RunLimits)


class TaskBrief(Value):
    input_revision: int = 0
    revision: int = 0
    basis_revision: int = 0
    objective: str = ""
    research_mode: ResearchMode = "topic_search"
    focus: tuple[str, ...] = ()
    seeds: tuple[ComparisonSeed, ...] = ()
    experimental_context: ExperimentalContext = Field(
        default_factory=ExperimentalContext
    )
    source_message_ids: tuple[str, ...] = ()
    delivery: DeliverySpec = Field(default_factory=DeliverySpec)
    constraints: tuple[str, ...] = Field(
        default=(), description="用户必须满足的条件，保留原意，不能降格成优先偏好"
    )
    preferences: tuple[str, ...] = Field(
        default=(), description="用户允许权衡的偏好，不放入明确的必要条件"
    )
    assumptions: tuple[str, ...] = Field(
        default=(), description="Agent 的暂定理解，不得冒充用户要求；变化时说明原因"
    )
    open_questions: tuple[str, ...] = ()
    hard_constraints: SearchConstraints = Field(
        default_factory=lambda: SearchConstraints()
    )


class WorkingSummary(Value):
    text: str = ""
    through_message_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    memory_revision: int = 0
    input_revision: int = 0


class ContextCompressionState(Value):
    revision: int = 0
    history_snipped: bool = False
    snipped_message_ids: tuple[str, ...] = ()
    cleared_tool_keys: tuple[str, ...] = ()
    collapsed_tool_keys: tuple[str, ...] = ()
    cleared_continuation_message_ids: tuple[str, ...] = ()
    consecutive_autocompact_failures: int = 0


class Session(Value):
    id: str = Field(default_factory=new_id)
    title: str = "新研究"
    effort: Effort = "quick"
    input_revision: int = 0
    brief: TaskBrief = Field(default_factory=TaskBrief)
    summary: WorkingSummary = Field(default_factory=WorkingSummary)
    context_state: ContextCompressionState = Field(
        default_factory=ContextCompressionState
    )
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class ToolCall(Value):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class SourceContent(Value):
    url: HttpUrl
    text: str


class ToolResult(Value):
    content: str
    data: Any = None
    artifact_ids: tuple[str, ...] = ()
    is_error: bool = False
    error_code: str | None = None
    retryable: bool = False
    sources: tuple[SourceContent, ...] = ()
    snapshot_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class Message(Value):
    id: str = Field(default_factory=new_id)
    session_id: str
    run_id: str
    role: Literal["user", "assistant", "tool", "system"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_result: ToolResult | None = None
    client_id: str | None = None
    document_ids: tuple[str, ...] = ()
    input_revision: int = 0
    task_revision: int = 0
    effort: Effort | None = None
    usage: Usage | None = None
    provider_response_id: str | None = None
    context_revision: int = 0
    created_at: datetime = Field(default_factory=now)


class StreamingMessage(Value):
    id: str
    run_id: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    created_at: datetime = Field(default_factory=now)


class Usage(Value):
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    estimated: bool = False
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    cost_cny: float = 0
    active_seconds: float = 0
    waiting_seconds: float = 0


class SkillUse(Value):
    name: str
    version: str
    file_id: str
    references: dict[str, str] = Field(default_factory=dict)
    loaded_references: tuple[str, ...] = ()


class Run(Value):
    id: str = Field(default_factory=new_id)
    session_id: str
    parent_id: str | None = None
    status: RunStatus = RunStatus.QUEUED
    phase: Literal["queued", "research", "delivery", "waiting", "finished"] = "queued"
    reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    budget_extensions: int = 0
    token_soft_target: int = Field(default=64000, ge=1000)
    deadline_at: datetime | None = None
    own_usage: Usage = Field(default_factory=Usage)
    task_revision: int = 0
    skills: tuple[SkillUse, ...] = ()
    finding_cursor: int = 0
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    brief: TaskBrief | None = None
    input_revision: int = 0
    summary: WorkingSummary = Field(default_factory=WorkingSummary)
    context_state: ContextCompressionState = Field(
        default_factory=ContextCompressionState
    )
    waiting_document_ids: tuple[str, ...] = ()
    waiting_since: datetime | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class ToolExecution(Value):
    id: str
    run_id: str
    call: ToolCall
    capability: Literal[
        "search", "read", "write", "planning", "delegation", "other"
    ] = "other"
    state: Literal["queued", "executing", "completed", "yielded", "uncertain"] = (
        "queued"
    )
    replay_safe: bool = False
    input_revision: int = 0
    model_stream_id: str | None = None
    result: ToolResult | None = None
    queued_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    yielded_at: datetime | None = None
    attempts: int = 1
    error: ExecutionError | None = None
    duration_ms: int | None = None


class Job(Value):
    id: str = Field(default_factory=new_id)
    delivery_id: str = Field(default_factory=new_id)
    kind: Literal["run", "document", "memory"]
    session_id: str
    target_id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = (
        "pending"
    )
    owner: str | None = None
    lease_until: datetime | None = None
    attempts: int = 0
    error: str | None = None


class Delivery(Value):
    id: str
    job_id: str
    session_id: str


class SourceRecord(Value):
    provider: str
    provider_paper_id: str
    canonical_id: str
    source_url: str | None = None
    retrieved_at: datetime = Field(default_factory=now)
    publication_date: date | None = None
    updated_date: date | None = None
    identity_status: Literal["discovered", "verified"] = "discovered"


class Affiliation(Value):
    name: str
    provider: str
    source_url: str | None = None


class Paper(Value):
    canonical_id: str
    doi: str | None = None
    aliases: tuple[str, ...] = ()
    title: str
    authors: tuple[str, ...] = ()
    year: int | None = None
    publication_date: date | None = None
    updated_date: date | None = None
    affiliations: tuple[Affiliation, ...] = ()
    venue: str | None = None
    abstract: str | None = None
    landing_page_url: HttpUrl | None = None
    pdf_url: HttpUrl | None = None
    is_open_access: bool = False
    open_access_status: Literal["pass", "fail", "unknown"] = "unknown"
    publication_status: Literal[
        "preprint", "submitted", "accepted", "published", "unknown"
    ] = "unknown"
    code_urls: tuple[HttpUrl, ...] = ()
    provider: str = "web"
    arxiv_id: str | None = None
    source_urls: tuple[HttpUrl, ...] = ()
    source_records: tuple[SourceRecord, ...] = ()

    @property
    def primary_url(self) -> HttpUrl | None:
        return self.landing_page_url or (
            self.source_urls[0] if self.source_urls else None
        )


class PaperResolution(Value):
    identifier: str
    status: Literal["resolved", "unresolved", "unavailable"]
    paper: Paper | None = None
    candidates: tuple[Paper, ...] = ()
    reason: str | None = None
    source: SourceContent | None = None


class SearchConstraints(Value):
    date_from: date | None = None
    date_to: date | None = None
    open_access_only: bool = False
    excluded_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not exceed date_to")
        return self


class SearchHit(Value):
    paper: Paper
    fusion_score: float = 0
    matched_queries: tuple[str, ...] = ()
    evidence_level: Literal["metadata", "abstract", "fulltext"] = "metadata"


class SourceDiagnostic(Value):
    provider: str
    query: str
    status: Literal["ok", "error", "unavailable", "not_queried"]
    count: int = 0
    error: str | None = None
    duration_ms: int = 0
    cached: bool = False
    mode: str = "keyword"
    sort: str = "relevance"
    date_from: date | None = None
    date_to: date | None = None


class SearchResult(Value):
    hits: tuple[SearchHit, ...]
    diagnostics: tuple[SourceDiagnostic, ...]
    candidate_count: int = 0
    new_candidates: int = 0
    duplicate_ratio: float = 0
    next_offset: int | None = None
    eligible_count: int = 0
    retrieval_ms: int = 0


class SearchQuery(Value):
    source: str
    text: str = Field(min_length=1, max_length=2000)
    mode: Literal["keyword", "title"] = "keyword"
    offset: int = Field(default=0, ge=0)
    question: str = ""
    date_from: date | None = Field(
        default=None,
        description="本次发现范围的起始日期 YYYY-MM-DD，可根据意图解释；不得突破用户硬约束",
    )
    date_to: date | None = Field(
        default=None, description="本次发现范围的截止日期 YYYY-MM-DD"
    )
    sort: Literal["relevance", "newest"] = "relevance"

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not exceed date_to")
        return self


class Candidate(Value):
    paper_id: str
    basis_revisions: tuple[int, ...] = ()
    run_ids: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    question: str = ""
    fusion_score: float = 0
    read: bool = False


class EvidenceClaim(Value):
    statement: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class ConstraintCheck(Value):
    requirement: str
    status: Literal["pass", "fail", "unknown"]
    reason: str
    evidence_ids: tuple[str, ...] = ()


class QualitySignal(Value):
    kind: Literal[
        "venue",
        "publication",
        "affiliation",
        "official_code",
        "public_code",
        "reproduction",
        "experiment",
        "citations",
    ]
    value: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class SeedComparison(Value):
    seed_paper_id: str
    shared_problem: EvidenceClaim
    relation: Literal["different_mechanism", "complementary", "same_method", "unclear"]
    differences: tuple[EvidenceClaim, ...] = ()
    limitations: tuple[str, ...] = ()


class BaselineFit(Value):
    route: EvidenceClaim
    status: Literal["aligned", "conditional", "incompatible", "unknown"]
    dimensions: tuple[ConstraintCheck, ...] = ()
    adjustments: tuple[str, ...] = ()


class PaperAssessment(Value):
    paper_id: str
    basis_revision: int
    assessment_revision: int = 0
    author_run_id: str = ""
    fit: Literal["direct", "partial", "background", "irrelevant", "unknown"]
    reason: str = Field(min_length=1)
    matched_requirements: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    constraints: tuple[ConstraintCheck, ...] = ()
    quality_signals: tuple[QualitySignal, ...] = ()
    comparisons: tuple[SeedComparison, ...] = ()
    baseline: BaselineFit | None = None
    limitations: tuple[str, ...] = ()
    disposition: Literal["shortlisted", "conditional", "background", "rejected"] = (
        "shortlisted"
    )


class ResearchQuestion(Value):
    id: str = Field(default_factory=new_id)
    text: str
    core: bool = True
    status: Literal["open", "satisfied", "blocked"] = "open"
    evidence_ids: tuple[str, ...] = ()
    paper_ids: tuple[str, ...] = ()
    reason: str = ""


class TaskAssignment(Value):
    kind: Literal["discovery", "evidence"]
    goal: str = Field(min_length=1, max_length=1000)
    facet_key: str = Field(min_length=1, max_length=80)
    question_ids: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    queries: tuple[SearchQuery, ...] = ()
    paper_ids: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    completion_criteria: tuple[str, ...] = ()


class WorkerTask(Value):
    id: str = Field(default_factory=new_id)
    root_run_id: str
    assignment: TaskAssignment
    basis_revision: int
    input_revision: int
    task_revision: int = 1
    status: Literal[
        "queued",
        "running",
        "waiting",
        "done",
        "blocked",
        "failed",
        "cancelled",
        "stale",
    ] = "queued"
    instructions: tuple[str, ...] = ()
    applied_update_ids: tuple[str, ...] = ()
    reason: str = ""
    created_at: datetime = Field(default_factory=now)


class Finding(Value):
    id: str = Field(default_factory=new_id)
    cursor: int = 0
    run_id: str = ""
    basis_revision: int
    task_revision: int = 0
    paper_id: str | None = None
    aspect: Literal["match", "mechanism", "experiment", "quality", "identity", "gap"]
    statement: str
    evidence_ids: tuple[str, ...] = ()
    assessment: PaperAssessment | None = None
    unanswered_questions: tuple[str, ...] = ()


class ResearchSelection(Value):
    paper_id: str
    title: str
    url: str | None = None
    assessment: PaperAssessment


class ResearchResult(Value):
    mode: ResearchMode
    input_revision: int
    basis_revision: int
    result_revision: int
    submission_call_id: str = ""
    run_id: str
    status: Literal["complete", "partial", "needs_input"]
    selected: tuple[ResearchSelection, ...] = ()
    pending_paper_ids: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    summary: str = ""
    markdown: str = ""


class SearchProgress(Value):
    basis_revision: int
    facet_key: str
    stagnant_rounds: int = 0
    evidence_count: int = 0


class ResearchBoard(Value):
    session_id: str
    candidates: tuple[Candidate, ...] = ()
    queries: tuple[SearchQuery, ...] = ()
    questions: tuple[ResearchQuestion, ...] = ()
    tasks: tuple[WorkerTask, ...] = ()
    findings: tuple[Finding, ...] = ()
    assessments: tuple[PaperAssessment, ...] = ()
    progress: tuple[SearchProgress, ...] = ()
    result: ResearchResult | None = None
    result_revision: int = 0
    cursor: int = 0


class SourceSnapshot(Value):
    id: str = Field(default_factory=new_id)
    session_id: str
    run_id: str
    url: str
    artifact_id: str
    tool_call_id: str
    retrieved_at: datetime = Field(default_factory=now)


class SourceSnapshotView(Value):
    snapshot: SourceSnapshot
    text: str


class DocumentBlock(Value):
    id: str = Field(default_factory=new_id)
    page: int = Field(ge=1)
    section: str = ""
    text: str
    bbox: tuple[float, float, float, float] | None = None
    page_width: float | None = None
    page_height: float | None = None


class ParsedDocument(Value):
    blocks: tuple[DocumentBlock, ...]
    page_count: int
    parser: str


class Document(Value):
    id: str = Field(default_factory=new_id)
    session_id: str
    owner_run_id: str | None = None
    research_deadline_at: datetime | None = None
    basis_revision: int | None = None
    paper_id: str | None = None
    name: str
    source_url: str | None = None
    sha256: str | None = None
    file_id: str | None = None
    status: Literal[
        "pending", "downloading", "parsing", "indexing", "ready", "failed", "cancelled"
    ] = "pending"
    error: str | None = None
    parsed: ParsedDocument | None = None
    indexed_blocks: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=now)


class Evidence(Value):
    id: str = Field(default_factory=new_id)
    session_id: str
    run_id: str | None = None
    snapshot_id: str | None = None
    paper_id: str | None = None
    document_id: str | None = None
    document_sha256: str | None = None
    block_id: str | None = None
    page: int | None = None
    section: str = ""
    excerpt: str
    source_url: str | None = None
    level: Literal["metadata", "abstract", "fulltext", "web"]
    origin_evidence_id: str | None = None
    created_at: datetime = Field(default_factory=now)


class Artifact(Value):
    id: str = Field(default_factory=new_id)
    session_id: str
    run_id: str | None = None
    kind: Literal["report", "tool_result", "file", "web_source", "skill"]
    title: str
    file_id: str
    mime_type: str = "text/plain"
    previous_id: str | None = None
    created_at: datetime = Field(default_factory=now)


class Memory(Value):
    id: str = Field(default_factory=new_id)
    key: str
    content: str
    scope: Literal["global", "session"] = "session"
    session_id: str | None = None
    source_message_id: str | None = None
    kind: Literal["preference", "research", "experience"] = "research"
    evidence_ids: tuple[str, ...] = ()
    paper_ids: tuple[str, ...] = ()
    source_run_id: str | None = None
    skill_name: str | None = None
    skill_version: str | None = None
    applicability: tuple[str, ...] = ()
    outcome: str = ""
    support: Literal["observed", "user_feedback", "suggested"] = "suggested"
    deleted: bool = False
    revision: int = 1
    updated_at: datetime = Field(default_factory=now)


class IndexRecord(Value):
    id: str
    kind: Literal["memory", "document"]
    scope: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class IndexHit(Value):
    record: IndexRecord
    score: float


class ModelMessage(Value):
    role: Literal["user", "assistant", "tool", "system"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    continuation_ref: str | None = None
    usage: Usage | None = None
    provider_response_id: str | None = None
    context_revision: int = 0


class ModelRequest(Value):
    messages: tuple[ModelMessage, ...]
    tools: tuple[dict[str, Any], ...] = ()
    max_output_tokens: int | None = Field(default=None, ge=1)
    options: ModelOptions | None = None
    purpose: str = "agent"
    response_format: Literal["text", "json_object"] = "text"
    timeout_seconds: float | None = None


class ModelEvent(Value):
    type: Literal["text", "tool_call", "usage", "done"]
    text: str = ""
    tool_call: ToolCall | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    continuation_ref: str | None = None
    provider_request_id: str | None = None
    finish_reason: str | None = None


class MissingRecord(LookupError):
    pass


class ModelUnavailable(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str = "model_error",
        status: int | None = None,
        finish_reason: str | None = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.status = status
        self.finish_reason = finish_reason
        self.request_id = request_id
        self.partial_content = ""


class Conflict(ValueError):
    pass


class ToolInputError(ValueError):
    """Rejected before performing any side effect; safe to return to the model."""


class DeadlineExceeded(RuntimeError):
    pass
