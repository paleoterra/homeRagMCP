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
