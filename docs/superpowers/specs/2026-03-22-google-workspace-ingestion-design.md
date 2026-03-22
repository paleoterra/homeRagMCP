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

### OAuth2 (personal use)

- New CLI command: `brain google-auth`
- Opens a browser, completes OAuth2 consent flow
- Saves token to `~/.config/brain/google_token.json`
- Token auto-refreshes silently on each use via `google-auth-oauthlib`
- Requires a Google Cloud project with Drive API enabled
- Client secrets downloaded once from Google Cloud Console, path set via `GOOGLE_CLIENT_SECRETS` env var (defaults to `~/.config/brain/client_secrets.json`)

### Service Account (headless/automation)

- Set `GOOGLE_SERVICE_ACCOUNT_KEY=/path/to/key.json`
- Drive folders/files must be shared with the service account email
- No interactive flow needed

### Resolution Order

1. `GOOGLE_SERVICE_ACCOUNT_KEY` env var → service account credentials
2. `~/.config/brain/google_token.json` exists → OAuth2 token
3. Neither → raise `RuntimeError` with message pointing to `brain google-auth`

A `GoogleAuth` class in `brain/ingestion/parsers/google_drive.py` wraps both paths and exposes a single `get_credentials()` method used internally by the Drive client.

---

## Google Drive Client & Parsing

A `GoogleDriveClient` class in `brain/ingestion/parsers/google_drive.py`.

### `fetch_item(file_id) → ParsedDocument | None`

Calls Drive API `files.get` for name, MIME type, `modifiedTime`, and owner, then dispatches:

| MIME type | Action |
|-----------|--------|
| `application/vnd.google-apps.document` | Docs API export as plain text → single prose document |
| `application/vnd.google-apps.spreadsheet` | Sheets API → each tab as CSV text, stored as pre-chunks with tab name as context header |
| `application/vnd.google-apps.presentation` | Slides API → each slide's text + speaker notes combined into a pre-chunk |
| `application/vnd.google-apps.folder` | Raises `ValueError` — use `fetch_folder` instead |
| Everything else (PDF, DOCX, binary) | Downloads to a temp file, returns `None` to signal delegation to existing `ingest_file` |

`modifiedTime` from Drive is stored in document `metadata` for future staleness checks.

### `fetch_folder(folder_id) → list[str]`

Recursively lists all non-trashed file IDs under a folder (unlimited depth). Returns a flat list of file IDs for the pipeline to iterate.

### `source_ref` Format

All Google Drive items use `gdrive://<file_id>` as `source_ref`. This is stable across renames, moves, and re-ingests.

---

## Pipeline

Two new functions in `brain/ingestion/pipeline.py`:

### `ingest_google_item(db, embedder, file_id_or_url, tags=None, force=False) -> str`

- Extracts file ID from a Google Drive/Docs/Sheets/Slides URL or bare file ID
- Calls `GoogleDriveClient.fetch_item(file_id)`
- If result is `None` (non-Google binary file): downloads to temp path, delegates to `ingest_file`
- Otherwise: calls `_store()` with:
  - `source_type` = `gdoc` / `gsheet` / `gslide`
  - `source_ref` = `gdrive://<file_id>`
  - `pre_chunks` for Sheets (per-tab) and Slides (per-slide)
- Returns document UUID

### `ingest_google_folder(db, embedder, folder_id_or_url, tags=None, force=False) -> dict`

- Extracts folder ID from URL or bare ID
- Calls `GoogleDriveClient.fetch_folder(folder_id)` to get all file IDs
- Iterates, calling `ingest_google_item()` for each
- Returns `{ingested, skipped, failed}` summary — same shape as `ingest_mbox`

---

## MCP Tools

Two new tools added to `brain/server.py`:

### `add_google_doc(url_or_id, tags=[], force=False) -> str`

Ingests a single Google Drive item (Doc, Sheet, Slide, or any other Drive file). Accepts the full Google Drive/Docs/Sheets/Slides URL or a bare file ID.

### `add_google_drive_folder(url_or_id, tags=[], force=False) -> str`

Recursively ingests all files in a Google Drive folder. Returns an ingestion summary.

### `update_document` Extension

Add `gdoc`, `gsheet`, `gslide` to the routing in the existing `update_document` tool — calls `ingest_google_item(..., force=True)` using the stored `source_ref`. Delete already works unchanged (ID-based).

---

## Dependencies

New optional dependency group in `pyproject.toml`:

```toml
[project.optional-dependencies]
google = [
    "google-auth-oauthlib>=1.2",
    "google-api-python-client>=2.100",
]
```

Install with: `uv add --optional google 'brain[google]'`

---

## New Files

| File | Purpose |
|------|---------|
| `brain/ingestion/parsers/google_drive.py` | `GoogleAuth`, `GoogleDriveClient`, `fetch_item`, `fetch_folder` |

## Modified Files

| File | Change |
|------|--------|
| `brain/ingestion/pipeline.py` | Add `ingest_google_item`, `ingest_google_folder` |
| `brain/server.py` | Add `add_google_doc`, `add_google_drive_folder` tools; extend `update_document` routing |
| `brain/cli.py` | Add `brain google-auth` command |
| `pyproject.toml` | Add `[google]` optional dependencies |

---

## Error Handling

- No credentials configured → `RuntimeError` with `brain google-auth` instructions
- OAuth2 token expired and refresh fails → re-raise with message to re-run `brain google-auth`
- File not accessible (permissions) → caught per-item in folder ingestion, counted as `failed`
- Non-exportable MIME type → log warning, count as `skipped`
- Folder ingestion: per-item errors are caught and counted, never abort the whole batch

---

## Out of Scope

- Background/automatic sync (manual `update_document` or `add_google_drive_folder force=True` only)
- Google Drive watch/push notifications
- Google Workspace Shared Drives (personal My Drive only for now)
- Gmail ingestion (separate concern, `add_mbox` already covers that path)
