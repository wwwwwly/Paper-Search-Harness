"""SQLite FTS5/vec implementation of the replaceable SearchIndex port."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import struct
from contextlib import closing
from pathlib import Path

import sqlite_vec

from paper_search.domain import IndexHit, IndexRecord


def _terms(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+", text.casefold())
    return list(
        dict.fromkeys(
            part
            for token in tokens
            for part in (
                [token[i : i + 2] for i in range(len(token) - 1)]
                if re.fullmatch(r"[\u3400-\u9fff]{2,}", token)
                else [token]
            )
        )
    )


def _vector(values: list[float]) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


class SqliteSearchIndex:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        return connection

    async def initialize(self):
        def initialize():
            with closing(self._connect()) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, payload TEXT NOT NULL, vector BLOB, dimensions INTEGER)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS record_scope ON records(kind, scope)"
                )
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS search_text USING fts5(id UNINDEXED, terms)"
                )

        await asyncio.to_thread(initialize)

    async def upsert(
        self, record: IndexRecord, vector: list[float] | None = None
    ) -> None:
        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.kind,
                        record.scope,
                        record.model_dump_json(),
                        _vector(vector) if vector else None,
                        len(vector) if vector else 0,
                    ),
                )
                connection.execute("DELETE FROM search_text WHERE id = ?", (record.id,))
                connection.execute(
                    "INSERT INTO search_text (id, terms) VALUES (?, ?)",
                    (record.id, " ".join(_terms(record.text))),
                )

        async with self.lock:
            await asyncio.to_thread(write)

    async def delete(self, record_id: str) -> None:
        def remove():
            with closing(self._connect()) as connection, connection:
                connection.execute("DELETE FROM records WHERE id = ?", (record_id,))
                connection.execute("DELETE FROM search_text WHERE id = ?", (record_id,))

        async with self.lock:
            await asyncio.to_thread(remove)

    async def search(
        self,
        query: str,
        *,
        kind: str,
        scopes: tuple[str, ...],
        vector: list[float] | None = None,
        limit: int = 10,
        document_id: str | None = None,
    ) -> list[IndexHit]:
        if not scopes:
            return []

        def search():
            marks = ",".join("?" for _ in scopes)
            scope_sql = f"r.kind = ? AND r.scope IN ({marks})"
            args = (kind, *scopes)
            if document_id is not None:
                scope_sql += (
                    " AND json_extract(r.payload, '$.metadata.document_id') = ?"
                )
                args = (*args, document_id)
            scores: dict[str, float] = {}
            payloads: dict[str, str] = {}
            with closing(self._connect()) as connection, connection:
                terms = _terms(query)
                if terms:
                    match = " OR ".join(f'"{term}"' for term in terms[:50])
                    rows = connection.execute(
                        f"SELECT r.id, r.payload FROM search_text JOIN records r ON search_text.id = r.id WHERE search_text MATCH ? AND {scope_sql} ORDER BY bm25(search_text) LIMIT ?",
                        (match, *args, limit * 4),
                    ).fetchall()
                    for rank, (key, payload) in enumerate(rows, 1):
                        scores[key] = 1 / (60 + rank)
                        payloads[key] = payload
                if vector:
                    rows = connection.execute(
                        f"SELECT r.id, r.payload FROM records r WHERE {scope_sql} AND r.dimensions = ? AND r.vector IS NOT NULL ORDER BY vec_distance_cosine(r.vector, ?) LIMIT ?",
                        (*args, len(vector), _vector(vector), limit * 4),
                    ).fetchall()
                    for rank, (key, payload) in enumerate(rows, 1):
                        scores[key] = scores.get(key, 0) + 1 / (60 + rank)
                        payloads[key] = payload
            return [
                IndexHit(
                    record=IndexRecord.model_validate(json.loads(payloads[key])),
                    score=score,
                )
                for key, score in sorted(
                    scores.items(), key=lambda item: (-item[1], item[0])
                )[:limit]
            ]

        async with self.lock:
            return await asyncio.to_thread(search)
