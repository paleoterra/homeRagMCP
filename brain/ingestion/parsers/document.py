"""DOCX, PowerPoint, Excel, Numbers, EPUB, and plain text parsers."""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ParsedDocument:
    title: str
    content: str
    author: str | None = None
    metadata: dict | None = None
    pre_chunks: list[dict] | None = None  # slide/sheet-aware chunks, if applicable


def _file_mtime(path: Path) -> str:
    import os
    from datetime import datetime, timezone
    mtime = os.path.getmtime(path)
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def parse_docx(path: str | Path) -> ParsedDocument:
    """Extract text from a Word document."""
    try:
        import docx
    except ImportError:
        raise ImportError("python-docx is required: uv add python-docx")

    path = Path(path)
    doc = docx.Document(str(path))

    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    content = "\n\n".join(paragraphs)

    props = doc.core_properties
    title = props.title or path.stem
    author = props.author or None

    return ParsedDocument(
        title=title,
        content=content,
        author=author,
        metadata={"file": str(path), "file_modified": _file_mtime(path)},
    )


def parse_text(path: str | Path) -> ParsedDocument:
    """Read a plain text or markdown file."""
    path = Path(path)
    content = path.read_text(encoding="utf-8", errors="replace")
    return ParsedDocument(
        title=path.stem,
        content=content,
        metadata={"file": str(path), "file_modified": _file_mtime(path)},
    )


def parse_pptx(path: str | Path) -> ParsedDocument:
    """Extract text from a PowerPoint presentation (.pptx).

    Each slide becomes a chunk (stored as pre_chunks), including speaker notes.
    Slide number is stored as page_number so search results cite the slide.
    """
    try:
        from pptx import Presentation
        from pptx.enum.shapes import PP_PLACEHOLDER
    except ImportError:
        raise ImportError("python-pptx is required: uv add python-pptx")

    path = Path(path)
    prs = Presentation(str(path))

    # Try to get a meaningful title from the first slide's title placeholder
    title = path.stem
    if prs.slides:
        for shape in prs.slides[0].placeholders:
            if shape.placeholder_format.idx == 0 and shape.has_text_frame:
                candidate = shape.text_frame.text.strip()
                if candidate:
                    title = candidate
                    break

    pre_chunks: list[dict] = []
    all_texts: list[str] = []

    for slide_num, slide in enumerate(prs.slides, 1):
        parts: list[str] = []

        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            text = shape.text_frame.text.strip()
            if text:
                parts.append(text)

        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"[Notes] {notes}")

        if parts:
            chunk_text = f"[Slide {slide_num}]\n" + "\n".join(parts)
            pre_chunks.append({"content": chunk_text, "page_number": slide_num})
            all_texts.append(chunk_text)

    content = "\n\n".join(all_texts)
    return ParsedDocument(
        title=title,
        content=content,
        metadata={
            "file": str(path),
            "file_modified": _file_mtime(path),
            "slide_count": len(prs.slides),
        },
        pre_chunks=pre_chunks,
    )


def parse_excel(path: str | Path) -> ParsedDocument:
    """Extract text from an Excel workbook (.xlsx).

    Each sheet becomes a chunk. Cells are tab-separated; empty rows are skipped.
    """
    try:
        import openpyxl
    except ImportError:
        raise ImportError("openpyxl is required: uv add openpyxl")

    path = Path(path)
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)

    pre_chunks: list[dict] = []
    all_texts: list[str] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows: list[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            if any(c.strip() for c in cells):
                rows.append("\t".join(cells))
        if rows:
            chunk_text = f"[Sheet: {sheet_name}]\n" + "\n".join(rows)
            pre_chunks.append({"content": chunk_text, "page_number": None})
            all_texts.append(chunk_text)

    wb.close()
    content = "\n\n".join(all_texts)
    return ParsedDocument(
        title=path.stem,
        content=content,
        metadata={
            "file": str(path),
            "file_modified": _file_mtime(path),
            "sheet_count": len(wb.sheetnames),
        },
        pre_chunks=pre_chunks,
    )


def parse_epub(path: str | Path) -> ParsedDocument:
    """Extract text from an EPUB ebook.

    Each chapter becomes a chunk. Chapter number is stored as page_number.
    Requires ebooklib and beautifulsoup4.
    """
    try:
        import ebooklib
        from bs4 import BeautifulSoup
        from ebooklib import epub
    except ImportError:
        raise ImportError(
            "ebooklib and beautifulsoup4 are required: uv add ebooklib beautifulsoup4"
        )

    import re

    path = Path(path)
    book = epub.read_epub(str(path))

    raw_title = book.get_metadata("DC", "title")
    title = raw_title[0][0] if raw_title else path.stem

    raw_author = book.get_metadata("DC", "creator")
    author = raw_author[0][0] if raw_author else None

    pre_chunks: list[dict] = []
    all_texts: list[str] = []
    chapter_num = 0

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if len(text) < 50:
            # Skip nav pages, covers, and near-empty fragments
            continue

        chapter_num += 1
        heading = soup.find(["h1", "h2", "h3"])
        chapter_label = heading.get_text(strip=True) if heading else f"Chapter {chapter_num}"

        chunk_text = f"[{chapter_label}]\n{text}"
        pre_chunks.append({"content": chunk_text, "page_number": chapter_num})
        all_texts.append(chunk_text)

    content = "\n\n".join(all_texts)
    return ParsedDocument(
        title=title,
        content=content,
        author=author,
        metadata={
            "file": str(path),
            "file_modified": _file_mtime(path),
            "chapter_count": chapter_num,
        },
        pre_chunks=pre_chunks,
    )


def parse_numbers(path: str | Path) -> ParsedDocument:
    """Extract text from an Apple Numbers spreadsheet (.numbers).

    Each table becomes a chunk. Requires the numbers-parser package.
    """
    try:
        import numbers_parser
    except ImportError:
        raise ImportError("numbers-parser is required: uv add numbers-parser")

    path = Path(path)
    doc = numbers_parser.Document(str(path))

    pre_chunks: list[dict] = []
    all_texts: list[str] = []

    for sheet in doc.sheets:
        for table in sheet.tables:
            rows: list[str] = []
            for row in table.iter_rows():
                cells = [str(c.value) if c.value is not None else "" for c in row]
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells))
            if rows:
                label = f"[Sheet: {sheet.name} / Table: {table.name}]"
                chunk_text = f"{label}\n" + "\n".join(rows)
                pre_chunks.append({"content": chunk_text, "page_number": None})
                all_texts.append(chunk_text)

    content = "\n\n".join(all_texts)
    return ParsedDocument(
        title=path.stem,
        content=content,
        metadata={"file": str(path), "file_modified": _file_mtime(path)},
        pre_chunks=pre_chunks,
    )
