CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- One record per ingested item
CREATE TABLE IF NOT EXISTS documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    source_type TEXT NOT NULL,    -- 'note' | 'pdf' | 'email' | 'url' | 'doc' | 'txt'
    source_ref  TEXT,             -- file path, URL, email Message-ID, etc.
    author      TEXT,
    created_at  TIMESTAMPTZ,      -- original creation date if known
    indexed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata    JSONB NOT NULL DEFAULT '{}'
);

-- Documents split into overlapping chunks for search
CREATE TABLE IF NOT EXISTS chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(768),      -- nomic-embed-text output dimension
    token_count INTEGER,
    indexed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tags for manual and auto-generated organization
CREATE TABLE IF NOT EXISTS tags (
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (document_id, tag)
);

-- HNSW index for fast approximate nearest-neighbor search
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Full-text search for hybrid search
CREATE INDEX IF NOT EXISTS chunks_fts_idx
    ON chunks USING gin(to_tsvector('english', content));

-- Lookup indexes
CREATE INDEX IF NOT EXISTS documents_source_type_idx ON documents(source_type);
CREATE INDEX IF NOT EXISTS documents_indexed_at_idx ON documents(indexed_at DESC);
CREATE INDEX IF NOT EXISTS tags_tag_idx ON tags(tag);
