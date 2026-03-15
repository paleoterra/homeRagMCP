# Brain Project

## Overview

Brain is a local semantic memory system that creates a RAG (Retrieval-Augmented Generation) engine accessible to Claude Desktop via MCP (Model Context Protocol). It supports multiple storage backends (PostgreSQL + pgvector or SQLite + sqlite-vec) and multiple embedding backends (Ollama, MLX, Apple NLEmbedding, OpenAI).

The system is split into two main entry points:
1. **CLI** (`brain/cli.py`) — manages multiple named brain databases locally
2. **MCP Server** (`brain/server.py`) — exposes search/ingest/add-note tools to Claude Desktop

## Project Structure

```
brain/
├── brain/
│   ├── cli.py                    # CLI: create/list/remove/migrate/ingest commands
│   ├── server.py                 # MCP server + FastMCP tools for Claude Desktop
│   ├── embedder.py               # Multi-backend embedder abstraction (Ollama, MLX, NLEmbedding, OpenAI)
│   ├── db.py                     # PostgreSQL + asyncpg + pgvector backend
│   ├── db_sqlite.py              # SQLite + sqlite-vec alternative backend
│   ├── ingestion/
│   │   ├── pipeline.py           # Unified ingest_* functions (file, text, url, email, mbox, chat)
│   │   ├── chunker.py            # Text chunking logic
│   │   └── parsers/
│   │       ├── pdf.py            # PDF parsing with page numbers
│   │       ├── document.py       # DOCX, PPTX, XLSX parsing
│   │       ├── markdown.py       # Markdown + text parsing
│   │       ├── url.py            # Web URL fetching + parsing
│   │       ├── email_parser.py   # Email message parsing
│   │       └── chat.py           # Chat export parsing
│   └── __init__.py
├── migrations/
│   ├── 001_initial.sql           # Postgres schema for 768-dim vectors (Ollama, MLX, OpenAI)
│   ├── 001_initial_512.sql       # Postgres schema for 512-dim vectors (NLEmbedding only)
│   └── 002_page_numbers.sql      # Adds page_number column to chunks
├── pyproject.toml                # uv project config with optional macOS backends
└── .env                          # Local config (not secret, but gitignored)
```

## Key Architecture Decisions

### Dual Backend Support

**Storage backends:**
- **PostgreSQL** (default for production/shared) — asyncpg pool, pgvector extension
- **SQLite** (default for local/offline) — single-threaded executor, sqlite-vec extension, auto-create schema

Database URL format determines backend:
- `postgresql://user:pass@host:5432/dbname` → PostgreSQL
- `sqlite:///path/to/brain.db` → SQLite (path auto-created)

**Embedding backends:**
- **Ollama** (default) — local server at `http://localhost:11434`, requires Ollama running
- **MLX** — Apple Silicon in-process, no server, requires `uv add mlx-embeddings`
- **NLEmbedding** — Apple built-in API, zero download, **512-dim**
- **OpenAI** — remote API, requires `OPENAI_API_KEY`, 768-dim

**CRITICAL:** Vector dimensions must match database schema. Ollama/MLX/OpenAI use 768-dim. NLEmbedding uses 512-dim and requires `migrations/001_initial_512.sql`. Never mix backends in the same database.

### Ingestion Pipeline

`pipeline.py` provides six top-level ingest functions:
- `ingest_file(path, ...)` — auto-detects format (PDF, DOCX, PPTX, XLSX, Markdown, text, email, mbox)
- `ingest_text(content, ...)` — raw text
- `ingest_url(url, ...)` — fetch and parse web pages
- `ingest_email(email_str, ...)` — raw email message
- `ingest_mbox(path, ...)` — mailbox file
- `ingest_chat(messages, ...)` — chat export (LLM conversation format)

All return document UUID. All support `force=True` to re-index existing documents (matched by `source_ref`).

### MCP Server Tools

`server.py` exposes via FastMCP:
- `add_note`, `ingest_file`, `ingest_text`, `ingest_url` — write tools
- `search`, `list_documents`, `get_document`, `get_chunks` — read tools

Tool names appear to Claude as `brain-<name>/search`, `brain-<name>/add_note`, etc.

## Common Commands

```bash
# Package management
uv sync                                       # install all dependencies

# Brain management
uv run brain create <name> --embedder ollama
uv run brain list
uv run brain remove <name> [--delete-data]
uv run brain migrate <name>                   # apply migrations to existing DB

# Ingest
uv run brain ingest /path/to/dir --name <name> [--recursive] [--tags tag1,tag2]

# Test MCP server directly
uv run python -m brain.server
```

## Important Gotchas

### Vector Dimensions
NLEmbedding produces 512-dim vectors — incompatible with default 768-dim schema. Use `001_initial_512.sql` for NLEmbedding-only databases. Do not mix embedder backends in the same database.

### Ollama Dependency
Ollama must be running locally for both ingest and query. Start with `ollama serve`. Model: `nomic-embed-text`.

### MLX Backend
Requires Apple Silicon. Model `mlx-community/nomic-embed-text-v1.5` requires HuggingFace auth:
```bash
uv run python -c "from huggingface_hub import login; login()"
```

### Postgres on Synology NAS
- Use port **5433** (Synology runs internal Postgres on 5432)
- Use IP directly (`192.168.1.127`) — asyncpg does not resolve `.local` mDNS

### Ingest Idempotency
- `force=False` (default) → raises ValueError if `source_ref` already exists
- `force=True` → deletes and re-indexes

### Null Bytes
Strip `\x00` from text before storing in Postgres — UTF-8 text columns reject null bytes.

## Environment Variables

```bash
DATABASE_URL=postgresql://user:pass@host:5432/brain    # or sqlite:///path/to/brain.db
EMBEDDER_BACKEND=ollama|mlx|nlembedding|openai
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=nomic-embed-text
OPENAI_API_KEY=sk-...
VECTOR_DIMENSIONS=768                                  # set to 512 for NLEmbedding
```

## Extending the Project

### New File Format Parser
1. Create `brain/ingestion/parsers/<format>.py`
2. Return `(title: str, content: str, metadata: dict)` — optionally include `pre_chunks` list for PDFs/page-aware formats
3. Register by file extension in `pipeline.py`

### New Embedder Backend
1. Subclass `_Backend` in `embedder.py`, implement `async def embed(text: str) -> list[float]`
2. Optionally override `async def embed_batch(texts)` for efficiency
3. Add to `EMBEDDER_BACKEND` env var docs
