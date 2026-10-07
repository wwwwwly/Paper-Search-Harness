from typing import Any, Literal

from pydantic import BaseModel, Field

from paper_search.domain import Effort, ExecutionSettings, Value


class CreateSession(BaseModel):
    title: str = Field(default="新研究", max_length=200)
    effort: Effort | None = None


class SendMessage(BaseModel):
    text: str = Field(min_length=1, max_length=30000)
    client_id: str = Field(min_length=1, max_length=100)
    document_ids: tuple[str, ...] = ()
    effort: Effort | None = None


class ResumeRun(BaseModel):
    extend_budget: bool = False


class DownloadDocument(BaseModel):
    session_id: str
    url: str
    name: str = "paper.pdf"
    paper_id: str | None = None


class EditMemory(BaseModel):
    content: str = Field(min_length=1, max_length=5000)
    restore: bool = False


class CreateMemory(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=5000)
    session_id: str | None = None
    kind: Literal["preference", "research", "experience"] = "research"


class ModelInfo(Value):
    id: str
    model: str
    configured: bool
    context_window: int
    max_output_tokens_supported: int
    capability_source: str
    supported_reasoning_efforts: tuple[str, ...] = ()
    features: tuple[str, ...] = ()


class ConnectionInfo(Value):
    name: str
    status: Literal["connected", "error"]
    tools: tuple[str, ...] = ()
    available_tools: tuple[str, ...] = ()
    error: str | None = None


class Configuration(Value):
    default_effort: Effort
    efforts: tuple[ExecutionSettings, ...]
    models: tuple[ModelInfo, ...]
    embedding_enabled: bool
    providers: tuple[str, ...]
    mcp: tuple[ConnectionInfo, ...]


class ToolInfo(Value):
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    parallel_safe: bool
    replay_safe: bool
    capability: Literal["search", "read", "write", "planning", "delegation", "other"]


class ToolCatalog(Value):
    connections: tuple[ConnectionInfo, ...]
    tools: tuple[ToolInfo, ...]
