"""Application read service. HTTP never touches repositories or database objects."""

from datetime import datetime

from pydantic import Field

from paper_search.domain import (
    Artifact,
    Document,
    Evidence,
    ExecutionError,
    Memory,
    Message,
    ResearchBoard,
    Run,
    RunStatus,
    SearchResult,
    Session,
    SourceSnapshotView,
    StreamingMessage,
    ToolExecution,
    Usage,
    Value,
)
from paper_search.events import RetrievalCandidatesData, SessionEvent
from paper_search.ports import ArtifactStore, UowFactory
from paper_search.reports import bibtex


class ModelTrace(Value):
    id: str
    run_id: str
    request_file: str = ""
    response_file: str | None = None
    purpose: str
    parent_tool_call_id: str | None = None
    model: str = ""
    reasoning_effort: str = ""
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    first_text_ms: int | None = None
    status: str = "running"
    usage: Usage = Field(default_factory=Usage)
    error: ExecutionError | None = None
    provider_request_id: str | None = None
    finish_reason: str | None = None


def project_model_event(calls: dict[str, ModelTrace], event: SessionEvent):
    if event.type == "run.started":
        for key, call in list(calls.items()):
            if call.run_id == event.run_id and call.status == "running":
                calls[key] = call.model_copy(update={"status": "interrupted"})
    elif event.type == "model.started":
        data = event.data
        calls[data.stream_id] = ModelTrace(
            id=data.stream_id,
            run_id=event.run_id,
            purpose=data.purpose,
            request_file=data.request_file,
            parent_tool_call_id=data.parent_tool_call_id,
            model=data.model,
            reasoning_effort=data.reasoning_effort,
            started_at=data.created_at,
        )
    elif event.type == "model.finished" and event.data.stream_id in calls:
        data = event.data
        calls[data.stream_id] = calls[data.stream_id].model_copy(
            update={
                "finished_at": event.created_at,
                "response_file": data.response_file,
                "duration_ms": data.duration_ms,
                "first_text_ms": data.first_text_ms,
                "status": "completed" if data.completed else "failed",
                "usage": data.usage,
                "error": data.error,
                "provider_request_id": data.provider_request_id,
                "finish_reason": data.finish_reason,
            }
        )


class RunTrace(Value):
    run: Run
    models: list[ModelTrace]
    tools: list[ToolExecution]


class SessionSnapshot(Value):
    session: Session
    messages: list[Message]
    runs: list[Run]
    documents: list[Document]
    artifacts: list[Artifact]
    evidence: list[Evidence]
    event_cursor: int
    tools: list[ToolExecution]
    streams: dict[str, StreamingMessage]
    research: ResearchBoard
    retrieval: SearchResult | None = None
    model_calls: dict[str, ModelTrace] = Field(default_factory=dict)
    retrieval_progress: RetrievalCandidatesData | None = None


class WorkbenchQueries:
    def __init__(self, uow: UowFactory, artifacts: ArtifactStore):
        self.uow, self.artifacts = uow, artifacts

    async def sessions(self) -> list[Session]:
        async with self.uow() as tx:
            return await tx.sessions.list()

    async def session(self, session_id: str) -> Session:
        async with self.uow() as tx:
            return await tx.sessions.get(session_id)

    async def events(self, session_id: str, after: int = 0):
        async with self.uow() as tx:
            await tx.sessions.get(session_id)
            return await tx.sessions.events(session_id, after)

    async def messages(self, session_id: str, before: str | None, limit: int):
        async with self.uow() as tx:
            roots = {
                run.id for run in await tx.runs.list(session_id) if not run.parent_id
            }
            return [
                item
                for item in await tx.sessions.messages(
                    session_id, before=before, limit=limit
                )
                if item.run_id in roots
            ]

    async def run_trace(self, run_id: str) -> RunTrace:
        async with self.uow() as tx:
            run = await tx.runs.get(run_id)
            calls, cursor = {}, 0
            while events := await tx.sessions.events(run.session_id, cursor, 1000):
                cursor = events[-1].seq
                for event in events:
                    if event.run_id == run_id:
                        project_model_event(calls, event)
            return RunTrace(
                run=run, models=list(calls.values()), tools=await tx.runs.tools(run_id)
            )

    async def model_view(self, run_id: str, call_id: str, view: str) -> str:
        from paper_search.domain import MissingRecord

        trace = await self.run_trace(run_id)
        call = next((item for item in trace.models if item.id == call_id), None)
        file_id = (
            (call.request_file if view == "request" else call.response_file)
            if call
            else None
        )
        if not file_id:
            raise MissingRecord("model view not found")
        return (await self.artifacts.read(file_id)).decode("utf-8")

    async def snapshot(self, session_id: str) -> SessionSnapshot:
        async with self.uow() as tx:
            session = await tx.sessions.get(session_id)
            runs = await tx.runs.list(session_id)
            roots = {run.id for run in runs if not run.parent_id}
            active = {run.id for run in runs if run.status == RunStatus.RUNNING}
            messages = [
                item
                for item in await tx.sessions.messages(session_id, limit=200)
                if item.run_id in roots
            ]
            docs = [
                item.model_copy(update={"parsed": None})
                for item in await tx.documents.list(session_id)
            ]
            artifacts = await tx.artifacts.list(session_id)
            evidence = await tx.papers.list_evidence(session_id)
            cursor, streams, retrieval = 0, {}, None
            model_calls, retrieval_progress = {}, None
            while events := await tx.sessions.events(session_id, cursor, 1000):
                cursor = events[-1].seq
                for event in events:
                    project_model_event(model_calls, event)
                    if event.type == "retrieval.candidates" and event.run_id in roots:
                        retrieval_progress = event.data
                    if event.type == "retrieval.completed" and event.run_id in roots:
                        retrieval = event.data.result
                        retrieval_progress = None
                    if event.run_id not in roots or event.run_id not in active:
                        continue
                    if event.type == "run.started":
                        streams = {
                            key: value
                            for key, value in streams.items()
                            if value.run_id != event.run_id
                        }
                    elif event.type == "model.started" and event.data.publish:
                        key = event.data.stream_id
                        streams[key] = StreamingMessage(
                            id=key,
                            run_id=event.run_id,
                            created_at=event.data.created_at,
                        )
                    elif (
                        event.type == "message.delta"
                        and event.data.stream_id in streams
                    ):
                        key = event.data.stream_id
                        streams[key] = streams[key].model_copy(
                            update={"content": streams[key].content + event.data.text}
                        )
                    elif event.type == "tool.queued":
                        key = event.data.execution.model_stream_id
                        if key and key in streams:
                            stream = streams[key]
                            streams[key] = stream.model_copy(
                                update={
                                    "tool_calls": tuple(
                                        dict.fromkeys(
                                            (
                                                *stream.tool_calls,
                                                event.data.execution.call,
                                            )
                                        )
                                    )
                                }
                            )
                    elif event.type == "model.finished" and not event.data.completed:
                        streams.pop(event.data.stream_id, None)
                    elif event.type == "message.completed":
                        streams.pop(event.data.message.id, None)
            calls = []
            for run in runs:
                calls.extend(await tx.runs.tools(run.id))
            return SessionSnapshot(
                session=session,
                messages=messages,
                runs=runs,
                documents=docs,
                artifacts=artifacts,
                evidence=evidence,
                event_cursor=cursor,
                tools=calls,
                streams=streams,
                research=await tx.research.get(session_id),
                retrieval=retrieval,
                model_calls=model_calls,
                retrieval_progress=retrieval_progress,
            )

    async def papers(self):
        async with self.uow() as tx:
            return await tx.papers.list()

    async def web_source(self, snapshot_id: str) -> SourceSnapshotView:
        async with self.uow() as tx:
            snapshot = await tx.research.snapshot(snapshot_id)
            artifact = await tx.artifacts.get(snapshot.artifact_id)
        return SourceSnapshotView(
            snapshot=snapshot,
            text=(await self.artifacts.read(artifact.file_id)).decode(),
        )

    async def bibliography(self):
        return bibtex(await self.papers())

    async def reports(self):
        async with self.uow() as tx:
            return [item for item in await tx.artifacts.list() if item.kind == "report"]

    async def document(self, document_id: str):
        async with self.uow() as tx:
            return await tx.documents.get(document_id)

    async def artifact_content(self, artifact_id: str):
        async with self.uow() as tx:
            artifact = await tx.artifacts.get(artifact_id)
        return await self.artifacts.read(artifact.file_id), artifact.mime_type

    async def memories(self, session_id: str | None):
        async with self.uow() as tx:
            return await tx.memories.list(session_id)

    async def memory(self, memory_id: str) -> Memory:
        async with self.uow() as tx:
            return await tx.memories.get(memory_id)

    async def jobs_health(self):
        async with self.uow() as tx:
            jobs = await tx.jobs.list()
        return {
            state: sum(job.status == state for job in jobs)
            for state in ("pending", "running", "failed")
        }
