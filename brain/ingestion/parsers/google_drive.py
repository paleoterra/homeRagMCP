"""
Google Drive / Workspace parser.

Handles authentication (OAuth2 + service account), all Google API calls,
and content extraction for Docs, Sheets, Slides, and binary Drive files.

Required extras: uv sync --extra google
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
]

_DEFAULT_SECRETS = Path.home() / ".config" / "brain" / "client_secrets.json"
_DEFAULT_TOKEN = Path.home() / ".config" / "brain" / "google_token.json"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class GoogleAuth:
    """Resolves Google credentials from service account or OAuth2 token."""

    def get_credentials(self):
        """Return valid Google credentials, refreshing if needed.

        Resolution order:
        1. GOOGLE_SERVICE_ACCOUNT_KEY env var
        2. OAuth2 token file at GOOGLE_TOKEN_PATH
        3. RuntimeError with setup instructions
        """
        sa_key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
        if sa_key:
            return self._service_account_creds(sa_key)

        token_path = Path(os.environ.get("GOOGLE_TOKEN_PATH", str(_DEFAULT_TOKEN)))
        if token_path.exists():
            return self._oauth2_creds(token_path)

        raise RuntimeError(
            "Google credentials not configured.\n"
            "  Desktop:  brain google-auth\n"
            "  Headless: set GOOGLE_SERVICE_ACCOUNT_KEY=/path/to/key.json"
        )

    def _service_account_creds(self, key_path: str):
        from google.oauth2.service_account import Credentials
        return Credentials.from_service_account_file(key_path, scopes=SCOPES)

    def _oauth2_creds(self, token_path: Path):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        import json

        data = json.loads(token_path.read_text())
        creds = Credentials.from_authorized_user_info(data, SCOPES)

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                token_path.write_text(creds.to_json())
            except Exception as e:
                raise RuntimeError(
                    f"OAuth2 token refresh failed: {e}\n"
                    "Re-run: brain google-auth"
                ) from e

        return creds


# ---------------------------------------------------------------------------
# MIME type constants
# ---------------------------------------------------------------------------

MAX_SHEET_ROWS = 1000

_MIME_GDOC = "application/vnd.google-apps.document"
_MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
_MIME_GSLIDE = "application/vnd.google-apps.presentation"
_MIME_GFOLDER = "application/vnd.google-apps.folder"
_MIME_GAPPS_PREFIX = "application/vnd.google-apps."


# ---------------------------------------------------------------------------
# Drive client
# ---------------------------------------------------------------------------


class GoogleDriveClient:
    """Wraps Google Drive, Docs, Sheets, and Slides APIs."""

    def __init__(self) -> None:
        try:
            from googleapiclient.discovery import build
        except ImportError:
            raise ImportError(
                "Google API client is required: uv sync --extra google"
            )

        creds = GoogleAuth().get_credentials()
        self._drive = build("drive", "v3", credentials=creds)
        self._docs = build("docs", "v1", credentials=creds)
        self._sheets = build("sheets", "v4", credentials=creds)
        self._slides = build("slides", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # fetch_item
    # ------------------------------------------------------------------

    def fetch_item(self, file_id: str) -> FetchItemResult:
        """Fetch and parse a single Drive item by file ID."""
        meta = (
            self._drive.files()
            .get(fileId=file_id, fields="id,name,mimeType,modifiedTime,owners")
            .execute()
        )
        mime = meta["mimeType"]
        name = meta["name"]
        author = (meta.get("owners") or [{}])[0].get("displayName")
        modified = meta.get("modifiedTime", "")
        base_meta = {"modified_time": modified, "mime_type": mime, "file_id": file_id}

        if mime == _MIME_GFOLDER:
            raise ValueError("Use fetch_folder() for folders, not fetch_item().")

        if mime == _MIME_GDOC:
            return self._parse_doc(file_id, name, author, base_meta)

        if mime == _MIME_GSHEET:
            return self._parse_sheet(file_id, name, author, base_meta)

        if mime == _MIME_GSLIDE:
            return self._parse_slide(file_id, name, author, base_meta)

        if mime.startswith(_MIME_GAPPS_PREFIX):
            raise UnsupportedGoogleTypeError(mime)

        # Binary file — caller downloads and routes through ingest_file
        return BinaryDriveFile(file_id=file_id, name=name, mime_type=mime)

    # ------------------------------------------------------------------
    # Google Docs
    # ------------------------------------------------------------------

    def _parse_doc(self, file_id, name, author, meta) -> GoogleParsedDocument:
        doc = self._docs.documents().get(documentId=file_id).execute()
        content = self._extract_doc_text(doc)
        return GoogleParsedDocument(
            title=name,
            content=content,
            source_type="gdoc",
            file_id=file_id,
            author=author,
            pre_chunks=None,
            metadata=meta,
        )

    def _extract_doc_text(self, doc: dict) -> str:
        """Walk Docs API structural elements and extract plain text."""
        lines = []
        for element in doc.get("body", {}).get("content", []):
            para = element.get("paragraph")
            if not para:
                continue
            for elem in para.get("elements", []):
                text = elem.get("textRun", {}).get("content", "")
                if text:
                    lines.append(text)
        return "".join(lines).strip()

    # ------------------------------------------------------------------
    # Google Sheets
    # ------------------------------------------------------------------

    def _parse_sheet(self, file_id, name, author, meta) -> GoogleParsedDocument:
        spreadsheet = (
            self._sheets.spreadsheets()
            .get(spreadsheetId=file_id, includeGridData=True)
            .execute()
        )
        pre_chunks = []
        all_text_parts = []

        for tab_index, sheet in enumerate(spreadsheet.get("sheets", [])):
            tab_name = sheet["properties"]["title"]
            rows = []
            for grid in sheet.get("data", []):
                for row_data in grid.get("rowData", []):
                    cells = [
                        str(c.get("formattedValue", ""))
                        for c in row_data.get("values", [])
                    ]
                    rows.append(",".join(cells))
                    if len(rows) > MAX_SHEET_ROWS:
                        break
                if len(rows) > MAX_SHEET_ROWS:
                    break

            if not any(r.strip() for r in rows):
                continue  # skip empty tabs

            truncated = len(rows) > MAX_SHEET_ROWS
            if truncated:
                rows = rows[:MAX_SHEET_ROWS]
            csv_text = "\n".join(rows)
            if truncated:
                csv_text += "\n# ... truncated"

            chunk_content = f"[Tab: {tab_name}]\n{csv_text}"
            pre_chunks.append({"content": chunk_content, "page_number": tab_index})
            all_text_parts.append(chunk_content)

        full_content = "\n\n".join(all_text_parts)
        return GoogleParsedDocument(
            title=name,
            content=full_content,
            source_type="gsheet",
            file_id=file_id,
            author=author,
            pre_chunks=pre_chunks if pre_chunks else None,
            metadata=meta,
        )

    # ------------------------------------------------------------------
    # Google Slides
    # ------------------------------------------------------------------

    def _parse_slide(self, file_id, name, author, meta) -> GoogleParsedDocument:
        presentation = self._slides.presentations().get(presentationId=file_id).execute()
        pre_chunks = []
        all_text_parts = []

        for slide_index, slide in enumerate(presentation.get("slides", []), start=1):
            texts = self._extract_slide_text(slide)
            notes = self._extract_slide_notes(slide)
            parts = texts + notes
            chunk_content = "\n".join(parts) if parts else f"[Slide {slide_index}]"
            pre_chunks.append({"content": chunk_content, "page_number": slide_index})
            all_text_parts.append(chunk_content)

        full_content = "\n\n".join(all_text_parts)
        return GoogleParsedDocument(
            title=name,
            content=full_content,
            source_type="gslide",
            file_id=file_id,
            author=author,
            pre_chunks=pre_chunks if pre_chunks else None,
            metadata=meta,
        )

    def _extract_slide_text(self, slide: dict) -> list[str]:
        """Extract all text from slide page elements."""
        texts = []
        for element in slide.get("pageElements", []):
            shape = element.get("shape", {})
            text_content = shape.get("text", {})
            for item in text_content.get("textElements", []):
                run = item.get("textRun", {})
                content = run.get("content", "").strip()
                if content:
                    texts.append(content)
        return texts

    def _extract_slide_notes(self, slide: dict) -> list[str]:
        """Extract speaker notes from a slide."""
        notes = []
        notes_page = slide.get("slideProperties", {}).get("notesPage", {})
        for element in notes_page.get("pageElements", []):
            shape = element.get("shape", {})
            text_content = shape.get("text", {})
            for item in text_content.get("textElements", []):
                run = item.get("textRun", {})
                content = run.get("content", "").strip()
                if content:
                    notes.append(f"[Note] {content}")
        return notes
