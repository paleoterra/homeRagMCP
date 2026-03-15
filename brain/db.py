"""
PostgreSQL + pgvector database layer.

All queries go through this module. The pool is initialized once at startup
via connect() and closed at shutdown via disconnect().
"""

import json
from typing import Any

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            self.url,
            init=self._init_conn,
            min_size=2,
            max_size=10,
        )

    async def disconnect(self) -> None:
        if self.pool:
            await self.pool.close()

    async def _init_conn(self, conn: asyncpg.Connection) -> None:
        await register_vector(conn)
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
            format="text",
        )

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
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO documents
                    (title, content, source_type, source_ref, author, created_at, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id::text
                """,
                title,
                content,
                source_type,
                source_ref,
                author,
                created_at,
                metadata or {},
            )
            return row["id"]

    async def add_chunks(self, document_id: str, chunks: list[dict]) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO chunks (document_id, chunk_index, content, embedding, token_count, page_number)
                VALUES ($1::uuid, $2, $3, $4, $5, $6)
                """,
                [
                    (
                        document_id,
                        c["index"],
                        c["content"],
                        np.array(c["embedding"], dtype=np.float32),
                        c["token_count"],
                        c.get("page_number"),
                    )
                    for c in chunks
                ],
            )

    async def find_by_source_ref(self, source_ref: str) -> str | None:
        """Return the document ID if a document with this source_ref already exists."""
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id::text FROM documents WHERE source_ref = $1 LIMIT 1",
                source_ref,
            )
            return row["id"] if row else None

    async def add_tags(self, document_id: str, tags: list[str]) -> None:
        if not tags:
            return
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO tags (document_id, tag) VALUES ($1::uuid, $2) ON CONFLICT DO NOTHING",
                [(document_id, tag.lower().strip()) for tag in tags if tag.strip()],
            )

    async def tag_document(self, document_id: str, tags: list[str]) -> None:
        await self.add_tags(document_id, tags)

    async def delete_document(self, document_id: str) -> bool:
        assert self.pool
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM documents WHERE id = $1::uuid",
                document_id,
            )
            return result != "DELETE 0"

    # -------------------------------------------------------------------------
    # Read operations
    # -------------------------------------------------------------------------

    async def vector_search(
        self,
        embedding: list[float],
        limit: int = 5,
        source_types: list[str] | None = None,
    ) -> list[dict]:
        assert self.pool
        vec = np.array(embedding, dtype=np.float32)
        async with self.pool.acquire() as conn:
            if source_types:
                rows = await conn.fetch(
                    """
                    SELECT d.id::text, d.title, d.source_type, d.source_ref,
                           c.content, c.page_number, 1 - (c.embedding <=> $1) AS score
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE d.source_type = ANY($3::text[])
                    ORDER BY c.embedding <=> $1
                    LIMIT $2
                    """,
                    vec,
                    limit,
                    source_types,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT d.id::text, d.title, d.source_type, d.source_ref,
                           c.content, c.page_number, 1 - (c.embedding <=> $1) AS score
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    ORDER BY c.embedding <=> $1
                    LIMIT $2
                    """,
                    vec,
                    limit,
                )
            return [dict(r) for r in rows]

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        limit: int = 5,
    ) -> list[dict]:
        """Reciprocal Rank Fusion of vector + full-text search."""
        assert self.pool
        vec = np.array(embedding, dtype=np.float32)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH vector_ranked AS (
                    SELECT c.id, c.document_id, c.content, c.page_number,
                           ROW_NUMBER() OVER (ORDER BY c.embedding <=> $1) AS rank
                    FROM chunks c
                    LIMIT 20
                ),
                fts_ranked AS (
                    SELECT c.id, c.document_id, c.content, c.page_number,
                           ROW_NUMBER() OVER (
                               ORDER BY ts_rank(
                                   to_tsvector('english', c.content),
                                   plainto_tsquery('english', $2)
                               ) DESC
                           ) AS rank
                    FROM chunks c
                    WHERE to_tsvector('english', c.content) @@ plainto_tsquery('english', $2)
                    LIMIT 20
                )
                SELECT
                    COALESCE(v.document_id, f.document_id)::text AS id,
                    COALESCE(v.content, f.content) AS content,
                    (COALESCE(1.0 / (60 + v.rank), 0) + COALESCE(1.0 / (60 + f.rank), 0)) AS score,
                    d.title, d.source_type, d.source_ref,
                    COALESCE(v.page_number, f.page_number) AS page_number
                FROM vector_ranked v
                FULL OUTER JOIN fts_ranked f ON v.id = f.id
                JOIN documents d ON d.id = COALESCE(v.document_id, f.document_id)
                ORDER BY score DESC
                LIMIT $3
                """,
                vec,
                query,
                limit,
            )
            return [dict(r) for r in rows]

    async def list_documents(
        self,
        source_type: str | None = None,
        tag: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        assert self.pool
        conditions = []
        params: list[Any] = [limit, offset]

        if source_type:
            params.append(source_type)
            conditions.append(f"d.source_type = ${len(params)}")
        if tag:
            params.append(tag.lower().strip())
            conditions.append(
                f"EXISTS (SELECT 1 FROM tags t WHERE t.document_id = d.id AND t.tag = ${len(params)})"
            )

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT d.id::text, d.title, d.source_type, d.source_ref,
                       d.indexed_at, d.created_at,
                       array_agg(t.tag ORDER BY t.tag) FILTER (WHERE t.tag IS NOT NULL) AS tags
                FROM documents d
                LEFT JOIN tags t ON t.document_id = d.id
                {where}
                GROUP BY d.id
                ORDER BY d.indexed_at DESC
                LIMIT $1 OFFSET $2
                """,
                *params,
            )
            return [dict(r) for r in rows]

    async def get_document(self, document_id: str) -> dict | None:
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT d.id::text, d.title, d.content, d.source_type, d.source_ref,
                       d.author, d.created_at, d.indexed_at, d.metadata,
                       array_agg(t.tag ORDER BY t.tag) FILTER (WHERE t.tag IS NOT NULL) AS tags
                FROM documents d
                LEFT JOIN tags t ON t.document_id = d.id
                WHERE d.id = $1::uuid
                GROUP BY d.id
                """,
                document_id,
            )
            return dict(row) if row else None

    async def stats(self) -> dict:
        assert self.pool
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM documents)::int AS doc_count,
                    (SELECT COUNT(*) FROM chunks)::int AS chunk_count,
                    (SELECT COUNT(DISTINCT tag) FROM tags)::int AS tag_count,
                    pg_size_pretty(pg_database_size(current_database())) AS db_size
                """
            )
            return dict(row)
