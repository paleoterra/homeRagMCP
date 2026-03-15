"""
Enhanced Markdown parser.

Features:
- YAML front matter extraction (title, author, date, tags)
- Title from first H1 heading if no front matter
- Heading-aware chunking: splits at H1/H2 boundaries so each chunk
  carries its section heading as context, making search much more precise.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_HEADING_SPLIT_RE = re.compile(r"^(#{1,2} .+)$", re.MULTILINE)


@dataclass
class ParsedMarkdown:
    title: str
    content: str                                  # body without front matter
    author: str | None = None
    created_at: object = None
    front_matter_tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def parse_markdown(path: str | Path) -> ParsedMarkdown:
    """Parse a Markdown file, extracting front matter and structure."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="replace")

    # --- Front matter ---
    fm_data: dict = {}
    content = raw
    match = _FRONT_MATTER_RE.match(raw)
    if match:
        try:
            fm_data = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            fm_data = {}
        content = raw[match.end():]

    # --- Title resolution: front matter → first H1 → filename ---
    title = (
        str(fm_data.get("title", "")).strip()
        or (m.group(1).strip() if (m := _H1_RE.search(content)) else "")
        or path.stem
    )

    # --- Author ---
    author = str(fm_data.get("author", "")).strip() or None

    # --- Date ---
    created_at = None
    if "date" in fm_data:
        d = fm_data["date"]
        if isinstance(d, datetime):
            created_at = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        elif isinstance(d, date):
            created_at = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)

    # --- Tags ---
    raw_tags = fm_data.get("tags") or fm_data.get("categories") or []
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",")]
    front_matter_tags = [str(t).lower().strip() for t in raw_tags if t]

    mtime = os.path.getmtime(path)
    file_modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

    return ParsedMarkdown(
        title=title,
        content=content.strip(),
        author=author,
        created_at=created_at,
        front_matter_tags=front_matter_tags,
        metadata={
            "file": str(path),
            "file_modified": file_modified,
            "front_matter": fm_data,
        },
    )


def chunk_markdown_sections(content: str) -> list[dict]:
    """Split markdown at H1/H2 boundaries.

    Each chunk contains a heading (for context) followed by the section body.
    Sections longer than ~1500 characters are split further with the generic chunker.
    """
    from brain.ingestion.chunker import chunk_text

    # re.split with a capturing group includes the matched headings in the result:
    # ['preamble', '## Heading 1', 'body 1', '## Heading 2', 'body 2', ...]
    parts = _HEADING_SPLIT_RE.split(content)

    result: list[dict] = []

    # Preamble (content before the first heading)
    preamble = parts[0].strip()
    if preamble:
        for chunk in chunk_text(preamble):
            result.append({"content": chunk, "page_number": None})

    # Alternating heading / body pairs
    for i in range(1, len(parts) - 1, 2):
        heading = parts[i].strip()
        body = parts[i + 1].strip() if (i + 1) < len(parts) else ""
        section = f"{heading}\n\n{body}" if body else heading

        if len(section) > 1500:
            for chunk in chunk_text(section):
                result.append({"content": chunk, "page_number": None})
        elif section:
            result.append({"content": section, "page_number": None})

    return result or [{"content": content, "page_number": None}]
