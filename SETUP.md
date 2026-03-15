# Brain — AI Vector Memory via MCP

Local semantic memory backed by PostgreSQL + pgvector, embedded locally or via API,
and exposed to Claude via the Model Context Protocol (MCP).

---

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — `brew install uv`
- [Claude Desktop](https://claude.ai/download)
- **Embedding backend** — choose one (see [Configure Claude Desktop](#4-configure-claude-desktop)):
  - [Ollama](https://ollama.com) — `brew install ollama` — local, cross-platform default
  - MLX — Apple Silicon only, no install needed beyond `uv add mlx-embeddings`
  - Apple NLEmbedding — built into macOS, zero download
  - OpenAI API — requires `OPENAI_API_KEY`, not local
- **Container runtime** — choose one:
  - [Docker Desktop](https://www.docker.com/products/docker-desktop/) — macOS 13+
  - [Apple container CLI](https://github.com/apple/container/releases) — macOS 26+, Apple Silicon optimised

---

## Setup

### 1. Start the database

Two container runtimes are supported. Pick one — the image, schema, and data are identical.

#### Option A — Docker (macOS 13+)

```bash
cd ~/workspace/brain
make up
```

#### Option B — Apple container CLI (macOS 26+)

```bash
# One-time install: download from https://github.com/apple/container/releases
# or: brew install --cask container

cd ~/workspace/brain
make apple-up
```

On first start, the postgres image initialises the data directory and automatically runs
every SQL file in `migrations/` in filename order.

- **Docker** stores data in a named Docker volume (`pgdata`)
- **Apple container** stores data in `~/workspace/brain/data/postgres/` (host directory)

Both survive container restarts and deletion.

### 2. Pull the embedding model (Ollama backend only)

Skip this step if using MLX, NLEmbedding, or OpenAI backends.

Ollama must be running before you can pull a model. Start it first:

```bash
ollama serve   # or open Ollama.app if you installed it via the GUI
```

Then in a new terminal:

```bash
ollama pull nomic-embed-text
```

768-dimension model, runs fully locally. One-time download (~274 MB).

### 3. Install Python dependencies

```bash
make install
```

### 4. Configure Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json` and merge in
the `mcpServers` entry from `claude_desktop_config.json`:

**Ollama backend (default):**

```json
{
  "mcpServers": {
    "brain": {
      "command": "uv",
      "args": [
        "--directory", "/Users/tmoore/workspace/brain",
        "run", "python", "-m", "brain.server"
      ],
      "env": {
        "DATABASE_URL": "postgresql://brain:brain@localhost:5432/brain",
        "OLLAMA_URL": "http://localhost:11434",
        "OLLAMA_MODEL": "nomic-embed-text"
      }
    }
  }
}
```

**MLX backend (Apple Silicon, no Ollama required):**

```json
{
  "mcpServers": {
    "brain": {
      "command": "uv",
      "args": [
        "--directory", "/Users/tmoore/workspace/brain",
        "run", "python", "-m", "brain.server"
      ],
      "env": {
        "DATABASE_URL": "postgresql://brain:brain@localhost:5432/brain",
        "EMBEDDER_BACKEND": "mlx",
        "MLX_MODEL": "mlx-community/nomic-embed-text-v1.5"
      }
    }
  }
}
```

The MLX backend downloads the model from Hugging Face on first run (~270 MB, cached locally).

**Apple NLEmbedding backend (built-in, zero download):**

```json
{
  "mcpServers": {
    "brain": {
      "command": "uv",
      "args": [
        "--directory", "/Users/tmoore/workspace/brain",
        "run", "python", "-m", "brain.server"
      ],
      "env": {
        "DATABASE_URL": "postgresql://brain:brain@localhost:5432/brain_nl",
        "EMBEDDER_BACKEND": "nlembedding"
      }
    }
  }
}
```

NLEmbedding is part of macOS — no model download, no Ollama, runs on Neural Engine.
However it produces **512-dimensional** vectors, which requires a separate database
initialised with `migrations/001_initial_512.sql` instead of `001_initial.sql`:

```bash
# Create a fresh 512-dim database
make psql
CREATE DATABASE brain_nl;
\c brain_nl
\i /docker-entrypoint-initdb.d/001_initial_512.sql
\i /docker-entrypoint-initdb.d/002_page_numbers.sql
\q
```

**OpenAI backend:**

```json
{
  "mcpServers": {
    "brain": {
      "command": "uv",
      "args": [
        "--directory", "/Users/tmoore/workspace/brain",
        "run", "python", "-m", "brain.server"
      ],
      "env": {
        "DATABASE_URL": "postgresql://brain:brain@localhost:5432/brain",
        "EMBEDDER_BACKEND": "openai",
        "OPENAI_API_KEY": "sk-...",
        "OPENAI_EMBED_MODEL": "text-embedding-3-small",
        "OPENAI_EMBED_DIMENSIONS": "768"
      }
    }
  }
}
```

`text-embedding-3-small` supports Matryoshka representations — setting `OPENAI_EMBED_DIMENSIONS=768`
requests 768-dim output, keeping vectors compatible with the default schema. `text-embedding-3-large`
also works (set `OPENAI_EMBED_DIMENSIONS=768` the same way). Note: every embedding call hits the
OpenAI API — not local, not private.

| Backend | Dimensions | Requires | Local? |
|---|---|---|---|
| `ollama` | 768 | Ollama running | Yes |
| `mlx` | 768 | Apple Silicon | Yes |
| `openai` | 768 (configurable) | `OPENAI_API_KEY` | No — API call |
| `nlembedding` | 512 | macOS 12+ | Yes |

**Never mix backends in the same database** — switching requires re-indexing everything.

**Restart Claude Desktop** after saving. The container must be running before Claude
launches the MCP server subprocess.

---

## Daily Use

#### Docker

```bash
make up      # start
make down    # stop (data preserved)
make reset   # wipe all data and recreate (destructive)
make status  # container health
make psql    # interactive psql shell
make logs    # follow postgres logs
```

#### Apple container CLI

```bash
make apple-up      # start
make apple-down    # stop (data preserved)
make apple-reset   # wipe all data and recreate (destructive)
make apple-status  # container status
make apple-psql    # interactive psql shell
make apple-logs    # follow postgres logs
```

Or call the script directly: `./scripts/apple-container.sh <command>`

---

## Applying Migrations to an Existing Database

The `docker-entrypoint-initdb.d` mechanism only runs on a **fresh** (empty) data
directory. If you already have data and need to apply a new migration, run it manually:

```bash
# Docker
make psql
\i /docker-entrypoint-initdb.d/002_page_numbers.sql

# Apple container
make apple-psql
\i /docker-entrypoint-initdb.d/002_page_numbers.sql

# Or from the host with psql installed
psql postgresql://brain:brain@localhost:5432/brain -f migrations/002_page_numbers.sql
```

---

## MCP Tools Reference

### Adding content

| Tool | Description | Key parameters |
|---|---|---|
| `add_note` | Save a text note or snippet | `content`, `title?`, `tags?`, `source_ref?`, `force?` |
| `add_document` | Ingest a local file (PDF, DOCX, MD, TXT) | `path`, `tags?`, `force?` |
| `add_url` | Fetch and ingest a web page | `url`, `tags?`, `force?` |
| `add_email` | Ingest a raw MIME email string | `raw_mime`, `tags?`, `force?` |
| `add_mbox` | Bulk-ingest an mbox archive file | `path`, `tags?`, `force?`, `skip_senders?`, `min_body_length?` |
| `add_chat` | Ingest a JSON chat export file | `path`, `tags?`, `force?` |

**`force=True`** — replaces an already-indexed document that shares the same `source_ref`
(file path, URL, Message-ID, or conversation UUID). Without it, re-ingesting the same
source raises a duplicate error.

**`source_ref` on notes** — supply any stable identifier (ticket ID, URL, filename) to
enable future updates via `force=True` or `update_document`.

### Searching

| Tool | Description | Key parameters |
|---|---|---|
| `search` | Semantic vector search | `query`, `limit?` (default 5), `source_types?` |
| `hybrid_search` | Vector + full-text (Reciprocal Rank Fusion) | `query`, `limit?` |

Use `hybrid_search` when `search` misses specific names, codes, or technical terms.
Results include source location and, for PDFs, the page number.

### Managing

| Tool | Description | Key parameters |
|---|---|---|
| `list_documents` | Browse indexed documents | `source_type?`, `tag?`, `limit?`, `offset?` |
| `get_document` | Retrieve full content by ID | `document_id` |
| `tag_document` | Add tags to an existing document | `document_id`, `tags` |
| `delete_document` | Permanently remove a document | `document_id` |
| `update_document` | Re-index from original source | `document_id` |

**`update_document`** looks up the document's original `source_ref`, deletes the old
record (chunks and tags included), and re-fetches or re-parses from source. Tags are
preserved. Works for: `pdf`, `doc`, `md`, `txt`, `url`, `chat`.

### Resources (always-on context)

| Resource | Description |
|---|---|
| `brain://stats` | Document count, chunk count, unique tag count, DB size |
| `brain://recent` | 10 most recently indexed documents |

---

## Supported Source Types

| Type | Tool | What is stored |
|---|---|---|
| `note` | `add_note` | Plain text, optional `source_ref` |
| `pdf` | `add_document` | Text per page; **page number** on every chunk |
| `doc` | `add_document` | Word document paragraphs (`.docx`) |
| `pptx` | `add_document` | PowerPoint slides + speaker notes; **slide number** on every chunk |
| `xlsx` | `add_document` | Excel workbook; one chunk per sheet |
| `numbers` | `add_document` | Apple Numbers spreadsheet; one chunk per table |
| `epub` | `add_document` | Ebook; one chunk per chapter with **chapter number** on every chunk |
| `md` | `add_document` | Markdown with YAML front matter; **heading-aware chunks** |
| `txt` | `add_document` | Plain text (`.txt`, `.rst`) |
| `url` | `add_url` | Cleaned article text via trafilatura |
| `email` | `add_email` / `add_mbox` | Full headers + body; **Message-ID** as dedup key |
| `chat` | `add_chat` | Full conversation; **conversation UUID** as dedup key |

---

## Source Metadata

Each source type captures different provenance information, stored in the `documents`
table and returned with search results.

### PDF
- `source_ref` — absolute file path
- `author` — from PDF internal metadata
- `metadata.file_modified` — file modification timestamp (UTC ISO)
- `metadata.page_count` — total pages
- **`page_number` on every chunk** — search results cite the exact page

### DOCX
- `source_ref` — absolute file path
- `author` — from Word core properties
- `metadata.file_modified` — file modification timestamp

### PowerPoint (.pptx)
- `source_ref` — absolute file path
- `title` — from the title placeholder of slide 1, or filename
- `metadata.slide_count` — total slides
- **`page_number` on every chunk** — search results cite the exact slide number
- Speaker notes are included in each slide's chunk text

### Excel (.xlsx)
- `source_ref` — absolute file path
- `metadata.sheet_count` — number of sheets
- One chunk per sheet; cells are tab-separated, empty rows skipped

### Apple Numbers (.numbers)
- `source_ref` — absolute file path
- One chunk per table, labelled with sheet and table name
- Requires `numbers-parser` (included in default dependencies)

### EPUB (.epub)
- `source_ref` — absolute file path
- `title` — from Dublin Core metadata, or filename
- `author` — from Dublin Core metadata
- `metadata.chapter_count` — number of non-empty chapters ingested
- **`page_number` on every chunk** — search results cite the chapter number
- Navigation pages, covers, and near-empty fragments are skipped automatically

### Pages / Keynote (.pages, .key)
These formats use a proprietary binary format and cannot be imported directly.
Export to a supported format first:
- **Pages** → File → Export To → Word (`.docx`) or PDF
- **Keynote** → File → Export To → PowerPoint (`.pptx`) or PDF

Alternatively, bulk-convert with LibreOffice (if installed):
```bash
soffice --headless --convert-to pdf *.pages
soffice --headless --convert-to pptx *.key
```

### Markdown
- `source_ref` — absolute file path
- `title` — from `title:` front matter, first `# H1`, or filename (in that order)
- `author` — from `author:` front matter
- `created_at` — from `date:` front matter
- `metadata.file_modified` — file modification timestamp
- Tags from front matter `tags:` or `categories:` merged with caller-supplied tags
- Chunked at `#` / `##` heading boundaries — each chunk carries its section heading

### Web URL
- `source_ref` — the URL (used for dedup on re-ingest)
- `author` — page byline if detectable
- `title` — page title

### Email / Mbox
- `source_ref` — `Message-ID` header (dedup key; same email never stored twice)
- `author` — `From` header
- `created_at` — `Date` header
- `metadata.from`, `metadata.to`, `metadata.cc`, `metadata.subject`
- `metadata.thread_id` — root `Message-ID` from `References` / `In-Reply-To`;
  all replies in a thread share this value, making threads queryable

**Bulk mbox ingestion** — use `add_mbox` with `skip_senders` to filter noise before it
reaches the database:

```
add_mbox(
  path="~/Mail/Projects.mbox",
  tags=["email", "projects"],
  skip_senders=["noreply@", "no-reply@", "newsletter@", "@notifications.", "donotreply@"],
  min_body_length=100
)
```

Common mbox export locations:
- **Apple Mail** — `~/Library/Mail/V10/<account>/` (`.mbox` folders per mailbox)
- **Thunderbird** — profile folder, no extension (rename to `.mbox` if needed)
- **Gmail Takeout** — `Takeout/Mail/All mail Including Spam and Trash.mbox`
- **Fastmail** — Settings → Export → mbox

The same email re-ingested will be skipped automatically via Message-ID dedup.
Pass `force=True` to replace previously ingested versions.

### Chat
- `source_ref` — conversation UUID (dedup key)
- `created_at` — conversation start timestamp
- `metadata.message_count` — number of messages
- `metadata.updated_at` — last message timestamp (Claude export)

Supported chat formats:
- **Claude.ai export** — Settings → Data & Privacy → Export Data → `conversations.json`
- **Generic JSON** — array or object with a `messages` array (`role` + `content` per message)

---

## Managing Multiple Databases

The `brain` CLI creates and removes databases on demand, updating the Claude Desktop
config automatically. Restart Claude Desktop after any change.

### Create a database

```bash
# SQLite + MLX (default — zero infrastructure)
uv run brain create personal

# SQLite with a different embedder
uv run brain create research --embedder ollama

# Postgres (creates the database and runs migrations automatically)
uv run brain create work-docs --backend postgres
uv run brain create work-docs --backend postgres --pg-url postgresql://brain:brain@nas-ip:5432

# Temporary brain (easy to delete later)
uv run brain create temp-experiment
```

Default SQLite databases are stored in `~/.brain/<name>.db`.

### List all registered databases

```bash
uv run brain list
```

```
NAME                 BACKEND    EMBEDDER       DATABASE
--------------------------------------------------------------------------------
personal             sqlite     mlx            ~/.brain/personal.db
research             sqlite     ollama         ~/.brain/research.db
work-docs            postgres   mlx            postgresql://.../brain_work_docs
```

### Remove a database

```bash
# Deregister only (keeps the data file)
uv run brain remove temp-experiment

# Deregister and permanently delete the data
uv run brain remove temp-experiment --delete-data

# For Postgres, supply the admin URL if different from default
uv run brain remove work-docs --delete-data --pg-url postgresql://brain:brain@nas-ip:5432
```

### Ingest a directory of files

```bash
# Ingest all supported files from a folder
uv run brain ingest ~/Documents/Notes --name personal

# With tags
uv run brain ingest ~/Documents/Reports --name work-docs --tags work,reports

# Flat directory only (no subdirectories)
uv run brain ingest ~/Desktop/PDFs --name shared --no-recursive

# Re-ingest files that have changed
uv run brain ingest ~/Documents/Notes --name personal --force

# Custom file extensions
uv run brain ingest ~/data --name research --extensions .pdf,.md,.txt
```

Supported extensions by default: `.pdf`, `.md`, `.txt`, `.docx`, `.pptx`, `.xlsx`, `.numbers`, `.epub`

Files already indexed are skipped automatically (shown as `–`). Use `--force` to re-ingest.

### Available options

| Option | Default | Values |
|---|---|---|
| `--backend` | `sqlite` | `sqlite`, `postgres` |
| `--embedder` | `mlx` | `mlx`, `ollama`, `nlembedding`, `openai` |
| `--sqlite-dir` | `~/.brain` | any path |
| `--pg-url` | `postgresql://brain:brain@localhost:5432` | Postgres admin URL |

---

## Multiple Databases (manual config)

To maintain separate isolated stores (e.g. work vs personal), create additional
PostgreSQL databases and add a second MCP server entry:

```bash
# Create additional database (with the container running)
make psql
CREATE DATABASE brain_work;
\c brain_work
\i /docker-entrypoint-initdb.d/001_initial.sql
\i /docker-entrypoint-initdb.d/002_page_numbers.sql
\q
```

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "brain": {
      "command": "uv",
      "args": ["--directory", "/Users/tmoore/workspace/brain", "run", "python", "-m", "brain.server"],
      "env": { "DATABASE_URL": "postgresql://brain:brain@localhost:5432/brain", "OLLAMA_URL": "http://localhost:11434" }
    },
    "brain-work": {
      "command": "uv",
      "args": ["--directory", "/Users/tmoore/workspace/brain", "run", "python", "-m", "brain.server"],
      "env": { "DATABASE_URL": "postgresql://brain:brain@localhost:5432/brain_work", "OLLAMA_URL": "http://localhost:11434" }
    }
  }
}
```

Each entry runs a separate MCP server process. Tools appear in Claude namespaced by
server name: `brain/search`, `brain-work/add_note`, etc.

---

## Alternative: Shared Postgres on a NAS (multi-user)

If multiple users on different machines need to share the same brain, run Postgres
on a NAS instead of locally. Each Mac runs its own MCP server and Ollama instance
(embedding stays local and fast); all point at the same remote database.

```
Mac 1                            NAS (e.g. Synology with x86 CPU)
├── Claude Desktop               └── Container Manager / Docker
├── brain MCP server (stdio)         └── postgres + pgvector
└── Ollama (local embed)
              ↘ shared DATABASE_URL
Mac 2
├── Claude Desktop
├── brain MCP server (stdio)
└── Ollama (local embed)
```

**Requirements:**
- NAS must support Docker (Synology: Container Manager package)
- x86/x86_64 CPU recommended — pgvector uses SIMD; ARM NAS models will be slower
- Postgres port 5432 reachable from each Mac (LAN or VPN — do not expose publicly)

**Setup:**

1. On the NAS, run the standard Postgres + pgvector Docker image:

```bash
docker run -d \
  --name brain-db \
  -e POSTGRES_USER=brain \
  -e POSTGRES_PASSWORD=brain \
  -e POSTGRES_DB=brain \
  -p 5432:5432 \
  -v brain_pgdata:/var/lib/postgresql/data \
  pgvector/pgvector:pg16
```

2. Run migrations once (from any Mac with psql, or `docker exec`):

```bash
psql postgresql://brain:brain@<NAS_IP>:5432/brain -f migrations/001_initial.sql
psql postgresql://brain:brain@<NAS_IP>:5432/brain -f migrations/002_page_numbers.sql
```

3. On each Mac, update `.env` and `claude_desktop_config.json` to point at the NAS:

```
DATABASE_URL=postgresql://brain:brain@<NAS_IP>:5432/brain
```

No other changes needed — the MCP server, embedding, and ingestion code are identical.

**Important:** all users must use the same Ollama embedding model and version.
Vectors from different models are not comparable and will produce incorrect search results.

---

## Alternative: sqlite-vec (single-user / offline-first)

[sqlite-vec](https://github.com/asg017/sqlite-vec) is a SQLite extension that adds
vector search. It requires no server — the database is a single `.db` file. No Docker,
no Postgres, no container runtime needed.

**When to use it:**
- Single user, single machine
- You want zero infrastructure — just a file
- You want a portable DB you can copy, back up, or put on Synology Drive

**When NOT to use it:**
- Multiple users writing concurrently — SQLite has file-level write locking
- DB file stored directly on a network share (SMB/NFS) — concurrent writes risk corruption

### Setup

**1. No database server to start** — the `.db` file is created automatically on first run.

**2. Configure Claude Desktop** — set `DATABASE_URL` to a `sqlite://` path:

```json
{
  "mcpServers": {
    "brain": {
      "command": "uv",
      "args": [
        "--directory", "/Users/tmoore/workspace/brain",
        "run", "python", "-m", "brain.server"
      ],
      "env": {
        "DATABASE_URL": "sqlite:///Users/tmoore/brain.db",
        "EMBEDDER_BACKEND": "mlx",
        "MLX_MODEL": "mlx-community/nomic-embed-text-v1.5"
      }
    }
  }
}
```

The path after `sqlite://` is an absolute filesystem path. The file and any missing
parent directories are created automatically.

**3. Restart Claude Desktop.** No migrations to run — the schema is initialised
automatically on first connection.

### Combining with embedding backends

sqlite-vec pairs especially well with the MLX or NLEmbedding backends — the result is
a completely server-free, offline-capable setup:

| Setup | Infrastructure needed |
|---|---|
| sqlite-vec + MLX | None — file DB + in-process embedding |
| sqlite-vec + NLEmbedding | None — file DB + built-in macOS model (512-dim, use `VECTOR_DIMENSIONS=512`) |
| sqlite-vec + Ollama | Ollama process only |
| sqlite-vec + OpenAI | Internet connection + API key |

For NLEmbedding (512-dim vectors) set an additional env var:
```json
"VECTOR_DIMENSIONS": "512"
```

### Limitations vs PostgreSQL

- `hybrid_search` merges vector and FTS results in Python (two queries) rather than
  a single SQL CTE — functionally equivalent, negligibly slower
- No pgAdmin browser
- Not suitable for concurrent writes from multiple processes

---

## pgAdmin (optional DB browser)

Available at [http://localhost:5050](http://localhost:5050) when the stack is running.

| | Docker | Apple container |
|---|---|---|
| Login email | `admin@brain.local` | `admin@brain.local` |
| Login password | `admin` | `admin` |
| Server host | `brain-db` | `host.docker.internal` or Mac LAN IP |
| Server port | `5432` | `5432` |
| Database | `brain` | `brain` |
| User | `brain` | `brain` |
| Password | `brain` | `brain` |

> With Docker Compose, pgAdmin reaches PostgreSQL via the internal Docker network using
> the container name `brain-db`. With Apple's container tool, containers run in
> separate VMs with no shared network, so pgAdmin must connect via the host address.

---

## Project Structure

```
brain/
├── docker-compose.yml              PostgreSQL + pgAdmin (Docker)
├── scripts/
│   └── apple-container.sh          PostgreSQL + pgAdmin (Apple container CLI)
├── .env                            Environment variables (git-ignored)
├── Makefile                        All commands — docker and apple-* targets
├── migrations/
│   ├── 001_initial.sql             Schema: tables, indexes, pgvector extension (768-dim)
│   ├── 001_initial_512.sql         Variant schema for NLEmbedding backend (512-dim)
│   └── 002_page_numbers.sql        Adds page_number column to chunks
├── claude_desktop_config.json      Paste into Claude Desktop config
├── SETUP.md                        This file
├── brain/
│   ├── cli.py                      brain CLI — create / list / remove databases
│   ├── server.py                   MCP server — all tools and resources
│   ├── db.py                       PostgreSQL + pgvector database layer
│   ├── db_sqlite.py                SQLite + sqlite-vec database layer
│   ├── embedder.py                 Multi-backend embedder (Ollama, MLX, NLEmbedding, OpenAI)
│   └── ingestion/
│       ├── pipeline.py             Orchestrates parse → chunk → embed → store
│       ├── chunker.py              Generic text splitting (langchain)
│       └── parsers/
│           ├── pdf.py              PyMuPDF — page-aware extraction
│           ├── document.py         DOCX, PPTX, XLSX, Numbers, EPUB, plain text
│           ├── markdown.py         Front matter + heading-aware chunking
│           ├── url.py              trafilatura web extraction
│           ├── email_parser.py     Python stdlib email + thread linking
│           └── chat.py             Claude export + generic JSON chat
└── pyproject.toml
```

---

## Troubleshooting

**"Connection refused" on startup**
The database is not running. Run `make up` (Docker) or `make apple-up` (Apple) and
wait a few seconds before retrying.

**"Could not connect to Ollama"**
Start Ollama: `ollama serve` or open the Ollama.app. Confirm with `ollama list`.
Not needed if using the `mlx`, `nlembedding`, or `openai` backends.

**"model not found" on embedding**
Run `ollama pull nomic-embed-text`. Only applies to the Ollama backend.

**MLX: slow on first run**
The model downloads from Hugging Face (~270 MB) and is cached in `~/.cache/huggingface/`.
Subsequent starts are fast.

**NLEmbedding: "sentence model unavailable"**
Requires macOS 12+. If language models are missing, go to System Settings → General →
Language & Region and ensure English is in your preferred languages list.

**OpenAI: "Incorrect API key"**
Verify `OPENAI_API_KEY` is set in the `env` block of `claude_desktop_config.json`.
Note that every embedding call (ingest and search) hits the OpenAI API.

**Brain tools not appearing in Claude**
Confirm the container is running, then fully quit (⌘Q) and relaunch Claude Desktop.
Check logs at `~/Library/Logs/Claude/mcp-server-brain.log`.

**"Already indexed … Pass force=True to re-index"**
The document was previously ingested. Use `force=True` on the tool, or call
`update_document` with the existing document ID.

**Apple container: `'container' CLI not found`**
Download from [github.com/apple/container/releases](https://github.com/apple/container/releases).

**Apple container: kernel install prompt on first run**
`container system start` downloads a Linux kernel (~50 MB) on first use. Accept the
prompt — it only happens once.

**Apple container: requires macOS 26**
Use Docker Desktop on macOS 13–25.

**Wipe everything and start fresh**
```bash
make reset         # Docker — destroys the named volume
make apple-reset   # Apple — wipes data/postgres/ directory
```
