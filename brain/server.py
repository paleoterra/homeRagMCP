"""
Brain MCP server.

Exposes semantic memory tools to Claude via the Model Context Protocol.
Run via: uv run python -m brain.server
"""

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

from brain.embedder import Embedder
from brain.ingestion.pipeline import ingest_chat, ingest_email, ingest_file, ingest_google_folder, ingest_google_item, ingest_mbox, ingest_text, ingest_url

# ---------------------------------------------------------------------------
# Service instances
# ---------------------------------------------------------------------------

_database_url = os.environ.get("DATABASE_URL", "postgresql://brain:brain@localhost:5432/brain")

if _database_url.startswith("sqlite://"):
    from brain.db_sqlite import SqliteDatabase
    _sqlite_path = _database_url.removeprefix("sqlite://")
    _dimensions = int(os.environ.get("VECTOR_DIMENSIONS", "768"))
    db = SqliteDatabase(_sqlite_path, dimensions=_dimensions)
else:
    from brain.db import Database
    db = Database(_database_url)
embedder = Embedder(
    backend=os.environ.get("EMBEDDER_BACKEND", "ollama"),
    base_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
    model=os.environ.get("OLLAMA_MODEL", "nomic-embed-text"),
    mlx_model=os.environ.get("MLX_MODEL", "mlx-community/nomic-embed-text-v1.5"),
    openai_model=os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
    openai_dimensions=int(os.environ.get("OPENAI_EMBED_DIMENSIONS", "768")),
    openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    await db.connect()
    yield
    await db.disconnect()


mcp = FastMCP(
    "brain",
    lifespan=lifespan,
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _fmt_results(results: list[dict]) -> str:
    if not results:
        return "No results found."
    lines = []
    for r in results:
        score = r.get("score", 0)
        title = r.get("title") or "Untitled"
        source_type = r.get("source_type", "")
        source_ref = r.get("source_ref") or r.get("id", "")
        page = r.get("page_number")
        content = r.get("content", "")
        location = f"{source_ref}" + (f" (page {page})" if page else "")
        lines.append(
            f"**{title}** [{source_type}] (score: {score:.3f})\n"
            f"ref: {location}\n"
            f"{content[:400]}{'…' if len(content) > 400 else ''}"
        )
    return "\n\n---\n\n".join(lines)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def add_note(
    content: str,
    title: str = "",
    tags: list[str] = [],
    source_ref: str = "",
    force: bool = False,
) -> str:
    """Add a quick note or piece of text to the brain for later search.

    Args:
        content: The note text.
        title: Optional short title or headline.
        tags: Optional list of tag strings (e.g. ["meeting", "project-x"]).
        source_ref: Optional unique identifier (e.g. a URL, ticket ID, or filename)
                    that lets this note be updated later with force=True.
        force: If True and source_ref matches an existing note, replace it.
    """
    try:
        doc_id = await ingest_text(
            db, embedder, content,
            title=title,
            source_ref=source_ref or None,
            tags=tags or [],
            force=force,
        )
        return f"Saved note {doc_id}"
    except ValueError as e:
        return f"Duplicate: {e}"
    except Exception as e:
        return f"Error saving note: {e}"


@mcp.tool()
async def add_document(path: str, tags: list[str] = [], force: bool = False) -> str:
    """Ingest a local file into the brain. Supports PDF, DOCX, TXT, and Markdown.

    Args:
        path: Absolute or home-relative path to the file (e.g. ~/Documents/paper.pdf).
        tags: Optional list of tag strings.
        force: If True, re-index the file even if it was previously ingested.
               Use this after the file has been updated on disk.
    """
    try:
        doc_id = await ingest_file(db, embedder, path, tags=tags or [], force=force)
        action = "Re-indexed" if force else "Ingested"
        return f"{action} {path} as document {doc_id}"
    except FileNotFoundError as e:
        return f"File not found: {e}"
    except ValueError as e:
        return str(e)   # includes "Pass force=True" hint for duplicates
    except Exception as e:
        return f"Error ingesting document: {e}"


@mcp.tool()
async def add_url(url: str, tags: list[str] = [], force: bool = False) -> str:
    """Fetch a web page and add its content to the brain.

    Args:
        url: Full URL including scheme (e.g. https://example.com/article).
        tags: Optional list of tag strings.
        force: If True, re-fetch and re-index even if this URL was previously ingested.
    """
    try:
        doc_id = await ingest_url(db, embedder, url, tags=tags or [], force=force)
        action = "Re-indexed" if force else "Ingested"
        return f"{action} {url} as document {doc_id}"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error ingesting URL: {e}"


@mcp.tool()
async def add_email(raw_mime: str, tags: list[str] = [], force: bool = False) -> str:
    """Ingest a raw MIME email string into the brain.

    The input should be the full RFC 2822 email text including headers.
    The Message-ID header is used as a unique identifier, so the same email
    will not be duplicated unless force=True is passed.

    Args:
        raw_mime: Full raw MIME email text.
        tags: Optional list of tag strings.
        force: If True, replace a previously ingested email with the same Message-ID.
    """
    try:
        doc_id = await ingest_email(db, embedder, raw_mime, tags=tags or [], force=force)
        return f"Ingested email as document {doc_id}"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error ingesting email: {e}"


@mcp.tool()
async def add_chat(path: str, tags: list[str] = [], force: bool = False) -> str:
    """Ingest a chat history JSON file into the brain.

    Each conversation in the file becomes a separate searchable document.
    Conversation UUIDs are used as source refs for deduplication, so the same
    export file can be re-ingested without creating duplicates.

    Supported formats:
    - Claude.ai export  (Settings → Data & Privacy → Export Data → conversations.json)
    - Generic JSON      (array or object with a "messages" array)

    Args:
        path: Path to the JSON chat export file.
        tags: Optional tags applied to every conversation in the file.
        force: Replace previously ingested conversations (matched by UUID).
    """
    try:
        doc_ids = await ingest_chat(db, embedder, path, tags=tags or [], force=force)
        n = len(doc_ids)
        return f"Ingested {n} conversation{'s' if n != 1 else ''} from {path}"
    except FileNotFoundError as e:
        return f"File not found: {e}"
    except ValueError as e:
        return f"Parse error: {e}"
    except Exception as e:
        return f"Error ingesting chat: {e}"


@mcp.tool()
async def add_mbox(
    path: str,
    tags: list[str] = [],
    force: bool = False,
    skip_senders: list[str] = [],
    min_body_length: int = 50,
) -> str:
    """Bulk-ingest an mbox email archive file into the brain.

    Iterates every message in the file, skipping duplicates (by Message-ID),
    near-empty bodies, and any senders matching the blocklist. Use this to
    ingest exported mailboxes from Apple Mail, Thunderbird, Gmail Takeout, etc.

    Args:
        path: Path to the .mbox file.
        tags: Tags applied to every ingested email.
        force: If True, replace previously ingested emails with the same Message-ID.
        skip_senders: List of sender substrings to ignore, e.g.
                      ["noreply@", "newsletter@", "no-reply@", "@notifications."].
                      Case-insensitive substring match against the From header.
        min_body_length: Skip emails whose body is shorter than this many characters
                         (default 50). Useful for filtering automated one-liners.
    """
    try:
        result = await ingest_mbox(
            db, embedder, path,
            tags=tags or [],
            force=force,
            skip_senders=skip_senders or None,
            min_body_length=min_body_length,
        )
        reasons = result["skip_reasons"]
        reason_str = ""
        if reasons:
            parts = [f"{v} {k}" for k, v in sorted(reasons.items())]
            reason_str = f" ({', '.join(parts)})"
        return (
            f"Ingested {result['ingested']} emails, "
            f"skipped {result['skipped']}{reason_str}, "
            f"failed {result['failed']}."
        )
    except FileNotFoundError as e:
        return f"File not found: {e}"
    except Exception as e:
        return f"Error ingesting mbox: {e}"


@mcp.tool()
async def add_google_drive_item(
    url_or_id: str,
    tags: list[str] = [],
    force: bool = False,
) -> str:
    """Ingest a single Google Drive item into the brain.

    Supports Google Docs, Sheets, Slides, and binary files (PDF, DOCX, etc.)
    stored in Drive. Accepts full Google URLs or a bare file ID.

    Args:
        url_or_id: Full Google URL (Docs/Sheets/Slides/Drive) or bare file ID.
        tags: Optional list of tag strings.
        force: If True, re-index even if this Drive item was previously ingested.
    """
    try:
        doc_id = await ingest_google_item(db, embedder, url_or_id, tags=tags or [], force=force)
        action = "Re-indexed" if force else "Ingested"
        # Fetch the title from DB — ingest_google_item only returns the UUID
        doc = await db.get_document(doc_id)
        title = (doc or {}).get("title") or "Drive item"
        return f"{action} '{title}' as document {doc_id}"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error ingesting Drive item: {e}"


@mcp.tool()
async def add_google_drive_folder(
    url_or_id: str,
    tags: list[str] = [],
    force: bool = False,
) -> str:
    """Recursively ingest all files in a Google Drive folder into the brain.

    Descends into all subfolders. Supports Google Docs, Sheets, Slides,
    and binary files (PDF, DOCX, etc.). Only personal My Drive folders
    are supported (not Shared Drives).

    Args:
        url_or_id: Full Drive folder URL or bare folder ID.
        tags: Optional tags applied to every ingested file.
        force: If True, re-index files already in the brain.
    """
    try:
        result = await ingest_google_folder(
            db, embedder, url_or_id, tags=tags or [], force=force
        )
        reasons = result["skip_reasons"]
        reason_str = ""
        if reasons:
            parts = [f"{v} {k}" for k, v in sorted(reasons.items())]
            reason_str = f" ({', '.join(parts)})"
        return (
            f"Ingested {result['ingested']} items, "
            f"skipped {result['skipped']}{reason_str}, "
            f"failed {result['failed']}."
        )
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error ingesting Drive folder: {e}"


@mcp.tool()
async def update_document(document_id: str) -> str:
    """Re-index a document from its original source.

    Fetches the current content from the original source (file, URL) and
    replaces the stored document with a fresh index. Tags are preserved.

    Works for: pdf, doc, md, txt, url, chat
    Does not work for: note, email (no stable re-fetchable source)

    Args:
        document_id: UUID of the document to update (from search or list_documents).
    """
    try:
        doc = await db.get_document(document_id)
        if not doc:
            return f"Document {document_id} not found."

        source_type = doc["source_type"]
        source_ref = doc["source_ref"]
        tags = list(doc["tags"] or [])

        if not source_ref:
            return (
                f"Document has no source reference and cannot be re-indexed automatically.\n"
                f"For notes: use add_note with the same source_ref and force=True.\n"
                f"For emails: use add_email with the original MIME and force=True."
            )

        if source_type in ("pdf", "doc", "md", "txt"):
            new_id = await ingest_file(db, embedder, source_ref, tags=tags, force=True)
            return f"Updated {source_type} — new document id: {new_id}"
        elif source_type == "url":
            new_id = await ingest_url(db, embedder, source_ref, tags=tags, force=True)
            return f"Updated {source_type} — new document id: {new_id}"
        elif source_type == "chat":
            new_ids = await ingest_chat(db, embedder, source_ref, tags=tags, force=True)
            return f"Updated chat file — {len(new_ids)} conversation(s) re-indexed"
        elif source_type in ("gdoc", "gsheet", "gslide"):
            new_id = await ingest_google_item(db, embedder, source_ref, tags=tags, force=True)
            return f"Updated {source_type} — new document id: {new_id}"
        else:
            return (
                f"Cannot auto-update source type '{source_type}'.\n"
                f"Use add_email (with force=True) or add_note (with source_ref + force=True)."
            )
    except FileNotFoundError:
        return f"Original file no longer exists at: {doc.get('source_ref')}"
    except Exception as e:
        return f"Error updating document: {e}"


# ---------------------------------------------------------------------------
# Search tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search(
    query: str,
    limit: int = 5,
    source_types: list[str] = [],
) -> str:
    """Semantic (vector) search across everything in the brain.

    Args:
        query: Natural language query — what you're looking for.
        limit: Max number of results to return (default 5).
        source_types: Filter by type: "note", "pdf", "url", "email", "doc", "txt".
    """
    try:
        embedding = await embedder.embed(query)
        results = await db.vector_search(
            embedding, limit=limit, source_types=source_types or None
        )
        return _fmt_results(results)
    except Exception as e:
        return f"Search error: {e}"


@mcp.tool()
async def hybrid_search(query: str, limit: int = 5) -> str:
    """Combined vector + full-text search using Reciprocal Rank Fusion.

    Better recall than pure vector search, especially for specific terms,
    names, or technical strings. Use this when plain search misses things.

    Args:
        query: Natural language or keyword query.
        limit: Max number of results (default 5).
    """
    try:
        embedding = await embedder.embed(query)
        results = await db.hybrid_search(query, embedding, limit=limit)
        return _fmt_results(results)
    except Exception as e:
        return f"Hybrid search error: {e}"


# ---------------------------------------------------------------------------
# Management tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_documents(
    source_type: str = "",
    tag: str = "",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Browse documents stored in the brain.

    Args:
        source_type: Filter by type ("note", "pdf", "url", "email", "doc", "txt"). Empty = all.
        tag: Filter by tag. Empty = all.
        limit: Max results (default 20).
        offset: Pagination offset.
    """
    try:
        docs = await db.list_documents(
            source_type=source_type or None,
            tag=tag or None,
            limit=limit,
            offset=offset,
        )
        if not docs:
            return "No documents found."
        lines = []
        for d in docs:
            tags_str = ", ".join(d["tags"] or [])
            indexed = d["indexed_at"].strftime("%Y-%m-%d %H:%M") if d["indexed_at"] else "?"
            lines.append(
                f"[{d['source_type']}] {d['title'] or 'Untitled'}  "
                f"id:{d['id']}  indexed:{indexed}"
                + (f"  tags:{tags_str}" if tags_str else "")
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing documents: {e}"


@mcp.tool()
async def get_document(document_id: str) -> str:
    """Retrieve the full content of a document by its ID.

    Args:
        document_id: UUID of the document (from search results or list_documents).
    """
    try:
        doc = await db.get_document(document_id)
        if not doc:
            return f"Document {document_id} not found."
        tags_str = ", ".join(doc["tags"] or [])
        return (
            f"**{doc['title'] or 'Untitled'}**\n"
            f"Type: {doc['source_type']} | Ref: {doc['source_ref'] or 'n/a'}\n"
            f"Author: {doc['author'] or 'n/a'} | Tags: {tags_str or 'none'}\n"
            f"Indexed: {doc['indexed_at']}\n\n"
            f"{doc['content']}"
        )
    except Exception as e:
        return f"Error retrieving document: {e}"


@mcp.tool()
async def tag_document(document_id: str, tags: list[str]) -> str:
    """Add tags to an existing document.

    Args:
        document_id: UUID of the document.
        tags: Tags to add.
    """
    try:
        await db.tag_document(document_id, tags)
        return f"Tagged document {document_id} with: {', '.join(tags)}"
    except Exception as e:
        return f"Error tagging document: {e}"


@mcp.tool()
async def delete_document(document_id: str) -> str:
    """Permanently delete a document and all its chunks from the brain.

    Args:
        document_id: UUID of the document to delete.
    """
    try:
        deleted = await db.delete_document(document_id)
        if deleted:
            return f"Deleted document {document_id}"
        return f"Document {document_id} not found."
    except Exception as e:
        return f"Error deleting document: {e}"


# ---------------------------------------------------------------------------
# Resources (always-available context)
# ---------------------------------------------------------------------------


@mcp.resource("brain://stats")
async def brain_stats() -> str:
    """Current database statistics."""
    try:
        s = await db.stats()
        return (
            f"Documents: {s['doc_count']}  "
            f"Chunks: {s['chunk_count']}  "
            f"Unique tags: {s['tag_count']}  "
            f"DB size: {s['db_size']}"
        )
    except Exception as e:
        return f"Stats unavailable: {e}"


@mcp.resource("brain://recent")
async def brain_recent() -> str:
    """The 10 most recently indexed documents."""
    try:
        docs = await db.list_documents(limit=10)
        if not docs:
            return "No documents yet."
        lines = []
        for d in docs:
            indexed = d["indexed_at"].strftime("%Y-%m-%d %H:%M") if d["indexed_at"] else "?"
            lines.append(f"{indexed}  [{d['source_type']}]  {d['title'] or 'Untitled'}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)  # type: ignore[arg-type]
