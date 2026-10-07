"""Validated event contracts shared by persistence, HTTP and generated clients."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from paper_search.domain import (
    Artifact,
    Document,
    Effort,
    Evidence,
    ExecutionError,
    Memory,
    Message,
    Paper,
    ResearchBoard,
    Run,
    RunLimits,
    SearchConstraints,
    SearchHit,
    SearchResult,
    Session,
    SourceDiagnostic,
    TaskBrief,
    ToolExecution,
    Usage,
    Value,
    now,
)


class EventEnvelope(Value):
    seq: int
    session_id: str
    run_id: str | None = None
    created_at: datetime = Field(default_factory=now)


class RunEvent(EventEnvelope):
    run_id: str


class SessionData(Value):
    session: Session


class SessionChanged(EventEnvelope):
    type: Literal["session.created", "session.updated"]
    data: SessionData


class RunData(Value):
    run: Run


class RunChanged(RunEvent):
    type: Literal["run.updated"]
    data: RunData


class RunStartedData(Value):
    model_profile: str
    model: str
    limits: RunLimits
    effort: Effort


class RunStarted(RunEvent):
    type: Literal["run.started"]
    data: RunStartedData


class MessageData(Value):
    message: Message


class MessageCompleted(RunEvent):
    type: Literal["message.completed"]
    data: MessageData


class ModelStartedData(Value):
    stream_id: str
    request_file: str
    estimated_input_tokens: int
    reserved_input_tokens: int = 0
    reserved_output_tokens: int = 0
    token_estimate_method: str = "legacy"
    publish: bool
    created_at: datetime
    purpose: str = "unclassified"
    parent_tool_call_id: str | None = None
    model: str = ""
    reasoning_effort: str = ""
    max_output_tokens: int | None = None


class ModelStarted(RunEvent):
    type: Literal["model.started"]
    data: ModelStartedData


class MessageDeltaData(Value):
    stream_id: str
    text: str


class MessageDelta(RunEvent):
    type: Literal["message.delta"]
    data: MessageDeltaData


class ModelFinishedData(Value):
    stream_id: str
    usage: Usage
    completed: bool
    duration_ms: int | None = None
    first_text_ms: int | None = None
    error: ExecutionError | None = None
    provider_request_id: str | None = None
    finish_reason: str | None = None
    response_file: str | None = None


class ModelFinished(RunEvent):
    type: Literal["model.finished"]
    data: ModelFinishedData


class CitationsCheckedData(Value):
    invalid_citations: tuple[str, ...] = ()


class CitationsChecked(RunEvent):
    type: Literal["answer.citations_checked"]
    data: CitationsCheckedData


class ModelRetryData(Value):
    attempt: int
    reason: str
    network_retry: int = 0
    continuation_retry: int = 0
    explicit_native_limit: bool = False


class ModelRetry(RunEvent):
    type: Literal["model.retry"]
    data: ModelRetryData


class ToolData(Value):
    execution: ToolExecution


class ToolChanged(RunEvent):
    type: Literal[
        "tool.queued", "tool.started", "tool.completed", "tool.yielded", "tool.resolved"
    ]
    data: ToolData


class TaskData(Value):
    brief: TaskBrief


class TaskChanged(RunEvent):
    type: Literal["task.updated"]
    data: TaskData


class DocumentData(Value):
    document: Document


class DocumentChanged(EventEnvelope):
    type: Literal["document.updated"]
    data: DocumentData


class EvidenceData(Value):
    evidence: Evidence


class EvidenceCreated(EventEnvelope):
    type: Literal["evidence.created"]
    data: EvidenceData


class ArtifactData(Value):
    artifact: Artifact


class ArtifactCreated(RunEvent):
    type: Literal["artifact.created"]
    data: ArtifactData


class MemoryData(Value):
    memory: Memory


class MemoryChanged(EventEnvelope):
    type: Literal["memory.updated"]
    data: MemoryData


class ContextCompactedData(Value):
    method: Literal["cleanup", "summary"] = "summary"
    stage: Literal[
        "tool_result_budget",
        "history_snip",
        "microcompact",
        "context_collapse",
        "autocompact",
    ] = "autocompact"
    through_message_id: str | None = None
    evidence_ids: tuple[str, ...]
    estimated_tokens_before: int
    estimated_tokens_after: int
    summary_tokens: int
    summarized_messages: int
    retained_messages: int
    scope: Literal["root", "worker"] = "root"
    trigger_reason: str = "context_threshold"
    cleaned_tool_results: int = 0
    estimated_tokens_after_cleanup: int = 0
    summary_input_estimated_tokens: int = 0
    summary_output_estimated_tokens: int = 0
    net_reclaimed_tokens: int = 0
    duration_ms: int = 0
    context_revision: int = 0


class ContextCompacted(RunEvent):
    type: Literal["context.compacted"]
    data: ContextCompactedData


class ContextCompactionFailedData(Value):
    scope: Literal["root", "worker"]
    stage: Literal["autocompact", "context_collapse"] = "autocompact"
    trigger_reason: str
    failure_reason: str
    estimated_tokens_before: int
    estimated_tokens_after_cleanup: int
    summary_input_estimated_tokens: int
    summary_output_estimated_tokens: int
    duration_ms: int
    consecutive_failures: int = 0


class ContextCompactionFailed(RunEvent):
    type: Literal["context.compaction_failed"]
    data: ContextCompactionFailedData


class OrchestrationData(Value):
    worker_facets: tuple[str, ...] = ()
    worker_count: int = 0


class OrchestrationChanged(RunEvent):
    type: Literal["orchestration.planned"]
    data: OrchestrationData


class SkillUsedData(Value):
    name: str
    version: str
    action: Literal["load", "reference"]
    file_id: str
    reference: str | None = None


class SkillUsed(RunEvent):
    type: Literal["skill.used"]
    data: SkillUsedData


class MemoryRecalledData(Value):
    query: str
    revisions: dict[str, int]


class MemoryRecalled(RunEvent):
    type: Literal["memory.recalled"]
    data: MemoryRecalledData


class BudgetPressureData(Value):
    phase: Literal["research", "delivery"]
    seconds_remaining: int
    tokens_used: int
    inflight_estimate: int
    soft_token_target: int


class BudgetPressure(RunEvent):
    type: Literal["budget.pressure"]
    data: BudgetPressureData


class ResearchIntent(Value):
    current_date_utc: str = ""
    input_revision: int = 0
    user_updates: tuple[str, ...] = ()
    original_request: str
    current_request: str = ""
    task: TaskBrief
    constraints: SearchConstraints
    paper_selection_policy: dict[str, object] = Field(default_factory=dict)


class RetrievalCandidatesData(Value):
    intent: ResearchIntent
    candidates: tuple[SearchHit, ...] = ()
    candidate_count: int = 0
    eligible_count: int = 0
    pending_count: int = 0
    diagnostics: tuple[SourceDiagnostic, ...]


class RankingBatchData(Value):
    candidate_ids: tuple[str, ...]
    evaluated_count: int
    duration_ms: int
    error: ExecutionError | None = None


class RankingBatch(RunEvent):
    type: Literal["retrieval.batch"]
    data: RankingBatchData


class RetrievalCandidates(RunEvent):
    type: Literal["retrieval.candidates"]
    data: RetrievalCandidatesData


class RetrievalCompletedData(Value):
    intent: ResearchIntent
    result: SearchResult


class RetrievalCompleted(RunEvent):
    type: Literal["retrieval.completed"]
    data: RetrievalCompletedData


class ResearchData(Value):
    research: ResearchBoard


class ResearchChanged(RunEvent):
    type: Literal["research.updated"]
    data: ResearchData


class PaperResolvedData(Value):
    paper: Paper
    source: Literal["resolution", "citation", "web_search"]
    identifier: str | None = None


class PaperResolved(RunEvent):
    type: Literal["paper.resolved"]
    data: PaperResolvedData


SessionEvent = Annotated[
    SessionChanged
    | RunChanged
    | RunStarted
    | MessageCompleted
    | ModelStarted
    | MessageDelta
    | ModelFinished
    | CitationsChecked
    | ModelRetry
    | ToolChanged
    | TaskChanged
    | DocumentChanged
    | EvidenceCreated
    | ArtifactCreated
    | MemoryChanged
    | ContextCompacted
    | ContextCompactionFailed
    | OrchestrationChanged
    | SkillUsed
    | MemoryRecalled
    | BudgetPressure
    | RetrievalCandidates
    | RankingBatch
    | RetrievalCompleted
    | ResearchChanged
    | PaperResolved,
    Field(discriminator="type"),
]
EVENT_ADAPTER = TypeAdapter(SessionEvent)
