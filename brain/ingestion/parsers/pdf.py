"""PDF parser using PyMuPDF (fitz)."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedPDF:
    title: str
    content: str                          # full joined text (stored on document)
    pages: list[tuple[int, str]]          # (1-based page number, page text)
    author: str | None = None
    metadata: dict = field(default_factory=dict)


def parse_pdf(path: str | Path) -> ParsedPDF:
    """Extract text from a PDF file, preserving per-page structure."""
    try:
        import fitz  # pymupdf
    except ImportError:
        raise ImportError("pymupdf is required: uv add pymupdf")

    import os

    path = Path(path)
    doc = fitz.open(str(path))

    pages: list[tuple[int, str]] = []
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text").strip()
        if text:
            pages.append((i, text))

    content = "\n\n".join(text for _, text in pages)

    meta = doc.metadata or {}
    title = meta.get("title") or path.stem
    author = meta.get("author") or None

    mtime = os.path.getmtime(path)
    from datetime import datetime, timezone
    file_modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

    doc.close()

    return ParsedPDF(
        title=title,
        content=content,
        pages=pages,
        author=author,
        metadata={
            "page_count": len(pages),
            "file": str(path),
            "file_modified": file_modified,
        },
    )
