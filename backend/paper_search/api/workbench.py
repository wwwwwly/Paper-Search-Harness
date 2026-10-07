from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from paper_search.api.contracts import (
    Configuration,
    CreateMemory,
    CreateSession,
    DownloadDocument,
    EditMemory,
    ModelInfo,
    ResumeRun,
    SendMessage,
    ToolCatalog,
)
from paper_search.domain import (
    Artifact,
    Conflict,
    Document,
    Memory,
    Message,
    MissingRecord,
    Paper,
    Run,
    Session,
    SourceSnapshotView,
    ToolResult,
    now,
)
from paper_search.events import SessionEvent
from paper_search.queries import RunTrace, SessionSnapshot


def create_app(lifespan) -> FastAPI:
    app = FastAPI(title="Paper Search Workbench", version="0.3.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    def runtime(request: Request):
        return request.app.state.runtime

    @app.exception_handler(MissingRecord)
    async def missing(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/api/skills", response_model=list[dict[str, str]])
    async def skill_catalog(request: Request):
        return runtime(request).skills.catalog()

    @app.get("/api/runs/{run_id}/model-calls/{call_id}/{view}")
    async def model_view(
        run_id: str,
        call_id: str,
        view: Literal["request", "response"],
        request: Request,
    ):
        content = await runtime(request).queries.model_view(run_id, call_id, view)
        return Response(
            content=content,
            media_type="application/json" if view == "request" else "text/plain",
        )

    @app.get("/api/health")
    async def health(request: Request):
        rt = runtime(request)
        return {
            "status": "ok",
            "jobs": await rt.queries.jobs_health(),
            "index_mode": "hybrid" if rt.settings.embedding.enabled else "keyword",
            "mcp": rt.mcp.status,
        }

    @app.get("/api/config", response_model=Configuration)
    async def config(request: Request):
        rt = runtime(request)
        return {
            "default_effort": rt.settings.default_effort,
            "efforts": [rt.settings.execution(name) for name in rt.settings.efforts],
            "models": [
                {
                    "id": name,
                    "model": profile.model,
                    "configured": bool(profile.api_key),
                    "context_window": profile.context_window,
                    "max_output_tokens_supported": profile.max_output_tokens_supported,
                    "capability_source": profile.capability_source,
                    "supported_reasoning_efforts": profile.supported_reasoning_efforts,
                    "features": profile.features,
                }
                for name, profile in rt.settings.models.items()
            ],
            "providers": rt.settings.providers,
            "embedding_enabled": rt.settings.embedding.enabled,
            "mcp": rt.mcp.status,
        }

    @app.post("/api/config/models/refresh", response_model=list[ModelInfo])
    async def refresh_models(request: Request):
        rt = runtime(request)
        settings = await rt.refresh_model_capabilities()
        return [
            {
                "id": name,
                "model": profile.model,
                "configured": bool(profile.api_key),
                "context_window": profile.context_window,
                "max_output_tokens_supported": profile.max_output_tokens_supported,
                "capability_source": profile.capability_source,
                "supported_reasoning_efforts": profile.supported_reasoning_efforts,
                "features": profile.features,
            }
            for name, profile in settings.models.items()
        ]

    @app.get("/api/sessions", response_model=list[Session])
    async def sessions_list(request: Request):
        return await runtime(request).queries.sessions()

    @app.post("/api/sessions", response_model=Session, status_code=201)
    async def sessions_create(payload: CreateSession, request: Request):
        rt = runtime(request)
        effort = payload.effort or rt.settings.default_effort
        if effort not in rt.settings.efforts:
            raise ValueError("未知研究档位")
        return await rt.sessions.create(payload.title, effort)

    @app.get("/api/sessions/{session_id}", response_model=SessionSnapshot)
    async def session_snapshot(session_id: str, request: Request):
        return await runtime(request).queries.snapshot(session_id)

    @app.get("/api/sessions/{session_id}/messages", response_model=list[Message])
    async def session_messages(
        session_id: str,
        request: Request,
        before: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ):
        return await runtime(request).queries.messages(session_id, before, limit)

    @app.post(
        "/api/sessions/{session_id}/messages", response_model=Message, status_code=202
    )
    async def message_send(session_id: str, payload: SendMessage, request: Request):
        return await runtime(request).sessions.send(
            session_id,
            payload.text,
            payload.client_id,
            document_ids=payload.document_ids,
            effort=payload.effort,
        )

    @app.get(
        "/api/sessions/{session_id}/events",
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def event_stream(session_id: str, request: Request, after: int = 0):
        rt = runtime(request)
        await rt.queries.session(session_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "invalid event cursor")

        async def stream():
            nonlocal cursor
            idle = 0
            while not await request.is_disconnected():
                events = await rt.queries.events(session_id, cursor)
                for event in events:
                    cursor = event.seq
                    yield f"id: {event.seq}\nevent: update\ndata: {event.model_dump_json()}\n\n"
                if not events:
                    idle += 1
                    if idle % 40 == 0:
                        yield ": heartbeat\n\n"
                    await asyncio.sleep(0.02)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/sessions/{session_id}/event-history", response_model=list[SessionEvent]
    )
    async def event_history(session_id: str, request: Request, after: int = 0):
        return await runtime(request).queries.events(session_id, after)

    @app.post("/api/runs/{run_id}/cancel", response_model=Run)
    async def cancel_run(run_id: str, request: Request):
        return await runtime(request).sessions.cancel(run_id)

    @app.get("/api/runs/{run_id}/trace", response_model=RunTrace)
    async def run_trace(run_id: str, request: Request):
        return await runtime(request).queries.run_trace(run_id)

    @app.post("/api/runs/{run_id}/resume", response_model=Run)
    async def resume_run(run_id: str, payload: ResumeRun, request: Request):
        return await runtime(request).sessions.resume(
            run_id, extend_budget=payload.extend_budget
        )

    @app.post("/api/runs/{run_id}/tools/{call_id}/resolve")
    async def resolve_tool(
        run_id: str, call_id: str, payload: ToolResult, request: Request
    ):
        await runtime(request).sessions.resolve_tool(run_id, call_id, result=payload)
        return {"resolved": True}

    @app.get("/api/papers", response_model=list[Paper])
    async def paper_list(request: Request):
        return await runtime(request).queries.papers()

    @app.get("/api/papers/bibtex")
    async def bibliography(request: Request):
        return Response(
            await runtime(request).queries.bibliography(),
            media_type="application/x-bibtex",
            headers={"Content-Disposition": 'attachment; filename="references.bib"'},
        )

    @app.post("/api/documents", response_model=Document, status_code=202)
    async def upload_document(
        request: Request,
        session_id: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        paper_id: Annotated[str | None, Form()] = None,
    ):
        rt = runtime(request)
        data = await file.read(rt.settings.max_pdf_bytes + 1)
        return await rt.documents.upload(
            session_id, file.filename or "paper.pdf", data, paper_id
        )

    @app.post("/api/documents/download", response_model=Document, status_code=202)
    async def download_document(payload: DownloadDocument, request: Request):
        return await runtime(request).documents.download(
            payload.session_id, payload.url, payload.name, payload.paper_id
        )

    @app.get("/api/documents/{document_id}", response_model=Document)
    async def get_document(document_id: str, request: Request):
        return await runtime(request).queries.document(document_id)

    @app.get("/api/documents/{document_id}/search")
    async def search_document(
        document_id: str,
        request: Request,
        query: Annotated[str, Query(min_length=1, max_length=2000)],
    ):
        rt = runtime(request)
        document = await rt.queries.document(document_id)
        return await rt.documents.search(
            document.session_id, query, document_id=document_id
        )

    @app.get("/api/documents/{document_id}/file")
    async def document_file(document_id: str, request: Request):
        rt = runtime(request)
        document = await rt.queries.document(document_id)
        if not document.file_id:
            raise HTTPException(409, "PDF 尚未下载完成")
        return FileResponse(
            rt.artifacts.local_path(document.file_id),
            media_type="application/pdf",
            filename=document.name,
            content_disposition_type="inline",
        )

    @app.post("/api/documents/{document_id}/cancel", response_model=Document)
    async def cancel_document(document_id: str, request: Request):
        rt = runtime(request)
        document = await rt.documents.cancel(document_id)
        rt.worker.cancel(document_id)
        return document

    @app.post("/api/documents/{document_id}/retry", response_model=Document)
    async def retry_document(document_id: str, request: Request):
        return await runtime(request).documents.retry(document_id)

    @app.get("/api/reports", response_model=list[Artifact])
    async def reports(request: Request):
        return await runtime(request).queries.reports()

    @app.get("/api/artifacts/{artifact_id}/content")
    async def artifact_content(artifact_id: str, request: Request):
        rt = runtime(request)
        content, mime_type = await rt.queries.artifact_content(artifact_id)
        return Response(content, media_type=mime_type)

    @app.get("/api/memories", response_model=list[Memory])
    async def memories(request: Request, session_id: str | None = None):
        return await runtime(request).queries.memories(session_id)

    @app.patch("/api/memories/{memory_id}", response_model=Memory)
    async def edit_memory(memory_id: str, payload: EditMemory, request: Request):
        rt = runtime(request)
        memory = await rt.queries.memory(memory_id)
        return await rt.memory.save(
            memory.model_copy(
                update={
                    "content": payload.content,
                    "deleted": False,
                    "updated_at": now(),
                }
            ),
            restore=payload.restore,
        )

    @app.post("/api/memories", response_model=Memory, status_code=201)
    async def create_memory(payload: CreateMemory, request: Request):
        rt = runtime(request)
        if payload.session_id:
            await rt.queries.session(payload.session_id)
        return await rt.memory.save(
            Memory(
                key=payload.key,
                content=payload.content,
                session_id=payload.session_id,
                scope="session" if payload.session_id else "global",
                kind=payload.kind,
            )
        )

    @app.delete("/api/memories/{memory_id}")
    async def delete_memory(memory_id: str, request: Request):
        await runtime(request).memory.delete(memory_id)
        return {"deleted": True}

    @app.post("/api/memories/{memory_id}/reindex", status_code=202)
    async def reindex_memory(memory_id: str, request: Request):
        await runtime(request).memory.reindex(memory_id)
        return {"queued": True}

    @app.get("/api/tools", response_model=ToolCatalog)
    async def tools_list(request: Request):
        rt = runtime(request)
        return {
            "connections": rt.mcp.status,
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                    "output_schema": spec.output_schema,
                    "parallel_safe": spec.parallel_safe,
                    "replay_safe": spec.replay_safe,
                    "capability": spec.capability,
                }
                for spec in rt.tool_catalog
            ],
        }

    @app.get("/api/sources/{snapshot_id}", response_model=SourceSnapshotView)
    async def web_source(snapshot_id: str, request: Request):
        return await runtime(request).queries.web_source(snapshot_id)

    return app
