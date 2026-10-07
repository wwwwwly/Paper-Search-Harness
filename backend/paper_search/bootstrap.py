"""The production composition root: select adapters here, never in business code."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
from fastapi import FastAPI
from filelock import FileLock

from paper_search.api.workbench import create_app
from paper_search.config.settings import Settings, load_settings
from paper_search.documents.download import HttpDocumentDownloader
from paper_search.documents.parser import PdfParser
from paper_search.documents.service import DocumentService
from paper_search.domain import Run, TaskBrief
from paper_search.harness.agent import AgentRunner
from paper_search.harness.conversation import ContextBuilder
from paper_search.harness.jobs import JobWorker
from paper_search.intent import require_research
from paper_search.memory import MemoryService
from paper_search.model_client import OpenAIEmbeddingClient, OpenAIModelClient
from paper_search.ports import AgentCallbacks
from paper_search.queries import WorkbenchQueries
from paper_search.reports import ReportService
from paper_search.research import ResearchService
from paper_search.retrieval.arxiv import ArxivProvider
from paper_search.retrieval.catalog import PaperCatalog
from paper_search.retrieval.crossref import CrossrefResolver
from paper_search.retrieval.engine import RetrievalService
from paper_search.retrieval.web import WebPaperResolver
from paper_search.session_service import SessionService
from paper_search.skills import SkillCatalog, SkillTools
from paper_search.storage.artifacts import LocalArtifactStore
from paper_search.storage.index import SqliteSearchIndex
from paper_search.storage.sql import LocalDeliveryQueue, SqlStore
from paper_search.tools.documents import DocumentTools
from paper_search.tools.functions import ToolRegistry, ToolSpec
from paper_search.tools.mcp import MCPConnections
from paper_search.tools.memory import MemoryTools
from paper_search.tools.papers import PaperTools
from paper_search.tools.session import SessionTools


@dataclass
class ApplicationRuntime:
    settings: Settings
    store: SqlStore
    artifacts: LocalArtifactStore
    sessions: SessionService
    documents: DocumentService
    memory: MemoryService
    worker: JobWorker
    runner: AgentRunner
    skills: SkillCatalog
    mcp: MCPConnections
    queries: WorkbenchQueries
    tool_catalog: tuple[ToolSpec, ...]
    resources: AsyncExitStack = field(default_factory=AsyncExitStack)

    async def close(self):
        try:
            await self.worker.close()
        finally:
            await self.resources.aclose()

    async def refresh_model_capabilities(self) -> Settings:
        from paper_search.config.capabilities import resolve_model_capabilities

        async with httpx.AsyncClient(
            timeout=self.settings.provider_timeout_seconds,
            follow_redirects=True,
        ) as client:
            updated = await resolve_model_capabilities(self.settings, client)
        self.settings = updated
        self.sessions.execution_for = updated.execution
        for name, profile in updated.models.items():
            model = self.runner.model_for(name)
            if isinstance(model, OpenAIModelClient):
                model.profile = profile
        return updated


async def build_runtime(settings: Settings) -> ApplicationRuntime:
    async with AsyncExitStack() as resources:
        runtime = await _assemble(settings, resources)
        runtime.resources = resources.pop_all()
        return runtime


async def _assemble(
    settings: Settings, resources: AsyncExitStack
) -> ApplicationRuntime:
    store = SqlStore(settings.database_url)
    resources.push_async_callback(store.close)
    await store.initialize()
    artifacts = LocalArtifactStore(settings.data_dir / "artifacts")
    index = SqliteSearchIndex(settings.data_dir / "index.db")
    await index.initialize()
    http = httpx.AsyncClient(
        timeout=settings.provider_timeout_seconds, follow_redirects=True
    )
    resources.push_async_callback(http.aclose)
    models = {}
    for name, profile in settings.models.items():
        models[name] = OpenAIModelClient(profile, artifacts)
        resources.push_async_callback(models[name].close)
    embedding = (
        OpenAIEmbeddingClient(settings.embedding)
        if settings.embedding.enabled
        else None
    )
    if embedding:
        resources.push_async_callback(embedding.close)
    mcp = MCPConnections(settings.mcp)
    resources.push_async_callback(mcp.close)
    await mcp.start()
    providers = {"arxiv": ArxivProvider(http, settings.arxiv_base_url)}
    unknown = set(settings.providers) - set(providers)
    if unknown:
        raise ValueError(f"Unknown paper providers: {unknown}")
    enabled_providers = {name: providers[name] for name in settings.providers}
    retrieval = RetrievalService(
        enabled_providers,
        store.uow,
        timeout=settings.provider_timeout_seconds,
    )
    paper_catalog = PaperCatalog(
        store.uow,
        (providers["arxiv"], WebPaperResolver(http)),
    )
    memory = MemoryService(store.uow, index, embedding)
    documents = DocumentService(
        uow=store.uow,
        artifacts=artifacts,
        parser=PdfParser(
            timeout=settings.document_timeout_seconds, max_pages=settings.max_pdf_pages
        ),
        index=index,
        downloader=HttpDocumentDownloader(http),
        embedding=embedding,
        max_bytes=settings.max_pdf_bytes,
    )

    research = ResearchService(store.uow)
    skills = SkillCatalog(Path(__file__).parent / "skills", store.uow, artifacts)

    def tools_for(run, callbacks):
        registry = ToolRegistry()
        paper_tools = PaperTools(
            run, store.uow, retrieval, paper_catalog, callbacks, CrossrefResolver(http)
        )
        document_tools = DocumentTools(run, store.uow, documents)
        memory_tools = MemoryTools(run, store.uow, memory)
        session_tools = SessionTools(
            run, store.uow, artifacts, callbacks, research, memory
        )
        skill_tools = SkillTools(run, skills)
        for function in (
            paper_tools.get_paper,
            document_tools.search_documents,
            document_tools.read_document,
            session_tools.read_artifact,
            memory_tools.search_memory,
            memory_tools.read_memory,
            skill_tools.load_skill,
            skill_tools.read_skill_reference,
        ):
            registry.register(
                function,
                replay_safe=True,
                cacheable=False,
                capability="read",
                parallel_safe=True,
                timeout_seconds=180,
            )
        registry.register(
            paper_tools.enrich_papers,
            replay_safe=True,
            cacheable=False,
            capability="search",
            timeout_seconds=60,
        )
        registry.register(
            paper_tools.resolve_papers,
            replay_safe=True,
            cacheable=False,
            capability="search",
            timeout_seconds=180,
        )
        if enabled_providers:
            registry.register(
                paper_tools.search_papers,
                replay_safe=True,
                cacheable=False,
                capability="search",
                timeout_seconds=180,
            )
        registry.register(
            session_tools.record_findings,
            replay_safe=True,
            cacheable=False,
            capability="write",
        )
        registry.register(
            document_tools.download_pdf,
            replay_safe=True,
            cacheable=False,
            capability="write",
        )
        registry.register(
            document_tools.wait_for_documents,
            replay_safe=True,
            cacheable=False,
            capability="read",
        )
        if not run.parent_id:
            registry.register(
                document_tools.bind_document_paper,
                replay_safe=True,
                cacheable=False,
                capability="write",
            )
            registry.register(
                session_tools.update_task,
                replay_safe=True,
                cacheable=False,
                capability="planning",
            )
            registry.register(
                session_tools.submit_research,
                replay_safe=True,
                cacheable=False,
                capability="write",
            )
            registry.register(memory_tools.remember_preference, capability="write")
            registry.register(
                ReportService(run, store.uow, artifacts).create_report,
                replay_safe=True,
                cacheable=False,
                capability="write",
            )
            if run.execution.effort == "deep":
                for function in (
                    session_tools.spawn_research_task,
                    session_tools.collect_tasks,
                    session_tools.send_task_update,
                    session_tools.cancel_research_task,
                ):
                    registry.register(
                        function,
                        replay_safe=True,
                        cacheable=False,
                        capability="delegation",
                        timeout_seconds=35,
                    )
        for spec in mcp.tools:
            if run.parent_id and not spec.replay_safe:
                continue
            if spec.capability in {"search", "read"}:
                invoke = spec.invoke

                async def invoke_after_sync(arguments, *, current=invoke):
                    await require_research(
                        store.uow, run, unavailable_message="请先确定研究问题"
                    )
                    return await current(arguments)

                spec = replace(spec, invoke=invoke_after_sync)
            registry.add(spec)
        return registry

    prompt_dir = Path(__file__).parent / "prompts"
    common_prompt = (prompt_dir / "common.md").read_text(encoding="utf-8")
    worker_prompt = (
        common_prompt + "\n\n" + (prompt_dir / "worker.md").read_text(encoding="utf-8")
    )
    summary_prompt = (prompt_dir / "summary.md").read_text(encoding="utf-8")
    runner = AgentRunner(
        uow=store.uow,
        artifacts=artifacts,
        model_for=models.__getitem__,
        tools_for=tools_for,
        stop_documents=documents.cancel_owned,
        context=ContextBuilder(
            store.uow,
            common_prompt,
            artifacts,
            worker_prompt=worker_prompt,
            skills=skills,
            memory=memory,
            summary_prompt=summary_prompt,
            strategy=settings.context_compression.strategy,
            idle_ttl_seconds=settings.context_compression.idle_ttl_seconds,
        ),
    )
    worker = JobWorker(
        store.uow,
        LocalDeliveryQueue(store),
        {
            "run": runner.execute,
            "document": documents.process,
            "memory": memory.process,
        },
    )
    documents.interrupt_job = worker.cancel
    sessions = SessionService(store.uow, worker.cancel, settings.execution)

    async def catalog_only(*_args):
        raise RuntimeError("Tool catalogue does not execute tools")

    tool_catalog = tools_for(
        Run(
            session_id="catalog",
            execution=settings.execution("deep"),
            brief=TaskBrief(input_revision=0, open_questions=("catalog",)),
        ),
        AgentCallbacks(
            catalog_only, catalog_only, catalog_only, catalog_only, catalog_only
        ),
    ).specs()
    return ApplicationRuntime(
        settings,
        store,
        artifacts,
        sessions,
        documents,
        memory,
        worker,
        runner,
        skills,
        mcp,
        WorkbenchQueries(store.uow, artifacts),
        tool_catalog,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.data_dir / "server.lock"), timeout=0):
        runtime = await build_runtime(settings)
        app.state.runtime = runtime
        try:
            await runtime.worker.start()
            yield
        finally:
            await runtime.close()


def create_production_app() -> FastAPI:
    return create_app(lifespan)
