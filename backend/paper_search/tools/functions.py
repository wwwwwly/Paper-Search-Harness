"""Small function registry. Schemas and validation are provided by Pydantic."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, TypeAdapter, validate_call

from paper_search.domain import ToolResult


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    invoke: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    capability: Literal[
        "search", "read", "write", "planning", "delegation", "other"
    ] = "other"
    output_schema: dict[str, Any] | None = None
    parallel_safe: bool = False
    replay_safe: bool = False
    cacheable: bool = False
    timeout_seconds: float = 60
    enabled: bool = True
    external_schema: bool = True


def tool_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = TypeAdapter(Any).dump_python(value, mode="json")
    return ToolResult(
        content=value
        if isinstance(value, str)
        else json.dumps(encoded, ensure_ascii=False),
        data=encoded,
    )


_SCHEMAS: dict[Callable, dict] = {}
_VALIDATORS: dict[Callable, Callable] = {}


_EXTERNAL_VALIDATORS = {}


def external_validator(schema):
    key = id(schema)
    if key not in _EXTERNAL_VALIDATORS:
        Draft202012Validator.check_schema(schema)
        _EXTERNAL_VALIDATORS[key] = Draft202012Validator(schema)
    return _EXTERNAL_VALIDATORS[key]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        function: Callable,
        *,
        name: str | None = None,
        description: str | None = None,
        parallel_safe: bool = False,
        replay_safe: bool = False,
        cacheable: bool | None = None,
        timeout_seconds: float = 60,
        capability: Literal[
            "search", "read", "write", "planning", "delegation", "other"
        ] = "other",
    ) -> ToolSpec:
        source = function.__func__ if inspect.ismethod(function) else function
        schema = _SCHEMAS.get(source)
        if schema is None:
            adapter = TypeAdapter(function, config=ConfigDict(defer_build=True))
            adapter.rebuild(_types_namespace=getattr(source, "__globals__", {}))
            schema = adapter.json_schema()
            if schema.get("type") != "object":
                raise ValueError("tools must use named, typed function parameters")
            _SCHEMAS[source] = schema
        if source not in _VALIDATORS:
            _VALIDATORS[source] = validate_call(source, validate_return=False)
        validated = _VALIDATORS[source]
        positional = (function.__self__,) if inspect.ismethod(function) else ()

        async def invoke(arguments: dict[str, Any]) -> ToolResult:
            if inspect.iscoroutinefunction(function):
                value = await validated(*positional, **arguments)
            else:
                value = await asyncio.to_thread(validated, *positional, **arguments)
            return tool_result(value)

        spec = ToolSpec(
            external_schema=False,
            name=name or function.__name__,
            description=description or inspect.getdoc(function) or function.__name__,
            input_schema=schema,
            invoke=invoke,
            capability=capability,
            parallel_safe=parallel_safe,
            replay_safe=replay_safe,
            cacheable=(
                replay_safe and capability in {"search", "read", "other"}
                if cacheable is None
                else cacheable
            ),
            timeout_seconds=timeout_seconds,
        )
        self.add(spec)
        return spec

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        if spec.external_schema:
            external_validator(spec.input_schema)
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools or not self._tools[name].enabled:
            raise ValueError(f"tool not enabled: {name}")
        return self._tools[name]

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def definitions(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in self._tools.values()
            if tool.enabled
        )
