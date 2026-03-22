"""Tests for Google Drive parser — URL parsing and data types."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from brain.ingestion.parsers.google_drive import (
    _extract_google_id,
    UnsupportedGoogleTypeError,
    GoogleAuth,
    GoogleDriveClient,
    GoogleParsedDocument,
    BinaryDriveFile,
)
from brain.ingestion.pipeline import ingest_google_item, ingest_google_folder


def _async(coro):
    return asyncio.run(coro)


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


def _make_client():
    """Return a GoogleDriveClient with mocked credentials."""
    with patch.object(GoogleAuth, "get_credentials", return_value=MagicMock()), \
         patch("googleapiclient.discovery.build", return_value=MagicMock()):
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


def test_parse_sheet_tabs_become_pre_chunks():
    """Each non-empty tab becomes a pre-chunk with correct page_number (0-based)."""
    client = _make_client()
    fake_sheets = MagicMock()
    fake_sheets.spreadsheets().get().execute.return_value = {
        "sheets": [
            {
                "properties": {"title": "Sheet1"},
                "data": [{"rowData": [
                    {"values": [{"formattedValue": "A"}, {"formattedValue": "B"}]},
                    {"values": [{"formattedValue": "C"}, {"formattedValue": "D"}]},
                ]}],
            },
            {
                "properties": {"title": "Empty"},
                "data": [{"rowData": []}],
            },
            {
                "properties": {"title": "Sheet2"},
                "data": [{"rowData": [
                    {"values": [{"formattedValue": "X"}]},
                ]}],
            },
        ]
    }
    fake_drive = MagicMock()
    fake_drive.files().get().execute.return_value = {
        "id": "SHEETID", "name": "My Sheet",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "modifiedTime": "2026-01-01T00:00:00Z", "owners": [],
    }
    with patch.object(client, "_drive", fake_drive), \
         patch.object(client, "_sheets", fake_sheets):
        result = client.fetch_item("SHEETID")

    assert isinstance(result, GoogleParsedDocument)
    assert result.source_type == "gsheet"
    # Empty tab skipped — only 2 non-empty tabs
    assert len(result.pre_chunks) == 2
    assert result.pre_chunks[0]["page_number"] == 0  # Sheet1
    assert result.pre_chunks[1]["page_number"] == 2  # Sheet2 (index 2)
    assert "[Tab: Sheet1]" in result.pre_chunks[0]["content"]
    assert "A,B" in result.pre_chunks[0]["content"]
    assert "[Tab: Sheet2]" in result.pre_chunks[1]["content"]


def test_parse_sheet_truncation():
    """Sheets with more than MAX_SHEET_ROWS rows get truncated suffix."""
    from brain.ingestion.parsers.google_drive import MAX_SHEET_ROWS
    client = _make_client()

    # Create MAX_SHEET_ROWS + 1 rows to trigger truncation
    row_data = [{"values": [{"formattedValue": f"row{i}"}]} for i in range(MAX_SHEET_ROWS + 1)]

    fake_sheets = MagicMock()
    fake_sheets.spreadsheets().get().execute.return_value = {
        "sheets": [
            {
                "properties": {"title": "Big"},
                "data": [{"rowData": row_data}],
            }
        ]
    }
    fake_drive = MagicMock()
    fake_drive.files().get().execute.return_value = {
        "id": "BIGID", "name": "Big Sheet",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "modifiedTime": "2026-01-01T00:00:00Z", "owners": [],
    }
    with patch.object(client, "_drive", fake_drive), \
         patch.object(client, "_sheets", fake_sheets):
        result = client.fetch_item("BIGID")

    assert "# ... truncated" in result.pre_chunks[0]["content"]
    # Should have exactly MAX_SHEET_ROWS rows, not MAX_SHEET_ROWS + 1
    lines = [l for l in result.pre_chunks[0]["content"].split("\n") if not l.startswith("[") and not l.startswith("#")]
    assert len(lines) == MAX_SHEET_ROWS


def test_parse_slide_pre_chunks_with_notes():
    """Each slide becomes a pre-chunk with 1-based page_number, including speaker notes."""
    client = _make_client()
    fake_slides = MagicMock()
    fake_slides.presentations().get().execute.return_value = {
        "slides": [
            {
                "pageElements": [
                    {"shape": {"text": {"textElements": [{"textRun": {"content": "Title Slide"}}]}}},
                ],
                "slideProperties": {
                    "notesPage": {
                        "pageElements": [
                            {"shape": {"text": {"textElements": [{"textRun": {"content": "Speaker note here"}}]}}},
                        ]
                    }
                },
            },
            {
                "pageElements": [],
                "slideProperties": {"notesPage": {"pageElements": []}},
            },
        ]
    }
    fake_drive = MagicMock()
    fake_drive.files().get().execute.return_value = {
        "id": "SLIDEID", "name": "My Slides",
        "mimeType": "application/vnd.google-apps.presentation",
        "modifiedTime": "2026-01-01T00:00:00Z", "owners": [],
    }
    with patch.object(client, "_drive", fake_drive), \
         patch.object(client, "_slides", fake_slides):
        result = client.fetch_item("SLIDEID")

    assert isinstance(result, GoogleParsedDocument)
    assert result.source_type == "gslide"
    assert len(result.pre_chunks) == 2
    # Slide 1: 1-based index = 1
    assert result.pre_chunks[0]["page_number"] == 1
    assert "Title Slide" in result.pre_chunks[0]["content"]
    assert "[Note] Speaker note here" in result.pre_chunks[0]["content"]
    # Slide 2: empty → falls back to "[Slide 2]"
    assert result.pre_chunks[1]["page_number"] == 2
    assert "[Slide 2]" in result.pre_chunks[1]["content"]


def test_fetch_folder_returns_flat_file_ids():
    """fetch_folder returns a flat list of non-folder file IDs, recursing into subfolders."""
    client = _make_client()

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


def test_ingest_google_item_binary():
    """ingest_google_item downloads and stores a binary Drive file with gdrive:// source_ref."""
    import tempfile, os
    db, embedder = _make_db_embedder()
    binary = BinaryDriveFile(file_id="PDFID", name="report.pdf", mime_type="application/pdf")
    mock_client = MagicMock()
    mock_client.fetch_item.return_value = binary
    mock_client.download_file.return_value = b"%PDF-1.4 fake content"

    # Write a real temp PDF that _parse_file_for_storage can "parse"
    # (pdf parser may fail on fake content — patch it instead)
    with patch("brain.ingestion.parsers.google_drive.GoogleDriveClient", return_value=mock_client), \
         patch("brain.ingestion.pipeline._parse_file_for_storage",
               return_value=("Report", "content", "pdf", None, None, {})):
        doc_id = _async(ingest_google_item(db, embedder, "gdrive://PDFID"))

    assert doc_id == "doc-uuid-1"
    call_kwargs = db.add_document.call_args.kwargs
    assert call_kwargs["source_ref"] == "gdrive://PDFID"
    assert call_kwargs["source_type"] == "pdf"
    mock_client.download_file.assert_called_once_with("PDFID")


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
