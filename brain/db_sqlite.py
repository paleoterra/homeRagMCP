"""
SQLite + sqlite-vec database layer.

Drop-in alternative to db.py for single-user / offline-first setups.
The public interface is identical to Database in db.py so the rest of the
codebase requires no changes.

Usage: set DATABASE_URL=sqlite:///path/to/brain.db in the environment.
The path is created automatically if it does not exist.

Limitations vs PostgreSQL:
- hybrid_search uses Python-side RRF (two queries merged) instead of a CTE
- No connection pooling — serialised via a single-threaded executor
- Not suitable for concurrent writes from multiple processes
"""

import asyncio
import concurrent.futures
import json
import sqlite3
import struct
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def _encode(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


class SqliteDatabase:
    def __init__(self, path: str, dimensions: int = 768) -> None:
        self.path = str(Path(path).expanduser().resolve())
        self.dimensions = dimensions
        self._conn: sqlite3.Connection | None = None
        # Single-threaded executor keeps sqlite access on one thread
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._connect_sync)

    def _connect_sync(self) -> None:
        try:
            import sqlite_vec
        except ImportError:
            raise ImportError("sqlite-vec is required: uv add sqlite-vec")

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS documents (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL DEFAULT '',
                content     TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref  TEXT,
                author      TEXT,
                created_at  TEXT,
                indexed_at  TEXT NOT NULL DEFAULT (datetime('now')),
                metadata    TEXT NOT NULL DEFAULT '{{}}'
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id          TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                content     TEXT NOT NULL,
                token_count INTEGER,
                page_number INTEGER,
                indexed_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Vector index
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(
                chunk_id  TEXT PRIMARY KEY,
                embedding float[{self.dimensions}]
            );

            -- Full-text search index
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                content
            );

            CREATE TABLE IF NOT EXISTS tags (
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                tag         TEXT NOT NULL,
                PRIMARY KEY (document_id, tag)
            );

            CREATE INDEX IF NOT EXISTS documents_source_ref_idx  ON documents(source_ref);
            CREATE INDEX IF NOT EXISTS documents_source_type_idx ON documents(source_type);
            CREATE INDEX IF NOT EXISTS documents_indexed_at_idx  ON documents(indexed_at DESC);
            CREATE INDEX IF NOT EXISTS tags_tag_idx              ON tags(tag);
        """)
        self._conn.commit()

    async def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
        self._executor.shutdown(wait=False)

    # -------------------------------------------------------------------------
    # Internal helper
    # -------------------------------------------------------------------------

    def _run(self, fn):
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(self._executor, fn)

    # -------------------------------------------------------------------------
    # Write operations
    # -------------------------------------------------------------------------

    async def add_document(
        self,
        title: str,
        content: str,
        source_type: str,
        source_ref: str | None = None,
        author: str | None = None,
        created_at: Any = None,
        metadata: dict | None = None,
    ) -> str:
        doc_id = str(uuid.uuid4())

        def _fn():
            self._conn.execute(
                """INSERT INTO documents
                       (id, title, content, source_type, source_ref, author, created_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    doc_id, title, content, source_type, source_ref, author,
                    created_at.isoformat() if created_at else None,
                    json.dumps(metadata or {}),
                ),
            )
            self._conn.commit()

        await self._run(_fn)
        return doc_id

    async def add_chunks(self, document_id: str, chunks: list[dict]) -> None:
        def _fn():
            for c in chunks:
                chunk_id = str(uuid.uuid4())
                self._conn.execute(
                    """INSERT INTO chunks (id, document_id, chunk_index, content, token_count, page_number)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (chunk_id, document_id, c["index"], c["content"],
                     c.get("token_count"), c.get("page_number")),
                )
                self._conn.execute(
                    "INSERT INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
                    (chunk_id, _encode(c["embedding"])),
                )
                self._conn.execute(
                    "INSERT INTO chunks_fts (chunk_id, content) VALUES (?, ?)",
                    (chunk_id, c["content"]),
                )
            self._conn.commit()

        await self._run(_fn)

    async def find_by_source_ref(self, source_ref: str) -> str | None:
        def _fn():
            row = self._conn.execute(
                "SELECT id FROM documents WHERE source_ref = ? LIMIT 1", (source_ref,)
            ).fetchone()
            return row["id"] if row else None

        return await self._run(_fn)

    async def add_tags(self, document_id: str, tags: list[str]) -> None:
        if not tags:
            return

        def _fn():
            for tag in tags:
                tag = tag.lower().strip()
                if tag:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO tags (document_id, tag) VALUES (?, ?)",
                        (document_id, tag),
                    )
            self._conn.commit()

        await self._run(_fn)

    async def tag_document(self, document_id: str, tags: list[str]) -> None:
        await self.add_tags(document_id, tags)

    async def delete_document(self, document_id: str) -> bool:
        def _fn():
            # Manually remove from virtual tables (CASCADE doesn't cover them)
            chunk_ids = [
                r[0] for r in self._conn.execute(
                    "SELECT id FROM chunks WHERE document_id = ?", (document_id,)
                ).fetchall()
            ]
            for cid in chunk_ids:
                self._conn.execute("DELETE FROM chunk_vec WHERE chunk_id = ?", (cid,))
                self._conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))

            result = self._conn.execute(
                "DELETE FROM documents WHERE id = ?", (document_id,)
            )
            self._conn.commit()
            return result.rowcount > 0

        return await self._run(_fn)

    # -------------------------------------------------------------------------
    # Read operations
    # -------------------------------------------------------------------------

    async def vector_search(
        self,
        embedding: list[float],
        limit: int = 5,
        source_types: list[str] | None = None,
    ) -> list[dict]:
        vec_bytes = _encode(embedding)

        def _fn():
            if source_types:
                placeholders = ",".join("?" * len(source_types))
                rows = self._conn.execute(
                    f"""
                    SELECT d.id, d.title, d.source_type, d.source_ref,
                           c.content, c.page_number,
                           1 - cv.distance AS score
                    FROM chunk_vec cv
                    JOIN chunks c ON c.id = cv.chunk_id
                    JOIN documents d ON d.id = c.document_id
                    WHERE cv.embedding MATCH ? AND k = ?
                      AND d.source_type IN ({placeholders})
                    ORDER BY cv.distance
                    """,
                    (vec_bytes, limit, *source_types),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT d.id, d.title, d.source_type, d.source_ref,
                           c.content, c.page_number,
                           1 - cv.distance AS score
                    FROM chunk_vec cv
                    JOIN chunks c ON c.id = cv.chunk_id
                    JOIN documents d ON d.id = c.document_id
                    WHERE cv.embedding MATCH ? AND k = ?
                    ORDER BY cv.distance
                    """,
                    (vec_bytes, limit),
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_fn)

    async def hybrid_search(self, query: str, embedding: list[float], limit: int = 5) -> list[dict]:
        """Reciprocal Rank Fusion of vector + FTS5 results, merged in Python."""
        vec_bytes = _encode(embedding)

        def _fn():
            # Vector results
            vec_rows = self._conn.execute(
                """
                SELECT cv.chunk_id, cv.distance
                FROM chunk_vec cv
                WHERE cv.embedding MATCH ? AND k = 20
                ORDER BY cv.distance
                """,
                (vec_bytes,),
            ).fetchall()

            # FTS5 results (BM25 via implicit rank column)
            fts_query = " OR ".join(f'"{w}"' for w in query.split() if w)
            try:
                fts_rows = self._conn.execute(
                    "SELECT chunk_id FROM chunks_fts WHERE content MATCH ? ORDER BY rank LIMIT 20",
                    (fts_query,),
                ).fetchall()
            except sqlite3.OperationalError:
                fts_rows = []

            # RRF scoring
            scores: dict[str, float] = {}
            for rank, r in enumerate(vec_rows, 1):
                cid = r["chunk_id"]
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + rank)
            for rank, r in enumerate(fts_rows, 1):
                cid = r["chunk_id"]
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + rank)

            top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:limit]
            if not top_ids:
                return []

            placeholders = ",".join("?" * len(top_ids))
            rows = self._conn.execute(
                f"""
                SELECT d.id, d.title, d.source_type, d.source_ref,
                       c.content, c.page_number, c.id AS chunk_id
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders})
                """,
                top_ids,
            ).fetchall()

            # Attach scores and re-sort
            by_id = {r["chunk_id"]: dict(r) for r in rows}
            result = []
            for cid in top_ids:
                if cid in by_id:
                    row = by_id[cid]
                    row["score"] = scores[cid]
                    result.append(row)
            return result

        return await self._run(_fn)

    async def list_documents(
        self,
        source_type: str | None = None,
        tag: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        def _fn():
            conditions, params = [], []
            if source_type:
                conditions.append("d.source_type = ?")
                params.append(source_type)
            if tag:
                conditions.append(
                    "EXISTS (SELECT 1 FROM tags t WHERE t.document_id = d.id AND t.tag = ?)"
                )
                params.append(tag.lower().strip())

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = self._conn.execute(
                f"""
                SELECT d.id, d.title, d.source_type, d.source_ref,
                       d.indexed_at, d.created_at,
                       GROUP_CONCAT(t.tag) AS tags_str
                FROM documents d
                LEFT JOIN tags t ON t.document_id = d.id
                {where}
                GROUP BY d.id
                ORDER BY d.indexed_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()

            result = []
            for r in rows:
                d = dict(r)
                d["tags"] = sorted(d.pop("tags_str").split(",")) if d["tags_str"] else []
                d["indexed_at"] = (
                    datetime.fromisoformat(d["indexed_at"]) if d["indexed_at"] else None
                )
                result.append(d)
            return result

        return await self._run(_fn)

    async def get_document(self, document_id: str) -> dict | None:
        def _fn():
            row = self._conn.execute(
                """
                SELECT d.id, d.title, d.content, d.source_type, d.source_ref,
                       d.author, d.created_at, d.indexed_at, d.metadata,
                       GROUP_CONCAT(t.tag) AS tags_str
                FROM documents d
                LEFT JOIN tags t ON t.document_id = d.id
                WHERE d.id = ?
                GROUP BY d.id
                """,
                (document_id,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["tags"] = sorted(d.pop("tags_str").split(",")) if d["tags_str"] else []
            d["metadata"] = json.loads(d["metadata"] or "{}")
            d["indexed_at"] = (
                datetime.fromisoformat(d["indexed_at"]) if d["indexed_at"] else None
            )
            return d

        return await self._run(_fn)

    async def stats(self) -> dict:
        def _fn():
            doc_count = self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunk_count = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            tag_count = self._conn.execute("SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0]
            size_bytes = Path(self.path).stat().st_size
            return {
                "doc_count": doc_count,
                "chunk_count": chunk_count,
                "tag_count": tag_count,
                "db_size": f"{size_bytes / 1024 / 1024:.1f} MB",
            }

        return await self._run(_fn)
