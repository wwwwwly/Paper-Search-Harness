"""SQLAlchemy persistence. Transaction and dialect details stay in this module."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    delete,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from paper_search.domain import (
    Artifact,
    Delivery,
    Document,
    Evidence,
    Job,
    Memory,
    Message,
    MissingRecord,
    Paper,
    ResearchBoard,
    Run,
    Session,
    SourceSnapshot,
    ToolExecution,
    now,
)
from paper_search.events import EVENT_ADAPTER, SessionEvent

metadata = MetaData()


def value_table(name: str, *columns: Column) -> Table:
    return Table(
        name,
        metadata,
        Column("id", String, primary_key=True),
        *columns,
        Column("payload", JSON, nullable=False),
    )


sessions = value_table("sessions")
runs = value_table("runs", Column("session_id", String, index=True))
messages = Table(
    "messages",
    metadata,
    Column("position", Integer, primary_key=True, autoincrement=True),
    Column("id", String, unique=True, nullable=False),
    Column("session_id", String, nullable=False, index=True),
    Column("client_id", String),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("session_id", "client_id"),
)
events = Table(
    "events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String, index=True, nullable=False),
    Column("run_id", String),
    Column("type", String, nullable=False),
    Column("data", JSON, nullable=False),
    Column("created_at", String, nullable=False),
)
tools = value_table("tool_executions", Column("run_id", String, index=True))
jobs = value_table("jobs")
outbox = value_table(
    "outbox", Column("published", Boolean, default=False, nullable=False)
)
papers = value_table("papers")
paper_identifiers = Table(
    "paper_identifiers",
    metadata,
    Column("key", String, primary_key=True),
    Column("paper_id", String, nullable=False, index=True),
)
evidence = value_table("evidence", Column("session_id", String, index=True))
documents = value_table("documents", Column("session_id", String, index=True))
memories = value_table("memories")
artifacts = value_table("artifacts", Column("session_id", String, index=True))
research = value_table("research")
source_snapshots = value_table("source_snapshots")
model_states = Table(
    "model_states",
    metadata,
    Column("message_id", String, primary_key=True),
    Column("file_id", String, nullable=False),
)
deliveries = Table(
    "deliveries",
    metadata,
    Column("id", String, primary_key=True),
    Column("job_id", String, nullable=False),
    Column("kind", String, nullable=False, index=True),
    Column("session_id", String, nullable=False),
    Column("ready_at", String, nullable=False),
)


async def _get(db: AsyncSession, table: Table, key: str, model):
    raw = await db.scalar(select(table.c.payload).where(table.c.id == key))
    if raw is None:
        raise MissingRecord(f"{table.name}: {key}")
    return model.model_validate(raw)


async def _save(db: AsyncSession, table: Table, key: str, value, **columns) -> None:
    values = {"payload": value.model_dump(mode="json"), **columns}
    present = await db.scalar(select(table.c.id).where(table.c.id == key))
    if present is None:
        await db.execute(insert(table).values(id=key, **values))
    else:
        await db.execute(update(table).where(table.c.id == key).values(**values))


async def _list(db: AsyncSession, table: Table, model, *conditions):
    rows = await db.scalars(select(table.c.payload).where(*conditions))
    return [model.model_validate(raw) for raw in rows]


class SqlSessions:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, session_id: str) -> Session:
        return await _get(self.db, sessions, session_id, Session)

    async def save(self, session: Session) -> None:
        await _save(self.db, sessions, session.id, session)

    async def list(self) -> list[Session]:
        return sorted(
            await _list(self.db, sessions, Session),
            key=lambda item: item.updated_at,
            reverse=True,
        )

    async def messages(
        self, session_id: str, *, before: str | None = None, limit: int = 1000
    ) -> list[Message]:
        query = select(messages.c.payload).where(messages.c.session_id == session_id)
        if before:
            position = await self.db.scalar(
                select(messages.c.position).where(
                    messages.c.id == before, messages.c.session_id == session_id
                )
            )
            if position is None:
                raise MissingRecord(f"message: {before}")
            query = query.where(messages.c.position < position)
        rows = await self.db.scalars(
            query.order_by(messages.c.position.desc()).limit(limit)
        )
        return list(reversed([Message.model_validate(raw) for raw in rows]))

    async def append_message(self, message: Message) -> None:
        if await self.db.scalar(
            select(messages.c.id).where(messages.c.id == message.id)
        ):
            return
        await self.db.execute(
            insert(messages).values(
                id=message.id,
                session_id=message.session_id,
                client_id=message.client_id,
                payload=message.model_dump(mode="json"),
            )
        )

    async def find_client_message(
        self, session_id: str, client_id: str
    ) -> Message | None:
        raw = await self.db.scalar(
            select(messages.c.payload).where(
                messages.c.session_id == session_id, messages.c.client_id == client_id
            )
        )
        return Message.model_validate(raw) if raw else None

    async def append_event(
        self, session_id: str, run_id: str | None, event_type: str, data: dict[str, Any]
    ) -> SessionEvent:
        event = EVENT_ADAPTER.validate_python(
            {
                "seq": 0,
                "session_id": session_id,
                "run_id": run_id,
                "type": event_type,
                "data": data,
            }
        )
        result = await self.db.execute(
            insert(events).values(
                session_id=session_id,
                run_id=run_id,
                type=event_type,
                data=event.data.model_dump(mode="json"),
                created_at=event.created_at.isoformat(),
            )
        )
        return event.model_copy(update={"seq": result.inserted_primary_key[0]})

    async def events(
        self, session_id: str, after: int = 0, limit: int = 250
    ) -> list[SessionEvent]:
        rows = await self.db.execute(
            select(events)
            .where(events.c.session_id == session_id, events.c.seq > after)
            .order_by(events.c.seq)
            .limit(limit)
        )
        return [EVENT_ADAPTER.validate_python(dict(row._mapping)) for row in rows]


class SqlRuns:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def model_state(self, message_id: str) -> str | None:
        return await self.db.scalar(
            select(model_states.c.file_id).where(
                model_states.c.message_id == message_id
            )
        )

    async def save_model_state(self, message_id: str, file_id: str) -> None:
        await self.db.execute(
            insert(model_states).values(message_id=message_id, file_id=file_id)
        )

    async def get(self, run_id: str) -> Run:
        return await _get(self.db, runs, run_id, Run)

    async def save(self, run: Run) -> None:
        await _save(self.db, runs, run.id, run, session_id=run.session_id)

    async def list(self, session_id: str | None = None) -> list[Run]:
        return await _list(
            self.db,
            runs,
            Run,
            *([runs.c.session_id == session_id] if session_id else []),
        )

    async def tool(self, run_id: str, call_id: str) -> ToolExecution | None:
        try:
            return await _get(self.db, tools, f"{run_id}:{call_id}", ToolExecution)
        except MissingRecord:
            return None

    async def save_tool(self, execution: ToolExecution) -> None:
        await _save(
            self.db,
            tools,
            f"{execution.run_id}:{execution.call.id}",
            execution,
            run_id=execution.run_id,
        )

    async def tools(self, run_id: str) -> list[ToolExecution]:
        return await _list(self.db, tools, ToolExecution, tools.c.run_id == run_id)


class SqlJobs:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, job_id: str) -> Job:
        return await _get(self.db, jobs, job_id, Job)

    async def enqueue(self, job: Job) -> None:
        await self.save(job)
        await _save(self.db, outbox, job.delivery_id, job, published=False)

    async def save(self, job: Job) -> None:
        await _save(self.db, jobs, job.id, job)

    async def claim(self, job_id: str, owner: str, lease_seconds: int) -> Job | None:
        job = await self.get(job_id)
        if job.status in {"completed", "failed", "cancelled"}:
            return None
        if job.status == "running" and job.lease_until and job.lease_until > now():
            return None
        job = job.model_copy(
            update={
                "status": "running",
                "owner": owner,
                "lease_until": now() + timedelta(seconds=lease_seconds),
                "attempts": job.attempts + 1,
            }
        )
        await self.save(job)
        return job

    async def pending_outbox(self) -> list[Job]:
        rows = await self.db.scalars(
            select(outbox.c.payload).where(outbox.c.published.is_(False))
        )
        return [Job.model_validate(row) for row in rows]

    async def mark_published(self, delivery_id: str) -> None:
        await self.db.execute(
            update(outbox).where(outbox.c.id == delivery_id).values(published=True)
        )

    async def list(self) -> list[Job]:
        return await _list(self.db, jobs, Job)


class SqlPapers:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, paper_id: str) -> Paper:
        key = await self.db.scalar(
            select(paper_identifiers.c.paper_id).where(
                paper_identifiers.c.key == paper_id
            )
        )
        return await _get(self.db, papers, key or paper_id, Paper)

    async def get_many(self, paper_ids: set[str]) -> dict[str, Paper]:
        aliases = dict(
            (
                await self.db.execute(
                    select(paper_identifiers.c.key, paper_identifiers.c.paper_id).where(
                        paper_identifiers.c.key.in_(paper_ids)
                    )
                )
            ).all()
        )
        ids = {aliases.get(key, key) for key in paper_ids}
        records = {
            item.canonical_id: item
            for row in await self.db.scalars(
                select(papers.c.payload).where(papers.c.id.in_(ids))
            )
            for item in (Paper.model_validate(row),)
        }
        return {
            key: records[aliases.get(key, key)]
            for key in paper_ids
            if aliases.get(key, key) in records
        }

    async def find_by_identifiers(self, keys) -> list[Paper]:
        ids = set(
            await self.db.scalars(
                select(paper_identifiers.c.paper_id).where(
                    paper_identifiers.c.key.in_(keys)
                )
            )
        )
        rows = await self.db.scalars(
            select(papers.c.payload).where(papers.c.id.in_(ids))
        )
        return [Paper.model_validate(row) for row in rows]

    async def save(self, paper: Paper) -> None:
        from paper_search.retrieval.identity import identity_keys

        await _save(self.db, papers, paper.canonical_id, paper)
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        for key in {paper.canonical_id, *paper.aliases, *identity_keys(paper)}:
            await self.db.execute(
                sqlite_insert(paper_identifiers)
                .values(key=key, paper_id=paper.canonical_id)
                .on_conflict_do_update(
                    index_elements=["key"], set_={"paper_id": paper.canonical_id}
                )
            )

    async def remap(self, old: str, new: str):
        # Only typed paper-reference fields change; original URLs and historical events stay intact.
        def refs(value, key=""):
            if isinstance(value, dict):
                return {name: refs(item, name) for name, item in value.items()}
            if isinstance(value, list):
                return [refs(item, key) for item in value]
            return (
                new
                if value == old
                and key
                in {
                    "paper_id",
                    "seed_paper_id",
                    "paper_ids",
                    "excluded_ids",
                    "selected_paper_ids",
                    "pending_paper_ids",
                }
                else value
            )

        for table in (research, evidence, documents, memories, sessions, runs):
            rows = (await self.db.execute(select(table.c.id, table.c.payload))).all()
            for key, raw in rows:
                changed = refs(raw)
                if changed != raw:
                    await self.db.execute(
                        update(table).where(table.c.id == key).values(payload=changed)
                    )
        await self.db.execute(
            update(paper_identifiers)
            .where(paper_identifiers.c.paper_id == old)
            .values(paper_id=new)
        )

    async def remove(self, paper_id: str) -> None:
        await self.db.execute(delete(papers).where(papers.c.id == paper_id))

    async def list(self) -> list[Paper]:
        return await _list(self.db, papers, Paper)

    async def save_evidence(self, item: Evidence) -> None:
        await _save(self.db, evidence, item.id, item, session_id=item.session_id)

    async def evidence(self, evidence_id: str) -> Evidence:
        return await _get(self.db, evidence, evidence_id, Evidence)

    async def list_evidence(self, session_id: str) -> list[Evidence]:
        return await _list(
            self.db, evidence, Evidence, evidence.c.session_id == session_id
        )


class SqlDocuments:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, document_id: str) -> Document:
        return await _get(self.db, documents, document_id, Document)

    async def save(self, document: Document) -> None:
        await _save(
            self.db, documents, document.id, document, session_id=document.session_id
        )

    async def list(self, session_id: str | None = None) -> list[Document]:
        return await _list(
            self.db,
            documents,
            Document,
            *([documents.c.session_id == session_id] if session_id else []),
        )


class SqlMemories:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, memory_id: str) -> Memory:
        return await _get(self.db, memories, memory_id, Memory)

    async def save(self, memory: Memory) -> None:
        await _save(self.db, memories, memory.id, memory)

    async def list(
        self, session_id: str | None = None, *, include_deleted: bool = False
    ) -> list[Memory]:
        return [
            item
            for item in await _list(self.db, memories, Memory)
            if (include_deleted or not item.deleted)
            and (
                session_id is None
                or item.scope == "global"
                or item.session_id == session_id
            )
        ]


class SqlArtifacts:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def save(self, artifact: Artifact) -> None:
        await _save(
            self.db, artifacts, artifact.id, artifact, session_id=artifact.session_id
        )

    async def get(self, artifact_id: str) -> Artifact:
        return await _get(self.db, artifacts, artifact_id, Artifact)

    async def list(self, session_id: str | None = None) -> list[Artifact]:
        return await _list(
            self.db,
            artifacts,
            Artifact,
            *([artifacts.c.session_id == session_id] if session_id else []),
        )


class SqlResearch:
    def __init__(self, db):
        self.db = db

    async def get(self, session_id):
        try:
            return await _get(self.db, research, session_id, ResearchBoard)
        except MissingRecord:
            return ResearchBoard(session_id=session_id)

    async def save(self, record):
        await _save(self.db, research, record.session_id, record)

    async def snapshot(self, snapshot_id):
        return await _get(self.db, source_snapshots, snapshot_id, SourceSnapshot)

    async def save_snapshot(self, snapshot):
        await _save(self.db, source_snapshots, snapshot.id, snapshot)


class SqlUnitOfWork:
    def __init__(self, store: SqlStore):
        self.store = store

    async def __aenter__(self):
        await self.store.lock.acquire()
        try:
            self.db = self.store.session_factory()
            await self.db.begin()
        except BaseException:
            self.store.lock.release()
            raise
        self.sessions, self.runs, self.jobs = (
            SqlSessions(self.db),
            SqlRuns(self.db),
            SqlJobs(self.db),
        )
        self.papers, self.documents = SqlPapers(self.db), SqlDocuments(self.db)
        self.memories, self.artifacts = SqlMemories(self.db), SqlArtifacts(self.db)
        self.research = SqlResearch(self.db)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                await self.db.commit()
            else:
                await self.db.rollback()
        finally:
            await self.db.close()
            self.store.lock.release()


class SqlStore:
    def __init__(self, url: str):
        parsed = make_url(url)
        if (
            parsed.get_backend_name() == "sqlite"
            and parsed.database
            and parsed.database != ":memory:"
        ):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(
            url, json_serializer=lambda value: json.dumps(value, ensure_ascii=False)
        )
        if parsed.get_backend_name() == "sqlite":

            @event.listens_for(self.engine.sync_engine, "connect")
            def configure(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.lock = asyncio.Lock()

    def uow(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self)

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()


class LocalDeliveryQueue:
    """Durable delivery only. Job state and session events live elsewhere."""

    def __init__(self, store: SqlStore):
        self.store = store

    async def publish(self, job: Job) -> None:
        async with self.store.uow() as tx:
            if (
                await tx.db.scalar(
                    select(deliveries.c.id).where(deliveries.c.id == job.delivery_id)
                )
                is None
            ):
                await tx.db.execute(
                    insert(deliveries).values(
                        id=job.delivery_id,
                        job_id=job.id,
                        kind=job.kind,
                        session_id=job.session_id,
                        ready_at=now().isoformat(),
                    )
                )

    async def receive(self, kind: str) -> Delivery | None:
        async with self.store.uow() as tx:
            row = (
                await tx.db.execute(
                    select(deliveries)
                    .where(
                        deliveries.c.kind == kind,
                        deliveries.c.ready_at <= now().isoformat(),
                    )
                    .order_by(deliveries.c.ready_at)
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            await tx.db.execute(
                update(deliveries)
                .where(deliveries.c.id == row.id)
                .values(ready_at=(now() + timedelta(seconds=30)).isoformat())
            )
            return Delivery(id=row.id, job_id=row.job_id, session_id=row.session_id)

    async def ack(self, delivery: Delivery) -> None:
        async with self.store.uow() as tx:
            await tx.db.execute(
                delete(deliveries).where(deliveries.c.id == delivery.id)
            )

    async def retry(self, delivery: Delivery, delay_seconds: float) -> None:
        async with self.store.uow() as tx:
            await tx.db.execute(
                update(deliveries)
                .where(deliveries.c.id == delivery.id)
                .values(ready_at=(now() + timedelta(seconds=delay_seconds)).isoformat())
            )
