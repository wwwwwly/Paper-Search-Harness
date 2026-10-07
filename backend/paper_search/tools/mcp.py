from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from paper_search.config.settings import MCPConfig
from paper_search.domain import SourceContent, ToolResult
from paper_search.tools.functions import ToolRegistry, ToolSpec


def search_cards(value) -> list[dict]:
    """Normalize structured web-search hits into provider-neutral paper cards."""
    if isinstance(value, str):
        return _search_cards_from_text(value)
    results = value.get("results") if isinstance(value, dict) else None
    if not isinstance(results, list):
        return []
    cards = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        if not isinstance(title, str) or not isinstance(url, str):
            continue
        cards.append(
            {
                "title": title.strip(),
                "url": url.strip(),
                "snippet": str(
                    item.get("text")
                    or item.get("summary")
                    or item.get("highlights")
                    or ""
                )[:2000],
                "published_date": item.get("publishedDate")
                or item.get("published_date"),
                "authors": item.get("authors") or item.get("author") or [],
                "venue": item.get("venue"),
            }
        )
    return cards


def _search_cards_from_text(text: str) -> list[dict]:
    """Parse the stable Title/URL headers used by search MCP text responses."""
    starts = list(re.finditer(r"(?m)^Title:\s*(.+)\r?\nURL:\s*(\S+)", text))
    cards = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[match.end() : end]
        published = re.search(r"(?m)^Published:\s*(.+)$", block)
        author = re.search(r"(?m)^Author:\s*(.+)$", block)
        highlights = re.search(r"(?ms)^Highlights:\s*\n(.+?)(?:\n\n|\Z)", block)
        snippet = (highlights.group(1) if highlights else block).strip()
        authors = []
        if author and author.group(1).strip() not in {"", "N/A"}:
            authors = [
                item.strip()
                for item in re.split(r"\s*(?:,|;|\band\b)\s*", author.group(1))
                if item.strip()
            ]
        cards.append(
            {
                "title": match.group(1).strip(),
                "url": match.group(2).strip(),
                "snippet": " ".join(snippet.split())[:2000],
                "published_date": published.group(1).strip() if published else None,
                "authors": authors,
                "venue": None,
            }
        )
    return cards


def mcp_tool_name(connection: str, remote: str) -> str:
    qualified = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp_{connection}_{remote}")
    if len(qualified) > 64:
        raise ValueError(
            f"MCP tool name exceeds 64 characters after normalization: {qualified}"
        )
    return qualified


def exa_pages(arguments: dict, text: str) -> tuple[SourceContent, ...]:
    urls = arguments.get("urls")
    if not isinstance(urls, list):
        return ()
    requested = {url.rstrip("/") for url in urls}
    headers = list(
        re.finditer(
            r"^# [^\r\n]+\r?\nURL: (https?://[^\s]+)\r?\n"
            r"(?:(?:Published|Author|Updated):[^\r\n]*\r?\n)*\r?\n",
            text,
            re.MULTILINE,
        )
    )
    pages = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[header.end() : end].strip()
        url = header[1].rstrip("/")
        if url in requested and body:
            pages[url] = SourceContent(url=header[1], text=body)
    return tuple(pages.values())


@asynccontextmanager
async def _http_transport(config: MCPConfig):
    async with (
        httpx2.AsyncClient(
            headers=config.headers, timeout=config.timeout_seconds
        ) as http,
        streamable_http_client(config.url, http_client=http) as streams,
    ):
        yield streams[0], streams[1]


class _Connection:
    """One lifecycle task owns SDK context managers, including reconnect/close."""

    def __init__(self, config):
        self.config = config
        self.client = None
        self.task = None
        self.stop = None
        self.lock = asyncio.Lock()
        self.discovered = []
        self.status = {
            "name": config.name,
            "status": "error",
            "tools": [],
            "available_tools": [],
        }

    async def _serve(self, ready):
        config = self.config
        try:
            transport = (
                StdioServerParameters(
                    command=config.command,
                    args=list(config.args),
                    env={**os.environ, **config.env},
                )
                if config.transport == "stdio"
                else _http_transport(config)
            )
            async with Client(
                transport, read_timeout_seconds=config.timeout_seconds
            ) as client:
                self.client = client
                discovered, seen, cursor = [], set(), None
                while True:
                    page = await client.list_tools(cursor=cursor)
                    discovered.extend(page.tools)
                    cursor = page.next_cursor
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError("MCP tool pagination repeated a cursor")
                    seen.add(cursor)
                self.discovered = discovered
                self.status.update(
                    status="connected",
                    available_tools=[tool.name for tool in discovered],
                )
                self.status.pop("error", None)
                ready.set_result(None)
                await self.stop.wait()
        except Exception as exc:  # noqa: BLE001 - report third-party lifecycle failure without credentials
            self.status.update(status="error", error=type(exc).__name__)
            if not ready.done():
                ready.set_exception(exc)
        finally:
            self.client = None

    async def connect(self, failed_client=None):
        async with self.lock:
            if self.client is not None and self.client is not failed_client:
                return
            if self.task:
                self.stop.set()
                await self.task
            self.stop = asyncio.Event()
            ready = asyncio.get_running_loop().create_future()
            self.task = asyncio.create_task(
                self._serve(ready), name=f"mcp-{self.config.name}"
            )
            await ready

    async def call(self, remote, arguments, safe):
        for attempt in range(2):
            await self.connect()
            client = self.client
            try:
                result = await client.call_tool(
                    remote, arguments, read_timeout_seconds=self.config.timeout_seconds
                )
                self.status.update(status="connected")
                self.status.pop("error", None)
                return result
            except Exception as exc:
                self.status.update(status="error", error=type(exc).__name__)
                if attempt or not safe:
                    raise
                await self.connect(failed_client=client)

    async def close(self):
        async with self.lock:
            if self.task:
                self.stop.set()
                await self.task
                self.task = None


class MCPConnections:
    def __init__(self, configs: tuple[MCPConfig, ...]):
        self.connections = [_Connection(config) for config in configs]
        self.tools: list[ToolSpec] = []
        self.status = [connection.status for connection in self.connections]

    async def start(self):
        for connection in self.connections:
            try:
                await connection.connect()
            except Exception:  # noqa: BLE001,S112 - connection records the failure in public status
                continue  # Explicit failed status is exposed in configuration/health.
            config = connection.config
            for tool in connection.discovered:
                if tool.name not in config.tools:
                    continue
                safe = tool.name in config.read_only_tools

                async def invoke(
                    arguments, *, remote=tool.name, current=connection, replay_safe=safe
                ):
                    result = await current.call(remote, arguments, replay_safe)
                    blocks = [
                        block.model_dump(mode="json", by_alias=True)
                        for block in result.content
                    ]
                    text = "\n".join(
                        block.get("text", "")
                        for block in blocks
                        if block.get("type") == "text"
                    )
                    if result.structured_content is not None:
                        text += "\nStructured result:\n" + json.dumps(
                            result.structured_content, ensure_ascii=False
                        )
                    cards = []
                    if "search" in remote.casefold() and not result.is_error:
                        cards = search_cards(result.structured_content) or search_cards(
                            text
                        )
                    if cards:
                        text = json.dumps({"paper_cards": cards}, ensure_ascii=False)
                    sources = ()
                    if (
                        urlparse(current.config.url or "").hostname == "mcp.exa.ai"
                        and remote == "web_fetch_exa"
                        and not result.is_error
                        and isinstance(arguments.get("urls"), list)
                        and text.strip()
                    ):
                        sources = exa_pages(arguments, text)
                        if sources:
                            text = f"已读取 {len(sources)}/{len(arguments['urls'])} 个页面，原文保存在下列来源中。"
                        else:
                            text = (
                                "未自动绑定网页引用；以下抓取结果仍可阅读，引用时使用可靠的原始链接。\n"
                                + text
                            )
                    return ToolResult(
                        content=text or json.dumps(blocks, ensure_ascii=False),
                        data={
                            "content": blocks,
                            "structured_content": result.structured_content,
                            "paper_cards": cards,
                        },
                        sources=sources,
                        is_error=bool(result.is_error),
                        error_code="mcp_error" if result.is_error else None,
                    )

                qualified = mcp_tool_name(config.name, tool.name)
                capability = (
                    "search"
                    if "search" in tool.name.casefold()
                    else "read"
                    if any(
                        word in tool.name.casefold()
                        for word in ("fetch", "read", "get", "list")
                    )
                    else "other"
                )
                self.tools.append(
                    ToolSpec(
                        name=qualified,
                        description=tool.description or tool.name,
                        input_schema=tool.input_schema,
                        output_schema=tool.output_schema,
                        invoke=invoke,
                        capability=capability,
                        parallel_safe=safe,
                        replay_safe=safe,
                        cacheable=safe and capability in {"search", "read"},
                        timeout_seconds=config.timeout_seconds * (2 if safe else 1),
                    )
                )
                connection.status["tools"].append(qualified)

    def register(self, registry: ToolRegistry, *, read_only: bool = False):
        for tool in self.tools:
            if not read_only or tool.replay_safe:
                registry.add(tool)

    async def close(self):
        await asyncio.gather(*(connection.close() for connection in self.connections))
