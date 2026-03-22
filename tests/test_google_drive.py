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
