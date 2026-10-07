from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from paper_search.domain import (
    Effort,
    ExecutionSettings,
    ModelOptions,
    RunLimits,
)


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelProfile(ConfigModel):
    model: str
    base_url: str
    api_key: str = ""
    context_window: int = Field(default=128000, ge=8192)
    max_output_tokens_supported: int = Field(default=128000, ge=1024)
    capabilities_url: str | None = None
    force_max_output_tokens: bool = False
    capability_source: str = "configured"
    supported_reasoning_efforts: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    timeout_seconds: float = 120
    extra_body: dict = Field(default_factory=dict)
    input_cny_per_million: float = Field(default=12, ge=0)
    output_cny_per_million: float = Field(default=27, ge=0)
    cached_input_cny_per_million: float = Field(default=12, ge=0)


class EmbeddingProfile(ConfigModel):
    enabled: bool = False
    model: str = "qwen3.7-text-embedding"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    dimensions: int = 256


class MCPConfig(ConfigModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    tools: tuple[str, ...] = ()
    read_only_tools: tuple[str, ...] = ()
    timeout_seconds: float = 60

    @model_validator(mode="after")
    def transport_options(self):
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP requires command")
        if self.transport == "http" and not self.url:
            raise ValueError("HTTP MCP requires url")
        return self


class EffortProfile(ConfigModel):
    model_profile: str
    reasoning_effort: str
    context_output_headroom: int = Field(ge=1024)
    limits: RunLimits


class ContextCompressionSettings(ConfigModel):
    strategy: Literal["collapse", "autocompact"] = "collapse"
    idle_ttl_seconds: int = Field(default=3600, ge=60)


class Settings(ConfigModel):
    database_url: str = "sqlite+aiosqlite:///./data/research.db"
    data_dir: Path = Path("data")
    default_effort: Effort = "quick"
    models: dict[str, ModelProfile]
    efforts: dict[Effort, EffortProfile]
    embedding: EmbeddingProfile = Field(default_factory=EmbeddingProfile)
    mcp: tuple[MCPConfig, ...] = ()
    providers: tuple[str, ...] = ()
    context_compression: ContextCompressionSettings = Field(
        default_factory=ContextCompressionSettings
    )
    arxiv_base_url: str = "https://export.arxiv.org/api/query"
    provider_timeout_seconds: float = 30
    document_timeout_seconds: int = 300
    max_pdf_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 200

    @model_validator(mode="after")
    def profile_exists(self):
        if self.default_effort not in self.efforts:
            raise ValueError("default_effort must name a configured effort")
        for effort in self.efforts.values():
            if effort.model_profile not in self.models:
                raise ValueError("effort must name a configured model profile")
            model = self.models[effort.model_profile]
            if effort.context_output_headroom >= model.context_window:
                raise ValueError("context output headroom must be below context window")
            if (
                model.supported_reasoning_efforts
                and effort.reasoning_effort not in model.supported_reasoning_efforts
            ):
                raise ValueError(
                    f"reasoning effort {effort.reasoning_effort!r} is not supported "
                    f"by {model.model}"
                )
        return self

    def execution(self, effort: Effort) -> ExecutionSettings:
        choice = self.efforts[effort]
        model = self.models[choice.model_profile]
        return ExecutionSettings(
            effort=effort,
            profile=choice.model_profile,
            limits=choice.limits,
            model=ModelOptions(
                **model.model_dump(
                    exclude={"api_key", "timeout_seconds", "capabilities_url"}
                ),
                context_output_headroom=choice.context_output_headroom,
                reasoning_effort=choice.reasoning_effort,
            ),
        )


def load_settings(path: str | Path | None = None) -> Settings:
    path = path or os.getenv("PAPER_SEARCH_CONFIG")
    config = Path(path) if path else Path(__file__).with_name("default.yaml")
    values = yaml.safe_load(config.read_text(encoding="utf-8"))
    if api_key := os.getenv("PAPER_SEARCH_MODEL_API_KEY"):
        for profile in values.get("models", {}).values():
            profile["api_key"] = api_key
    if api_key := os.getenv("PAPER_SEARCH_EMBEDDING_API_KEY"):
        values.setdefault("embedding", {})["api_key"] = api_key
    return Settings.model_validate(values)
