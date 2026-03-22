# Google Workspace Ingestion — Design Spec

**Date:** 2026-03-22
**Status:** Approved
**Scope:** Add Google Docs, Sheets, Slides, and Google Drive folder ingestion to the Brain MCP system, with update and delete support.

---

## Overview

Extend the Brain ingestion pipeline with a unified Google Drive client that can ingest Google Docs, Sheets, Slides, and any other Drive-hosted files (PDFs, Word docs, etc.) into the vector database. Content is searchable, updatable, and deletable via the existing MCP tools.

---

## Authentication

Two credential modes, auto-detected by presence of env vars / credential files.

### OAuth2 (personal use — desktop only)

- New CLI command: `brain google-auth` — for local/desktop use only; cannot open a browser in headless/SSH contexts. Use service account for headless environments.
- Saves token to path configured by `GOOGLE_TOKEN_PATH` env var, defaulting to `~/.config/brain/google_token.json`
- Token auto-refreshes silently on each use via `google-auth-oauthlib`
- Requires a Google Cloud project with Drive, Docs, Sheets, and Slides APIs enabled
- Client secrets downloaded once from Google Cloud Console; path set via `GOOGLE_CLIENT_SECRETS` env var, defaulting to `~/.config/brain/client_secrets.json`
- **OAuth2 scopes requested:**
  - `https://www.googleapis.com/auth/drive.readonly`
  - `https://www.googleapis.com/auth/spreadsheets.readonly`
  - `https://www.googleapis.com/auth/presentations.readonly`

### Service Account (headless/automation)

- Set `GOOGLE_SERVICE_ACCOUNT_KEY=/path/to/key.json`
- Drive folders/files must be shared with the service account email
- No interactive flow needed; works in CI/SSH/headless contexts

### Resolution Order

1. `GOOGLE_SERVICE_ACCOUNT_KEY` env var set → service account credentials
2. Token file at `GOOGLE_TOKEN_PATH` exists → OAuth2 token
3. Neither → raise `RuntimeError`:
   ```
   Google credentials not configured.
   Run: brain google-auth   (desktop)
   Or set GOOGLE_SERVICE_ACCOUNT_KEY=/path/to/key.json   (headless)
   ```

A `GoogleAuth` class in `brain/ingestion/parsers/google_drive.py` wraps both paths and exposes a single `get_credentials()` method.

### Environment Variables (complete list)

| Variable | Default | Purpose |
|----------|---------|---------|
| `GOOGLE_CLIENT_SECRETS` | `~/.config/brain/client_secrets.json` | OAuth2 client secrets file |
| `GOOGLE_TOKEN_PATH` | `~/.config/brain/google_token.json` | OAuth2 token storage |
| `GOOGLE_SERVICE_ACCOUNT_KEY` | (unset) | Path to service account JSON key |

---

## Data Types

### `GoogleParsedDocument`

Returned by `GoogleDriveClient.fetch_item()` for Google Workspace types (Docs, Sheets, Slides):

```python
@dataclass
class GoogleParsedDocument:
    title: str
    content: str                          # full concatenated text
    source_type: str                      # "gdoc", "gsheet", "gslide"
    file_id: str                          # raw Drive file ID
    author: str | None = None             # owner display name
    pre_chunks: list[dict] | None = None  # list of {content: str, page_number: int | None}
    metadata: dict | None = None          # includes modified_time, mime_type, file_id
```

`pre_chunks` maps directly to `_store()`: each dict has `content` (str) and `page_number` (int or None). For Sheets, `page_number` is the tab index (0-based). For Slides, `page_number` is the slide index (1-based). For Docs, `pre_chunks=None` (standard chunker used).

### `BinaryDriveFile`

Returned by `fetch_item()` for non-Google binary files (PDF, DOCX, etc.):

```python
@dataclass
class BinaryDriveFile:
    file_id: str
    name: str        # original filename including extension
    mime_type: str   # e.g. "application/pdf"
```

The caller is responsible for downloading and routing through the existing `ingest_file` pipeline.

### `FetchItemResult`

`fetch_item()` returns a discriminated union:

```python
FetchItemResult = GoogleParsedDocument | BinaryDriveFile
```

- `GoogleParsedDocument` → Google Workspace type, ready to store
- `BinaryDriveFile` → binary file, needs download + `ingest_file` routing
- For **unsupported Google MIME types** (e.g. Google Forms, Maps): raises `UnsupportedGoogleTypeError(mime_type)` — a custom exception defined in `google_drive.py`

---

## Google Drive Client & Parsing

A `GoogleDriveClient` class in `brain/ingestion/parsers/google_drive.py`.

### `fetch_item(file_id) → FetchItemResult`

Calls Drive API `files.get?fields=id,name,mimeType,modifiedTime,owners` then dispatches:

| MIME type | Returns |
|-----------|---------|
| `application/vnd.google-apps.document` | `GoogleParsedDocument(source_type="gdoc", pre_chunks=None)` |
| `application/vnd.google-apps.spreadsheet` | `GoogleParsedDocument(source_type="gsheet", pre_chunks=[per-tab])` |
| `application/vnd.google-apps.presentation` | `GoogleParsedDocument(source_type="gslide", pre_chunks=[per-slide])` |
| `application/vnd.google-apps.folder` | Raises `ValueError("Use fetch_folder() for folders")` |
| `application/vnd.google-apps.*` (other) | Raises `UnsupportedGoogleTypeError(mime_type)` |
| Everything else (PDF, DOCX, binary) | `BinaryDriveFile(file_id, name, mime_type)` |

**Google Docs parsing:** Calls Docs API `documents.get`, walks structural elements to extract text.

**Google Sheets parsing:** Calls Sheets API `spreadsheets.get(includeGridData=True)`. Each tab becomes one pre-chunk: `f"[Tab: {tab_name}]\n{csv_rows}"`. Rows are capped at `MAX_SHEET_ROWS = 1000` per tab (rows beyond this are truncated with a `# ... truncated` suffix). Empty tabs are skipped.

**Google Slides parsing:** Calls Slides API `presentations.get`. Each slide becomes one pre-chunk: slide title + bullet text + text boxes + speaker notes, concatenated with newlines. Empty slides are included (with at least the slide index as context).

`metadata` includes: `{"modified_time": "<ISO8601>", "mime_type": "<mime>", "file_id": "<id>"}`.

### `fetch_folder(folder_id) → list[str]`

Recursively lists all non-trashed file IDs using Drive API `files.list` with `q="'<id>' in parents and trashed=false"`. Returns a flat list of file IDs only (no folder IDs). Raises `ValueError("Shared Drives are not supported.")` immediately if the root folder belongs to a Shared Drive (detected via `driveId` field on the folder metadata).

### `source_ref` Format

All Google Drive items use `gdrive://<file_id>` as `source_ref`. Stable across renames, moves, and re-ingests.

### URL Parsing

A `_extract_google_id(url_or_id: str) -> str` helper handles all URL forms:

| URL pattern | Extracted ID |
|-------------|-------------|
| `docs.google.com/document/d/<id>/...` | `<id>` |
| `docs.google.com/spreadsheets/d/<id>/...` | `<id>` |
| `docs.google.com/presentation/d/<id>/...` | `<id>` |
| `drive.google.com/file/d/<id>/...` | `<id>` |
| `drive.google.com/drive/folders/<id>` | `<id>` |
| `drive.google.com/open?id=<id>` | `<id>` |
| `gdrive://<id>` | `<id>` (used by `update_document`) |
| Bare alphanumeric string (no `/`, no `://`) | used as-is |

Unrecognized format raises `ValueError(f"Could not extract Google Drive file ID from: {url_or_id}")`.

---

## Pipeline

Two new functions in `brain/ingestion/pipeline.py`:

### `ingest_google_item(db, embedder, file_id_or_url, tags=None, force=False) -> str`

1. Extract file ID: `file_id = _extract_google_id(file_id_or_url)`
2. Call `GoogleDriveClient.fetch_item(file_id)` → `result`
3. Dispatch on result type:
   - **`GoogleParsedDocument`**: call `_store()` with `source_type=result.source_type`, `source_ref=f"gdrive://{file_id}"`, `pre_chunks=result.pre_chunks`, `author`, `metadata`, `tags`, `force`. Return document UUID.
   - **`BinaryDriveFile`**: download file content via Drive API `files.get(fileId, alt="media")` to a `tempfile.NamedTemporaryFile` with the correct extension from `result.name`. Parse using the appropriate parser from `brain/ingestion/parsers/` (same dispatch logic as `ingest_file`, by file extension). Then call `_store()` directly with `source_ref=f"gdrive://{file_id}"` and `source_type` derived from extension. Return document UUID.
   - **`UnsupportedGoogleTypeError`**: re-raise (caller handles counting)

### `ingest_google_folder(db, embedder, folder_id_or_url, tags=None, force=False) -> dict`

1. Extract folder ID: `folder_id = _extract_google_id(folder_id_or_url)`
2. Call `GoogleDriveClient.fetch_folder(folder_id)` — if it raises `ValueError` (Shared Drive), propagate immediately
3. Iterate file IDs, calling `ingest_google_item()` for each:

| Exception | Action |
|-----------|--------|
| `ValueError` with "Already indexed" (duplicate) | `skipped += 1`, `skip_reasons["duplicate"] += 1` |
| `UnsupportedGoogleTypeError` | `skipped += 1`, `skip_reasons["unsupported_type"] += 1` |
| Any other exception | `failed += 1`, log warning with file ID and error |
| Success | `ingested += 1` |

4. Return `{"ingested": int, "skipped": int, "failed": int, "skip_reasons": {"duplicate": int, "unsupported_type": int}}`

---

## MCP Tools

New tools added to `brain/server.py`:

### `add_google_drive_item(url_or_id, tags=[], force=False) -> str`

Ingests a single Google Drive item (Doc, Sheet, Slide, or binary file). Accepts full Google URLs or a bare file ID. Returns `"Ingested <title> as document <id>"` on success.

### `add_google_drive_folder(url_or_id, tags=[], force=False) -> str`

Recursively ingests all files in a Google Drive folder. Returns a human-readable summary string:
`"Ingested 12 items, skipped 3 (2 duplicate, 1 unsupported_type), failed 0."`

### `update_document` Extension

Add `gdoc`, `gsheet`, `gslide` to the existing routing in `update_document`. The stored `source_ref` is `gdrive://<file_id>`, which `_extract_google_id()` already handles:

```python
elif source_type in ("gdoc", "gsheet", "gslide"):
    new_id = await ingest_google_item(db, embedder, source_ref, tags=tags, force=True)
    return f"Updated {source_type} — new document id: {new_id}"
```

`delete_document` requires no changes (ID-based, already works).

---

## CLI: `brain google-auth`

New Typer command in `brain/cli.py`:

```
brain google-auth [--secrets PATH] [--token-path PATH] [--force]
```

- `--secrets`: path to `client_secrets.json` (overrides `GOOGLE_CLIENT_SECRETS` env var)
- `--token-path`: where to save the token (overrides `GOOGLE_TOKEN_PATH` env var)
- `--force`: re-authenticate even if a valid token already exists

**Behavior:**
1. If a valid, non-expired token already exists at `token-path` and `--force` is not set: print `"Already authenticated. Use --force to re-authenticate."` and exit 0.
2. If `client_secrets.json` is not found: print `"client_secrets.json not found at <path>.\nDownload it from Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID → Download JSON."` and exit 1.
3. Run `google-auth-oauthlib` local server flow (opens browser, listens on localhost for OAuth callback). This requires a desktop environment — headless environments should use a service account instead.
4. On success: save token, print `"Authenticated successfully. Token saved to <path>."` and exit 0.
5. On failure: print error, exit 1.

---

## Dependencies

New optional dependency group in `pyproject.toml`:

```toml
[project.optional-dependencies]
google = [
    "google-auth>=2.0",
    "google-auth-oauthlib>=1.2",
    "google-api-python-client>=2.100",
]
```

Install with: `uv sync --extra google`

---

## New Files

| File | Purpose |
|------|---------|
| `brain/ingestion/parsers/google_drive.py` | `GoogleAuth`, `GoogleDriveClient`, `GoogleParsedDocument`, `BinaryDriveFile`, `UnsupportedGoogleTypeError`, `_extract_google_id` |

## Modified Files

| File | Change |
|------|--------|
| `brain/ingestion/pipeline.py` | Add `ingest_google_item`, `ingest_google_folder` |
| `brain/server.py` | Add `add_google_drive_item`, `add_google_drive_folder`; extend `update_document` routing |
| `brain/cli.py` | Add `brain google-auth` command |
| `pyproject.toml` | Add `[google]` optional dependencies |

---

## Error Handling

| Condition | Behavior |
|-----------|---------|
| No credentials configured | `RuntimeError` with auth instructions |
| OAuth2 token expired, refresh fails | Re-raise with "Re-run `brain google-auth`" message |
| `client_secrets.json` missing | CLI prints helpful message and exits 1 |
| File not accessible (permissions) | Per-item in folder ingestion: `failed += 1` |
| Unsupported Google MIME type | Per-item: `skipped += 1`, `skip_reasons["unsupported_type"] += 1` |
| Shared Drive folder | `ValueError` propagates immediately from `fetch_folder` |
| Unrecognized URL format | `ValueError` with the offending string |
| Large Sheets tab (>1000 rows) | Truncated with `# ... truncated` suffix, no error |
| Folder ingestion per-item errors | Always caught and counted — batch never aborts |

---

## Out of Scope

- Background/automatic sync (manual `update_document` or `add_google_drive_folder force=True` only)
- Google Drive watch/push notifications
- Google Workspace Shared Drives (personal My Drive only)
- Gmail ingestion (`add_mbox` already covers that path)
- Write access to Google Workspace (read-only scopes only)
