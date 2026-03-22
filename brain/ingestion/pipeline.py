"""
Ingestion pipeline: parse → chunk → embed → store.

All ingest_* functions return the document UUID as a string.
Pass force=True to re-index a document that already exists at the same source_ref.
"""

from pathlib import Path

from brain.db import Database
from brain.embedder import Embedder
from brain.ingestion.chunker import chunk_text


async def _store(
    db: Database,
    embedder: Embedder,
    title: str,
    content: str,
    source_type: str,
    source_ref: str | None = None,
    author: str | None = None,
    created_at=None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
    pre_chunks: list[dict] | None = None,  # page-aware chunks from PDF parser
    force: bool = False,
) -> str:
    """Common storage path for all parsers.

    If source_ref is provided and a document with that ref already exists:
    - force=False → raises ValueError with the existing document ID
    - force=True  → deletes the existing document and re-indexes
    """
    # Postgres rejects null bytes in UTF8 text columns — strip them
    title = title.replace("\x00", "") if title else title
    content = content.replace("\x00", "") if content else content

    # Upsert logic
    if source_ref:
        existing_id = await db.find_by_source_ref(source_ref)
        if existing_id:
            if not force:
                raise ValueError(
                    f"Already indexed as {existing_id}. Pass force=True to re-index."
                )
            await db.delete_document(existing_id)

    doc_id = await db.add_document(
        title=title,
        content=content,
        source_type=source_type,
        source_ref=source_ref,
        author=author,
        created_at=created_at,
        metadata=metadata,
    )

    # Build chunks — use pre_chunks if provided (e.g. PDF pages), otherwise chunk content
    if pre_chunks is not None:
        chunks_to_embed = pre_chunks
    else:
        chunks_to_embed = [
            {"content": c, "page_number": None}
            for c in chunk_text(content)
        ]

    if chunks_to_embed:
        chunks = []
        for i, pc in enumerate(chunks_to_embed):
            chunk_content = pc["content"].replace("\x00", "")
            embedding = await embedder.embed(chunk_content)
            chunks.append(
                {
                    "index": i,
                    "content": chunk_content,
                    "embedding": embedding,
                    "token_count": len(pc["content"].split()),
                    "page_number": pc.get("page_number"),
                }
            )
        await db.add_chunks(doc_id, chunks)

    if tags:
        await db.add_tags(doc_id, tags)

    return doc_id


async def ingest_text(
    db: Database,
    embedder: Embedder,
    content: str,
    title: str = "",
    source_type: str = "note",
    source_ref: str | None = None,
    tags: list[str] | None = None,
    force: bool = False,
) -> str:
    """Ingest a plain text note."""
    return await _store(
        db, embedder, title or "Note", content,
        source_type=source_type, source_ref=source_ref, tags=tags, force=force,
    )


async def ingest_file(
    db: Database,
    embedder: Embedder,
    path: str,
    tags: list[str] | None = None,
    force: bool = False,
) -> str:
    """Ingest a local file. Supports PDF, DOCX, TXT, MD."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = p.suffix.lower()

    if suffix == ".pdf":
        from brain.ingestion.parsers.pdf import parse_pdf
        parsed = parse_pdf(p)
        # Build page-aware pre_chunks so each chunk knows its source page
        pre_chunks = [
            {"content": chunk, "page_number": page_num}
            for page_num, page_text in parsed.pages
            for chunk in chunk_text(page_text)
            if chunk.strip()
        ]
        return await _store(
            db, embedder,
            parsed.title, parsed.content,
            source_type="pdf",
            source_ref=str(p),
            author=parsed.author,
            metadata=parsed.metadata,
            tags=tags,
            pre_chunks=pre_chunks,
            force=force,
        )

    elif suffix in {".md", ".markdown"}:
        from brain.ingestion.parsers.markdown import chunk_markdown_sections, parse_markdown
        parsed = parse_markdown(p)
        pre_chunks = chunk_markdown_sections(parsed.content)
        # Merge tags from front matter with caller-supplied tags
        all_tags = list(set((tags or []) + parsed.front_matter_tags))
        return await _store(
            db, embedder,
            parsed.title, parsed.content,
            source_type="md",
            source_ref=str(p),
            author=parsed.author,
            created_at=parsed.created_at,
            metadata=parsed.metadata,
            tags=all_tags,
            pre_chunks=pre_chunks,
            force=force,
        )

    elif suffix in {".docx", ".doc"}:
        from brain.ingestion.parsers.document import parse_docx
        parsed = parse_docx(p)
        source_type = "doc"
    elif suffix in {".pptx"}:
        from brain.ingestion.parsers.document import parse_pptx
        parsed = parse_pptx(p)
        source_type = "pptx"
    elif suffix in {".xlsx"}:
        from brain.ingestion.parsers.document import parse_excel
        parsed = parse_excel(p)
        source_type = "xlsx"
    elif suffix in {".numbers"}:
        from brain.ingestion.parsers.document import parse_numbers
        parsed = parse_numbers(p)
        source_type = "numbers"
    elif suffix in {".epub"}:
        from brain.ingestion.parsers.document import parse_epub
        parsed = parse_epub(p)
        source_type = "epub"
    elif suffix in {".txt", ".rst"}:
        from brain.ingestion.parsers.document import parse_text
        parsed = parse_text(p)
        source_type = "txt"
    elif suffix in {".ppt", ".xls", ".key", ".pages"}:
        raise ValueError(
            f"Direct import of {suffix} is not supported. "
            f"Export to a supported format first:\n"
            f"  .ppt  → Save As .pptx in PowerPoint, or export to PDF\n"
            f"  .xls  → Save As .xlsx in Excel\n"
            f"  .key  → File → Export To → PDF in Keynote\n"
            f"  .pages → File → Export To → Word or PDF in Pages\n"
            f"Alternatively, install LibreOffice and convert via:\n"
            f"  soffice --headless --convert-to pdf '{p}'"
        )
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    return await _store(
        db, embedder,
        parsed.title, parsed.content,
        source_type=source_type,
        source_ref=str(p),
        author=parsed.author,
        metadata=parsed.metadata,
        tags=tags,
        pre_chunks=parsed.pre_chunks,
        force=force,
    )


async def ingest_url(
    db: Database,
    embedder: Embedder,
    url: str,
    tags: list[str] | None = None,
    force: bool = False,
) -> str:
    """Fetch and ingest a web page."""
    from brain.ingestion.parsers.url import parse_url
    parsed = await parse_url(url)
    return await _store(
        db, embedder,
        parsed.title, parsed.content,
        source_type="url",
        source_ref=url,
        author=parsed.author,
        metadata=parsed.metadata,
        tags=tags,
        force=force,
    )


async def ingest_email(
    db: Database,
    embedder: Embedder,
    raw_mime: str,
    tags: list[str] | None = None,
    force: bool = False,
) -> str:
    """Ingest a raw MIME email string."""
    from brain.ingestion.parsers.email_parser import parse_email
    parsed = parse_email(raw_mime)
    return await _store(
        db, embedder,
        parsed.title, parsed.content,
        source_type="email",
        source_ref=parsed.source_ref,   # Message-ID
        author=parsed.author,
        created_at=parsed.created_at,
        metadata=parsed.metadata,
        tags=tags,
        force=force,
    )


async def ingest_mbox(
    db: Database,
    embedder: Embedder,
    path: str,
    tags: list[str] | None = None,
    force: bool = False,
    skip_senders: list[str] | None = None,
    min_body_length: int = 50,
) -> dict:
    """Bulk-ingest an mbox archive file.

    Returns a dict with keys: ingested, skipped, failed, skip_reasons.
    skip_reasons breaks down why emails were skipped (duplicate, sender_filter, too_short).
    """
    import io
    import mailbox
    from email.generator import BytesGenerator

    from brain.ingestion.parsers.email_parser import parse_email

    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    mbox = mailbox.mbox(str(p), factory=None, create=False)
    ingested = 0
    skipped = 0
    failed = 0
    skip_reasons: dict[str, int] = {}

    def _bump(reason: str) -> None:
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    try:
        for msg in mbox:
            try:
                buf = io.BytesIO()
                BytesGenerator(buf).flatten(msg)
                raw = buf.getvalue().decode("utf-8", errors="replace")

                parsed = parse_email(raw)

                # Filter: sender blocklist (substring match, case-insensitive)
                if skip_senders:
                    from_addr = (parsed.author or "").lower()
                    if any(p.lower() in from_addr for p in skip_senders):
                        skipped += 1
                        _bump("sender_filter")
                        continue

                # Filter: skip near-empty bodies
                if len(parsed.content.strip()) < min_body_length:
                    skipped += 1
                    _bump("too_short")
                    continue

                await _store(
                    db, embedder,
                    parsed.title, parsed.content,
                    source_type="email",
                    source_ref=parsed.source_ref,
                    author=parsed.author,
                    created_at=parsed.created_at,
                    metadata=parsed.metadata,
                    tags=tags,
                    force=force,
                )
                ingested += 1

            except ValueError:
                # Duplicate (force=False) — not an error
                skipped += 1
                _bump("duplicate")
            except Exception:
                failed += 1
    finally:
        mbox.close()

    return {
        "ingested": ingested,
        "skipped": skipped,
        "failed": failed,
        "skip_reasons": skip_reasons,
    }


async def ingest_chat(
    db: Database,
    embedder: Embedder,
    path: str,
    tags: list[str] | None = None,
    force: bool = False,
) -> list[str]:
    """Ingest a chat history JSON file.

    Returns a list of document IDs — one per conversation found in the file.
    Conversations without a UUID source_ref cannot be deduped across runs;
    pass force=True to overwrite ones that do have a stable ID.
    """
    from brain.ingestion.parsers.chat import parse_chat_file
    conversations = parse_chat_file(path)

    doc_ids = []
    for conv in conversations:
        doc_id = await _store(
            db, embedder,
            conv.title, conv.content,
            source_type="chat",
            source_ref=conv.source_ref,
            created_at=conv.created_at,
            metadata=conv.metadata,
            tags=tags,
            force=force,
        )
        doc_ids.append(doc_id)

    return doc_ids


async def ingest_google_item(
    db: Database,
    embedder: Embedder,
    file_id_or_url: str,
    tags: list[str] | None = None,
    force: bool = False,
) -> str:
    """Ingest a single Google Drive item (Doc, Sheet, Slide, or binary file).

    Accepts a full Google URL or a bare file ID (or gdrive://ID).
    Returns the document UUID.
    """
    from brain.ingestion.parsers.google_drive import (
        _extract_google_id,
        BinaryDriveFile,
        GoogleDriveClient,
        GoogleParsedDocument,
    )

    file_id = _extract_google_id(file_id_or_url)
    client = GoogleDriveClient()
    result = client.fetch_item(file_id)
    source_ref = f"gdrive://{file_id}"

    if isinstance(result, GoogleParsedDocument):
        return await _store(
            db, embedder,
            result.title, result.content,
            source_type=result.source_type,
            source_ref=source_ref,
            author=result.author,
            metadata=result.metadata,
            tags=tags,
            pre_chunks=result.pre_chunks,
            force=force,
        )

    # BinaryDriveFile — download to temp and parse with existing parsers
    if not isinstance(result, BinaryDriveFile):
        raise TypeError(f"Unexpected fetch_item result type: {type(result)}")
    return await _ingest_binary_drive_file(
        db, embedder, client, result, source_ref, tags=tags, force=force
    )


async def _ingest_binary_drive_file(
    db: Database,
    embedder: Embedder,
    client,
    binary: "BinaryDriveFile",
    source_ref: str,
    tags: list[str] | None = None,
    force: bool = False,
) -> str:
    """Download a binary Drive file to a temp path, parse it, and store with gdrive source_ref."""
    import tempfile
    from pathlib import Path as _Path

    data = client.download_file(binary.file_id)

    suffix = _Path(binary.name).suffix.lower() or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        title, content, src_type, pre_chunks, author, meta = _parse_file_for_storage(tmp_path, binary.name)
    finally:
        import os
        os.unlink(tmp_path)

    return await _store(
        db, embedder,
        title, content,
        source_type=src_type,
        source_ref=source_ref,
        author=author,
        metadata=meta,
        tags=tags,
        pre_chunks=pre_chunks,
        force=force,
    )


def _parse_file_for_storage(tmp_path: str, original_name: str):
    """Parse a file and return (title, content, source_type, pre_chunks, author, metadata).

    This mirrors the dispatch logic in ingest_file but returns parsed data
    rather than calling _store(), so the caller can provide a custom source_ref.
    """
    from pathlib import Path as _Path
    suffix = _Path(original_name).suffix.lower()

    if suffix == ".pdf":
        from brain.ingestion.parsers.pdf import parse_pdf
        from brain.ingestion.chunker import chunk_text
        parsed = parse_pdf(tmp_path)
        pre_chunks = [
            {"content": chunk, "page_number": page_num}
            for page_num, page_text in parsed.pages
            for chunk in chunk_text(page_text)
            if chunk.strip()
        ]
        return parsed.title, parsed.content, "pdf", pre_chunks, parsed.author, parsed.metadata

    elif suffix in {".md", ".markdown"}:
        from brain.ingestion.parsers.markdown import chunk_markdown_sections, parse_markdown
        parsed = parse_markdown(tmp_path)
        pre_chunks = chunk_markdown_sections(parsed.content)
        return parsed.title, parsed.content, "md", pre_chunks, parsed.author, parsed.metadata

    elif suffix in {".docx", ".doc"}:
        from brain.ingestion.parsers.document import parse_docx
        parsed = parse_docx(tmp_path)
        return parsed.title, parsed.content, "doc", parsed.pre_chunks, parsed.author, parsed.metadata

    elif suffix == ".pptx":
        from brain.ingestion.parsers.document import parse_pptx
        parsed = parse_pptx(tmp_path)
        return parsed.title, parsed.content, "pptx", parsed.pre_chunks, parsed.author, parsed.metadata

    elif suffix == ".xlsx":
        from brain.ingestion.parsers.document import parse_excel
        parsed = parse_excel(tmp_path)
        return parsed.title, parsed.content, "xlsx", parsed.pre_chunks, parsed.author, parsed.metadata

    elif suffix == ".numbers":
        from brain.ingestion.parsers.document import parse_numbers
        parsed = parse_numbers(tmp_path)
        return parsed.title, parsed.content, "numbers", parsed.pre_chunks, parsed.author, parsed.metadata

    elif suffix == ".epub":
        from brain.ingestion.parsers.document import parse_epub
        parsed = parse_epub(tmp_path)
        return parsed.title, parsed.content, "epub", parsed.pre_chunks, parsed.author, parsed.metadata

    elif suffix in {".txt", ".rst"}:
        from brain.ingestion.parsers.document import parse_text
        parsed = parse_text(tmp_path)
        return parsed.title, parsed.content, "txt", None, parsed.author, parsed.metadata

    else:
        # Unknown binary type — ingest as raw text best-effort
        try:
            content = open(tmp_path, encoding="utf-8", errors="replace").read()
        except Exception:
            content = ""
        return _Path(original_name).stem, content, "txt", None, None, None


async def ingest_google_folder(
    db: Database,
    embedder: Embedder,
    folder_id_or_url: str,
    tags: list[str] | None = None,
    force: bool = False,
) -> dict:
    """Recursively ingest all files in a Google Drive folder.

    Returns {"ingested": int, "skipped": int, "failed": int, "skip_reasons": dict}.
    """
    import logging

    from brain.ingestion.parsers.google_drive import (
        _extract_google_id,
        GoogleDriveClient,
        UnsupportedGoogleTypeError,
    )

    log = logging.getLogger(__name__)
    folder_id = _extract_google_id(folder_id_or_url)
    client = GoogleDriveClient()
    # fetch_folder raises ValueError for Shared Drives — this is OUTSIDE the per-file
    # try/except below, so Shared Drive errors propagate immediately (not caught/counted).
    file_ids = client.fetch_folder(folder_id)

    ingested = 0
    skipped = 0
    failed = 0
    skip_reasons: dict[str, int] = {}

    def _bump(reason: str) -> None:
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    for file_id in file_ids:
        try:
            await ingest_google_item(db, embedder, file_id, tags=tags, force=force)
            ingested += 1
        except ValueError as e:
            if "Already indexed" in str(e) or "force=True" in str(e):
                skipped += 1
                _bump("duplicate")
            else:
                # Other ValueErrors (bad URL format, etc.) — treat as failure
                failed += 1
                log.warning("Failed to ingest Drive file %s: %s", file_id, e)
        except UnsupportedGoogleTypeError:
            skipped += 1
            _bump("unsupported_type")
        except Exception as e:
            failed += 1
            log.warning("Failed to ingest Drive file %s: %s", file_id, e)

    return {
        "ingested": ingested,
        "skipped": skipped,
        "failed": failed,
        "skip_reasons": skip_reasons,
    }
