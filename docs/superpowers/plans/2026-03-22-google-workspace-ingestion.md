# Google Workspace Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Google Docs, Sheets, Slides, and Drive folder ingestion to the Brain MCP system with update and delete support.

**Architecture:** A new `google_drive.py` parser handles auth, API calls, and content extraction. Two pipeline functions (`ingest_google_item`, `ingest_google_folder`) integrate with the existing `_store()` path. Two MCP tools and a CLI auth command complete the surface area.

**Tech Stack:** `google-api-python-client`, `google-auth-oauthlib`, `google-auth` — all optional extras. No new runtime infrastructure.

**Spec:** `docs/superpowers/specs/2026-03-22-google-workspace-ingestion-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `brain/ingestion/parsers/google_drive.py` | Auth, client, data types, URL parsing, all Google API calls |
| Create | `tests/test_google_drive.py` | Unit tests for URL parsing, data types, parsing logic (mocked API) |
| Modify | `brain/ingestion/pipeline.py` | Add `ingest_google_item`, `ingest_google_folder` |
| Modify | `brain/server.py` | Add `add_google_drive_item`, `add_google_drive_folder`; extend `update_document` |
| Modify | `brain/cli.py` | Add `google_auth` command |
| Modify | `pyproject.toml` | Add `[google]` optional extras |

---

## Task 1: Add Google dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the optional extras and pytest dev dependency**

Edit `pyproject.toml`. After the existing `[project.optional-dependencies]` `macos` block, add:

```toml
google = [
    "google-auth>=2.0",
    "google-auth-oauthlib>=1.2",
    "google-api-python-client>=2.100",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```

Note: `pyproject.toml` already has one `[project.optional-dependencies]` block for `macos`. Merge both `google` and `dev` into that same block — TOML does not allow duplicate section headers. The final block should look like:

```toml
[project.optional-dependencies]
macos = [
    "mlx-embeddings>=0.0.5",
    "pyobjc-framework-naturallanguage>=12.1",
    "numbers-parser>=4.0",
]
google = [
    "google-auth>=2.0",
    "google-auth-oauthlib>=1.2",
    "google-api-python-client>=2.100",
]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```

- [ ] **Step 2: Sync dependencies**

```bash
uv sync --extra google --extra dev
```

Expected: resolves and installs `google-auth`, `google-auth-oauthlib`, `google-api-python-client`, `pytest`, and their deps.

- [ ] **Step 3: Verify imports**

```bash
uv run python -c "import googleapiclient; import google_auth_oauthlib; print('ok')"
uv run pytest --version
```

Expected: first prints `ok`, second prints pytest version.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat: add google and dev optional dependencies"
```

---

## Task 2: URL parsing and data types

**Files:**
- Create: `brain/ingestion/parsers/google_drive.py`
- Create: `tests/test_google_drive.py`

- [ ] **Step 1: Write failing tests for `_extract_google_id`**

Create `tests/test_google_drive.py`:

```python
"""Tests for Google Drive parser — URL parsing and data types."""
import pytest
from brain.ingestion.parsers.google_drive import _extract_google_id, UnsupportedGoogleTypeError


def test_extract_docs_url():
    url = "https://docs.google.com/document/d/ABC123def/edit"
    assert _extract_google_id(url) == "ABC123def"


def test_extract_sheets_url():
    url = "https://docs.google.com/spreadsheets/d/XYZ789/edit#gid=0"
    assert _extract_google_id(url) == "XYZ789"


def test_extract_slides_url():
    url = "https://docs.google.com/presentation/d/PRES456/edit"
    assert _extract_google_id(url) == "PRES456"


def test_extract_drive_file_url():
    url = "https://drive.google.com/file/d/FILE999/view"
    assert _extract_google_id(url) == "FILE999"


def test_extract_drive_folder_url():
    url = "https://drive.google.com/drive/folders/FOLD111"
    assert _extract_google_id(url) == "FOLD111"


def test_extract_drive_open_url():
    url = "https://drive.google.com/open?id=OPEN222"
    assert _extract_google_id(url) == "OPEN222"


def test_extract_gdrive_scheme():
    assert _extract_google_id("gdrive://FILEID123") == "FILEID123"


def test_extract_bare_id():
    # Real Google IDs contain alphanumeric chars, hyphens, and underscores
    assert _extract_google_id("BAREID456") == "BAREID456"
    assert _extract_google_id("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms") == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"


def test_extract_unrecognized_raises():
    with pytest.raises(ValueError, match="Could not extract"):
        _extract_google_id("https://example.com/not-google")


def test_unsupported_google_type_error():
    err = UnsupportedGoogleTypeError("application/vnd.google-apps.form")
    assert "application/vnd.google-apps.form" in str(err)
```

- [ ] **Step 2: Run tests to see them fail**

```bash
uv run pytest tests/test_google_drive.py -v
```

Expected: `ModuleNotFoundError` or `ImportError` — the module doesn't exist yet.

- [ ] **Step 3: Create `google_drive.py` with URL parsing and data types**

Create `brain/ingestion/parsers/google_drive.py`:

```python
"""
Google Drive / Workspace parser.

Handles authentication (OAuth2 + service account), all Google API calls,
and content extraction for Docs, Sheets, Slides, and binary Drive files.

Required extras: uv sync --extra google
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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

    # Bare ID: no slash, no ://
    if "/" not in url_or_id and "://" not in url_or_id:
        return url_or_id

    raise ValueError(f"Could not extract Google Drive file ID from: {url_or_id}")
```

- [ ] **Step 4: Run tests — URL parsing should pass**

```bash
uv run pytest tests/test_google_drive.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/ingestion/parsers/google_drive.py tests/test_google_drive.py
git commit -m "feat: google drive URL parsing and data types"
```

---

## Task 3: Authentication (`GoogleAuth`)

**Files:**
- Modify: `brain/ingestion/parsers/google_drive.py`
- Modify: `tests/test_google_drive.py`

- [ ] **Step 1: Write failing tests for `GoogleAuth`**

Add to `tests/test_google_drive.py`:

```python
import json
import tempfile
from unittest.mock import MagicMock, patch
from brain.ingestion.parsers.google_drive import GoogleAuth


def test_google_auth_no_credentials_raises(tmp_path, monkeypatch):
    """No env vars and no token file → RuntimeError with instructions."""
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "nonexistent.json"))
    auth = GoogleAuth()
    with pytest.raises(RuntimeError, match="brain google-auth"):
        auth.get_credentials()


def test_google_auth_service_account(tmp_path, monkeypatch):
    """Service account key path is set → builds service account credentials."""
    key_file = tmp_path / "key.json"
    key_file.write_text(json.dumps({
        "type": "service_account",
        "project_id": "test",
        "private_key_id": "key1",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA-----END RSA PRIVATE KEY-----\n",
        "client_email": "test@test.iam.gserviceaccount.com",
        "client_id": "123",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY", str(key_file))

    fake_creds = MagicMock()
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=fake_creds,
    ):
        auth = GoogleAuth()
        creds = auth.get_credentials()
    assert creds is fake_creds
```

- [ ] **Step 2: Run tests — see them fail**

```bash
uv run pytest tests/test_google_drive.py::test_google_auth_no_credentials_raises tests/test_google_drive.py::test_google_auth_service_account -v
```

Expected: `ImportError` or `AttributeError` — `GoogleAuth` not defined yet.

- [ ] **Step 3: Implement `GoogleAuth`**

Append to `brain/ingestion/parsers/google_drive.py` (after the data types section):

```python
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
```

- [ ] **Step 4: Run auth tests**

```bash
uv run pytest tests/test_google_drive.py::test_google_auth_no_credentials_raises tests/test_google_drive.py::test_google_auth_service_account -v
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/ingestion/parsers/google_drive.py tests/test_google_drive.py
git commit -m "feat: GoogleAuth with service account and OAuth2 support"
```

---

## Task 4: `GoogleDriveClient` — metadata and Docs parsing

**Files:**
- Modify: `brain/ingestion/parsers/google_drive.py`
- Modify: `tests/test_google_drive.py`

- [ ] **Step 1: Write failing tests for Docs parsing**

Add to `tests/test_google_drive.py`:

```python
from unittest.mock import MagicMock, patch
from brain.ingestion.parsers.google_drive import GoogleDriveClient, GoogleParsedDocument, BinaryDriveFile


def _make_client():
    """Return a GoogleDriveClient with mocked credentials."""
    with patch.object(GoogleAuth, "get_credentials", return_value=MagicMock()):
        return GoogleDriveClient()


def test_fetch_item_binary_returns_binary_drive_file():
    """A PDF stored in Drive returns a BinaryDriveFile."""
    client = _make_client()
    fake_drive = MagicMock()
    fake_drive.files().get().execute.return_value = {
        "id": "PDFID",
        "name": "report.pdf",
        "mimeType": "application/pdf",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "owners": [{"displayName": "Alice"}],
    }
    with patch.object(client, "_drive", fake_drive):
        result = client.fetch_item("PDFID")
    assert isinstance(result, BinaryDriveFile)
    assert result.file_id == "PDFID"
    assert result.name == "report.pdf"
    assert result.mime_type == "application/pdf"


def test_fetch_item_folder_raises():
    """Passing a folder ID to fetch_item raises ValueError."""
    client = _make_client()
    fake_drive = MagicMock()
    fake_drive.files().get().execute.return_value = {
        "id": "FOLDERID",
        "name": "My Folder",
        "mimeType": "application/vnd.google-apps.folder",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "owners": [],
    }
    with patch.object(client, "_drive", fake_drive):
        with pytest.raises(ValueError, match="fetch_folder"):
            client.fetch_item("FOLDERID")


def test_fetch_item_unsupported_google_type_raises():
    """A Google Form raises UnsupportedGoogleTypeError."""
    from brain.ingestion.parsers.google_drive import UnsupportedGoogleTypeError
    client = _make_client()
    fake_drive = MagicMock()
    fake_drive.files().get().execute.return_value = {
        "id": "FORMID",
        "name": "Survey",
        "mimeType": "application/vnd.google-apps.form",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "owners": [],
    }
    with patch.object(client, "_drive", fake_drive):
        with pytest.raises(UnsupportedGoogleTypeError):
            client.fetch_item("FORMID")


def test_fetch_item_google_doc():
    """A Google Doc returns a GoogleParsedDocument with source_type gdoc."""
    client = _make_client()
    fake_drive = MagicMock()
    fake_drive.files().get().execute.return_value = {
        "id": "DOCID",
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "owners": [{"displayName": "Bob"}],
    }
    fake_doc_content = {
        "body": {
            "content": [
                {"paragraph": {"elements": [{"textRun": {"content": "Hello world\n"}}]}},
                {"paragraph": {"elements": [{"textRun": {"content": "Second paragraph\n"}}]}},
            ]
        }
    }
    fake_docs = MagicMock()
    fake_docs.documents().get().execute.return_value = fake_doc_content

    with patch.object(client, "_drive", fake_drive), \
         patch.object(client, "_docs", fake_docs):
        result = client.fetch_item("DOCID")

    assert isinstance(result, GoogleParsedDocument)
    assert result.source_type == "gdoc"
    assert result.title == "My Doc"
    assert result.author == "Bob"
    assert result.file_id == "DOCID"
    assert result.pre_chunks is None
    assert "Hello world" in result.content
    assert "Second paragraph" in result.content
    assert result.metadata["modified_time"] == "2026-01-01T00:00:00Z"
```

- [ ] **Step 2: Run tests — see them fail**

```bash
uv run pytest tests/test_google_drive.py::test_fetch_item_binary_returns_binary_drive_file tests/test_google_drive.py::test_fetch_item_folder_raises tests/test_google_drive.py::test_fetch_item_unsupported_google_type_raises tests/test_google_drive.py::test_fetch_item_google_doc -v
```

Expected: `AttributeError` — `GoogleDriveClient` not defined.

- [ ] **Step 3: Implement `GoogleDriveClient` with `fetch_item` for all types**

Append to `brain/ingestion/parsers/google_drive.py`:

```python
# ---------------------------------------------------------------------------
# Constants
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
                    if len(rows) >= MAX_SHEET_ROWS:
                        break
                if len(rows) >= MAX_SHEET_ROWS:
                    break

            if not any(r.strip() for r in rows):
                continue  # skip empty tabs

            truncated = len(rows) >= MAX_SHEET_ROWS
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
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_google_drive.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/ingestion/parsers/google_drive.py tests/test_google_drive.py
git commit -m "feat: GoogleDriveClient with Docs, Sheets, Slides, and binary dispatch"
```

---

## Task 5: `fetch_folder` — recursive listing

**Files:**
- Modify: `brain/ingestion/parsers/google_drive.py`
- Modify: `tests/test_google_drive.py`

- [ ] **Step 1: Write failing tests for `fetch_folder`**

Add to `tests/test_google_drive.py`:

```python
def test_fetch_folder_returns_flat_file_ids():
    """fetch_folder returns a flat list of non-folder file IDs, recursing into subfolders."""
    client = _make_client()

    # Folder contains 2 files + 1 subfolder; subfolder contains 1 file
    def fake_list(q, fields, pageToken=None):
        mock = MagicMock()
        if "ROOTFOLDER" in q:
            mock.execute.return_value = {
                "files": [
                    {"id": "FILE1", "mimeType": "application/pdf", "name": "a.pdf"},
                    {"id": "FILE2", "mimeType": "application/vnd.google-apps.document", "name": "doc"},
                    {"id": "SUBFOLDER", "mimeType": "application/vnd.google-apps.folder", "name": "sub"},
                ],
                "nextPageToken": None,
            }
        elif "SUBFOLDER" in q:
            mock.execute.return_value = {
                "files": [
                    {"id": "FILE3", "mimeType": "application/pdf", "name": "b.pdf"},
                ],
                "nextPageToken": None,
            }
        else:
            mock.execute.return_value = {"files": [], "nextPageToken": None}
        return mock

    root_meta = MagicMock()
    root_meta.execute.return_value = {
        "id": "ROOTFOLDER",
        "mimeType": "application/vnd.google-apps.folder",
        "driveId": None,
    }

    fake_drive = MagicMock()
    fake_drive.files().get.return_value = root_meta
    fake_drive.files().list.side_effect = fake_list

    with patch.object(client, "_drive", fake_drive):
        result = client.fetch_folder("ROOTFOLDER")

    assert sorted(result) == ["FILE1", "FILE2", "FILE3"]


def test_fetch_folder_shared_drive_raises():
    """fetch_folder raises ValueError for Shared Drive folders."""
    client = _make_client()
    root_meta = MagicMock()
    root_meta.execute.return_value = {
        "id": "SHAREDFOLDER",
        "mimeType": "application/vnd.google-apps.folder",
        "driveId": "DRIVE123",
    }
    fake_drive = MagicMock()
    fake_drive.files().get.return_value = root_meta
    with patch.object(client, "_drive", fake_drive):
        with pytest.raises(ValueError, match="Shared Drive"):
            client.fetch_folder("SHAREDFOLDER")
```

- [ ] **Step 2: Run tests — see them fail**

```bash
uv run pytest tests/test_google_drive.py::test_fetch_folder_returns_flat_file_ids tests/test_google_drive.py::test_fetch_folder_shared_drive_raises -v
```

Expected: `AttributeError` — `fetch_folder` not defined.

- [ ] **Step 3: Implement `fetch_folder`**

Add method to `GoogleDriveClient` class in `google_drive.py`:

```python
    # ------------------------------------------------------------------
    # fetch_folder
    # ------------------------------------------------------------------

    def fetch_folder(self, folder_id: str) -> list[str]:
        """Recursively list all non-trashed file IDs under a folder.

        Does not include folder IDs themselves in the result.
        Raises ValueError if the folder belongs to a Shared Drive.
        """
        # Check for Shared Drive membership
        meta = (
            self._drive.files()
            .get(fileId=folder_id, fields="id,mimeType,driveId")
            .execute()
        )
        if meta.get("driveId"):
            raise ValueError(
                "Shared Drives are not supported. "
                "Only personal My Drive folders are supported."
            )

        return self._list_folder_recursive(folder_id)

    def _list_folder_recursive(self, folder_id: str) -> list[str]:
        """Walk folder tree, returning a flat list of file IDs (no folders)."""
        file_ids: list[str] = []
        page_token = None

        while True:
            kwargs = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,mimeType,name)",
            }
            if page_token:
                kwargs["pageToken"] = page_token

            response = self._drive.files().list(**kwargs).execute()

            for item in response.get("files", []):
                if item["mimeType"] == _MIME_GFOLDER:
                    # Recurse into subfolder
                    file_ids.extend(self._list_folder_recursive(item["id"]))
                else:
                    file_ids.append(item["id"])

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return file_ids
```

- [ ] **Step 4: Run all tests**

```bash
uv run pytest tests/test_google_drive.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/ingestion/parsers/google_drive.py tests/test_google_drive.py
git commit -m "feat: fetch_folder with recursive listing and Shared Drive guard"
```

---

## Task 6: Pipeline — `ingest_google_item` and `ingest_google_folder`

**Files:**
- Modify: `brain/ingestion/pipeline.py`
- Modify: `tests/test_google_drive.py`

- [ ] **Step 1: Write failing tests for pipeline functions**

Add to `tests/test_google_drive.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from brain.ingestion.pipeline import ingest_google_item, ingest_google_folder
from brain.ingestion.parsers.google_drive import (
    GoogleParsedDocument, BinaryDriveFile, UnsupportedGoogleTypeError,
)


def _async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_db_embedder():
    db = MagicMock()
    db.find_by_source_ref = AsyncMock(return_value=None)
    db.add_document = AsyncMock(return_value="doc-uuid-1")
    db.add_chunks = AsyncMock()
    db.add_tags = AsyncMock()
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 768)
    return db, embedder


def test_ingest_google_item_gdoc():
    """ingest_google_item stores a GoogleParsedDocument and returns a UUID."""
    db, embedder = _make_db_embedder()
    parsed = GoogleParsedDocument(
        title="Test Doc",
        content="Hello world",
        source_type="gdoc",
        file_id="DOCID",
        author="Alice",
        metadata={"modified_time": "2026-01-01T00:00:00Z"},
    )
    mock_client = MagicMock()
    mock_client.fetch_item.return_value = parsed

    with patch(
        # Patch at the source module — ingest_google_item uses a local import
        # so patching pipeline.GoogleDriveClient would be silently ignored
        "brain.ingestion.parsers.google_drive.GoogleDriveClient",
        return_value=mock_client,
    ):
        doc_id = _async(ingest_google_item(db, embedder, "gdrive://DOCID"))

    assert doc_id == "doc-uuid-1"
    db.add_document.assert_called_once()
    call_kwargs = db.add_document.call_args.kwargs
    assert call_kwargs["source_ref"] == "gdrive://DOCID"
    assert call_kwargs["source_type"] == "gdoc"


def test_ingest_google_folder_counts():
    """ingest_google_folder returns correct ingested/skipped/failed counts."""
    db, embedder = _make_db_embedder()

    mock_client = MagicMock()
    mock_client.fetch_folder.return_value = ["FILE1", "FILE2", "FILE3"]
    mock_client.fetch_item.side_effect = [
        GoogleParsedDocument(
            title="Doc1", content="a", source_type="gdoc", file_id="FILE1",
            metadata={},
        ),
        UnsupportedGoogleTypeError("application/vnd.google-apps.form"),
        GoogleParsedDocument(
            title="Doc3", content="c", source_type="gdoc", file_id="FILE3",
            metadata={},
        ),
    ]

    with patch("brain.ingestion.parsers.google_drive.GoogleDriveClient", return_value=mock_client):
        result = _async(ingest_google_folder(db, embedder, "gdrive://FOLDERID"))

    assert result["ingested"] == 2
    assert result["skipped"] == 1
    assert result["skip_reasons"]["unsupported_type"] == 1
    assert result["failed"] == 0
```

- [ ] **Step 2: Run tests — see them fail**

```bash
uv run pytest tests/test_google_drive.py::test_ingest_google_item_gdoc tests/test_google_drive.py::test_ingest_google_folder_counts -v
```

Expected: `ImportError` — functions not yet defined.

- [ ] **Step 3: Implement `ingest_google_item` and `ingest_google_folder`**

Add to `brain/ingestion/pipeline.py` (at the bottom, after `ingest_chat`):

```python
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
    assert isinstance(result, BinaryDriveFile)
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
    import io
    import tempfile
    from pathlib import Path as _Path

    from googleapiclient.http import MediaIoBaseDownload

    request = client._drive.files().get_media(fileId=binary.file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)

    suffix = _Path(binary.name).suffix.lower() or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(buf.read())
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
```

Also add the import at the top of `pipeline.py` after existing imports:

```python
from brain.db import Database
from brain.embedder import Embedder
```

(These are already imported — just confirm they exist. The `Database` type hint is used in the new functions. If using SQLite, the type is `SqliteDatabase` which is not imported here, but the existing code uses duck typing throughout so this is fine.)

- [ ] **Step 4: Run all tests**

```bash
uv run pytest tests/test_google_drive.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/ingestion/pipeline.py tests/test_google_drive.py
git commit -m "feat: ingest_google_item and ingest_google_folder pipeline functions"
```

---

## Task 7: MCP server tools and `update_document` extension

**Files:**
- Modify: `brain/server.py`

- [ ] **Step 1: Add imports and two new MCP tools**

In `brain/server.py`, add to the imports line:

```python
from brain.ingestion.pipeline import (
    ingest_chat, ingest_email, ingest_file, ingest_google_folder,
    ingest_google_item, ingest_mbox, ingest_text, ingest_url,
)
```

Then add two new tools after `add_mbox` (before `update_document`):

```python
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
```

- [ ] **Step 2: Extend `update_document` to handle Google types**

In `brain/server.py`, find the `update_document` function. After the `elif source_type == "chat":` block and before the final `else:` block, add:

```python
        elif source_type in ("gdoc", "gsheet", "gslide"):
            new_id = await ingest_google_item(db, embedder, source_ref, tags=tags, force=True)
            return f"Updated {source_type} — new document id: {new_id}"
```

- [ ] **Step 3: Verify the server starts without error**

```bash
uv run python -c "import brain.server; print('server imports ok')"
```

Expected: `server imports ok`

- [ ] **Step 4: Commit**

```bash
git add brain/server.py
git commit -m "feat: add_google_drive_item, add_google_drive_folder MCP tools, update_document extension"
```

---

## Task 8: CLI `brain google-auth` command

**Files:**
- Modify: `brain/cli.py`

- [ ] **Step 1: Add the `google_auth` command**

Add to `brain/cli.py` after the existing `ingest` command (before `if __name__ == "__main__":`):

```python
@app.command(name="google-auth")
def google_auth(
    secrets: Path = typer.Option(
        None,
        "--secrets",
        help="Path to client_secrets.json (overrides GOOGLE_CLIENT_SECRETS env var)",
    ),
    token_path: Path = typer.Option(
        None,
        "--token-path",
        help="Where to save the token (overrides GOOGLE_TOKEN_PATH env var)",
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-authenticate even if a valid token already exists"
    ),
) -> None:
    """Authenticate with Google (OAuth2) for Drive/Docs/Sheets/Slides access.

    Opens a browser to complete the OAuth2 consent flow, then saves a token
    for future use. Desktop environments only — use a service account for
    headless/SSH contexts.

    Requires: uv sync --extra google
    """
    import json
    import os

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
    except ImportError:
        typer.echo("Error: Google extras not installed. Run: uv sync --extra google")
        raise typer.Exit(1)

    from brain.ingestion.parsers.google_drive import SCOPES, _DEFAULT_SECRETS, _DEFAULT_TOKEN

    secrets_path = secrets or Path(os.environ.get("GOOGLE_CLIENT_SECRETS", str(_DEFAULT_SECRETS)))
    tok_path = token_path or Path(os.environ.get("GOOGLE_TOKEN_PATH", str(_DEFAULT_TOKEN)))

    # Check if already authenticated
    if not force and tok_path.exists():
        try:
            data = json.loads(tok_path.read_text())
            creds = Credentials.from_authorized_user_info(data, SCOPES)
            if creds.valid:
                typer.echo(f"Already authenticated. Use --force to re-authenticate.")
                typer.echo(f"Token: {tok_path}")
                return
        except Exception:
            pass  # Token invalid or corrupt — proceed to re-auth

    # Check client secrets exist
    if not secrets_path.exists():
        typer.echo(f"Error: client_secrets.json not found at {secrets_path}")
        typer.echo(
            "Download it from Google Cloud Console → APIs & Services → "
            "Credentials → OAuth 2.0 Client ID → Download JSON"
        )
        raise typer.Exit(1)

    typer.echo("Opening browser for Google OAuth2 consent…")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as e:
        typer.echo(f"Authentication failed: {e}")
        raise typer.Exit(1)

    tok_path.parent.mkdir(parents=True, exist_ok=True)
    tok_path.write_text(creds.to_json())
    typer.echo(f"Authenticated successfully. Token saved to {tok_path}")
```

- [ ] **Step 2: Verify the command is registered**

```bash
uv run brain --help
```

Expected: `google-auth` appears in the command list.

```bash
uv run brain google-auth --help
```

Expected: shows `--secrets`, `--token-path`, `--force` options.

- [ ] **Step 3: Commit**

```bash
git add brain/cli.py
git commit -m "feat: brain google-auth CLI command for OAuth2 setup"
```

---

## Task 9: Final wiring check and full test run

**Files:**
- (none new)

- [ ] **Step 1: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: all tests PASS with no errors.

- [ ] **Step 2: Verify server still imports cleanly**

```bash
uv run python -c "import brain.server; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Verify CLI still works**

```bash
uv run brain --help
uv run brain google-auth --help
```

Expected: both commands show correct help.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: Google Workspace ingestion complete (Docs, Sheets, Slides, Drive folders)"
```

---

## Summary of new surface area

| Tool / Command | What it does |
|----------------|-------------|
| `brain google-auth` | One-time OAuth2 setup (desktop) |
| MCP `add_google_drive_item` | Ingest a single Doc/Sheet/Slide/binary by URL or ID |
| MCP `add_google_drive_folder` | Recursively ingest a Drive folder |
| MCP `update_document` | Extended to re-fetch `gdoc`/`gsheet`/`gslide` from Drive |
| MCP `delete_document` | Works unchanged (by UUID) |
| MCP `search` / `hybrid_search` | Works unchanged — all Drive content is indexed and searchable |

## Environment Variables

| Variable | Default | Set when |
|----------|---------|---------|
| `GOOGLE_SERVICE_ACCOUNT_KEY` | (unset) | Headless/automation use |
| `GOOGLE_CLIENT_SECRETS` | `~/.config/brain/client_secrets.json` | OAuth2 setup |
| `GOOGLE_TOKEN_PATH` | `~/.config/brain/google_token.json` | OAuth2 token location |
