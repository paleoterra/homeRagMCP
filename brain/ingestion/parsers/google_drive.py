"""
Google Drive / Workspace parser.

Handles authentication (OAuth2 + service account), all Google API calls,
and content extraction for Docs, Sheets, Slides, and binary Drive files.

Required extras: uv sync --extra google
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class UnsupportedGoogleTypeError(Exception):
    """Raised when a Google Workspace MIME type has no supported parser."""

    def __init__(self, mime_type: str) -> None:
        self.mime_type = mime_type
        super().__init__(f"Unsupported Google Workspace type: {mime_type}")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class GoogleParsedDocument:
    """Result of parsing a Google Workspace file (Doc, Sheet, or Slide)."""

    title: str
    content: str
    source_type: str                              # "gdoc", "gsheet", "gslide"
    file_id: str
    author: str | None = None
    pre_chunks: list[dict] | None = None          # {content: str, page_number: int | None}
    metadata: dict | None = None                  # modified_time, mime_type, file_id


@dataclass
class BinaryDriveFile:
    """A non-Google binary file stored in Drive (PDF, DOCX, etc.)."""

    file_id: str
    name: str       # original filename including extension
    mime_type: str


FetchItemResult = Union[GoogleParsedDocument, BinaryDriveFile]


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_GDRIVE_SCHEME = re.compile(r"^gdrive://(.+)$")

_URL_PATTERNS = [
    # docs.google.com/document/d/<id>/...
    re.compile(r"docs\.google\.com/document/d/([^/?#]+)"),
    # docs.google.com/spreadsheets/d/<id>/...
    re.compile(r"docs\.google\.com/spreadsheets/d/([^/?#]+)"),
    # docs.google.com/presentation/d/<id>/...
    re.compile(r"docs\.google\.com/presentation/d/([^/?#]+)"),
    # drive.google.com/file/d/<id>/...
    re.compile(r"drive\.google\.com/file/d/([^/?#]+)"),
    # drive.google.com/drive/folders/<id>
    re.compile(r"drive\.google\.com/drive/folders/([^/?#]+)"),
    # drive.google.com/open?id=<id>
    re.compile(r"drive\.google\.com/open\?id=([^&]+)"),
]


def _extract_google_id(url_or_id: str) -> str:
    """Extract a Drive file/folder ID from a Google URL or return a bare ID as-is."""
    # gdrive:// scheme (used internally by source_ref)
    m = _GDRIVE_SCHEME.match(url_or_id)
    if m:
        return m.group(1)

    # Known URL patterns
    for pattern in _URL_PATTERNS:
        m = pattern.search(url_or_id)
        if m:
            return m.group(1)

    # Bare ID: valid Google ID characters (alphanumeric, hyphen, underscore)
    if re.fullmatch(r"[A-Za-z0-9_\-]+", url_or_id):
        return url_or_id

    raise ValueError(f"Could not extract Google Drive file ID from: {url_or_id}")
