"""Tests for source_archive.capture with mocked HTTP + Playwright."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from source_archive import capture as capture_mod
from source_archive.capture import capture_url, classify_content_type


SAMPLE_HTML = """
<html><head><title>Test Article</title></head>
<body>
  <h1>Climate Report 2026</h1>
  <p>Global temperatures reached a new high this year according to scientists.</p>
  <p>The findings underscore the urgency of emissions reductions worldwide.</p>
</body></html>
"""


def _fake_requests_get(url, timeout=30, headers=None):
    """Minimal fake requests.get response object."""
    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8", "Server": "nginx"}

        @property
        def content(self):
            return SAMPLE_HTML.encode("utf-8")

        @property
        def text(self):
            return SAMPLE_HTML

    return FakeResp()


def _fake_render(url, dest_dir, timeout):
    """Write small placeholder artifacts so manifest entries are populated."""
    (dest_dir / "singlefile.html").write_text("<html><body>rendered</body></html>")
    (dest_dir / "output.pdf").write_bytes(b"%PDF-1.4 fake pdf bytes")
    (dest_dir / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n fake png")
    return {
        "singlefile_html": dest_dir / "singlefile.html",
        "pdf": dest_dir / "output.pdf",
        "screenshot": dest_dir / "screenshot.png",
    }


@pytest.fixture
def temp_output(tmp_path: Path) -> Path:
    """Clean per-test archive directory."""
    return tmp_path / "archive"


def test_capture_url_writes_manifest_and_index(temp_output: Path) -> None:
    with mock.patch.object(capture_mod.requests, "get", side_effect=_fake_requests_get), \
         mock.patch.object(capture_mod, "_render_with_playwright", side_effect=_fake_render):
        result = capture_url("https://example.com/article", output_dir=temp_output)

    assert result["ok"] is True
    manifest_path = Path(result["manifest_path"])
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert manifest["url"] == "https://example.com/article"
    assert manifest["http_status"] == 200
    assert manifest["content_type"] == "text/html; charset=utf-8"
    assert manifest["content_category"] == "html"
    assert manifest["wayback_url"] is None
    # All five artifact categories should be present.
    assert set(manifest["artifacts"]) >= {
        "raw_html", "singlefile_html", "pdf", "screenshot", "article_text"
    }
    # Each artifact has sha256 + size.
    for art in manifest["artifacts"].values():
        assert len(art["sha256"]) == 64
        assert art["size"] > 0

    # SQLite index should have the row.
    from source_archive.db import get_db_path, lookup_capture
    row = lookup_capture(get_db_path(temp_output), "https://example.com/article")
    assert row is not None
    assert row["url"] == "https://example.com/article"
    assert row["http_status"] == 200


def test_capture_falls_back_when_playwright_fails(temp_output: Path) -> None:
    with mock.patch.object(capture_mod.requests, "get", side_effect=_fake_requests_get), \
         mock.patch.object(capture_mod, "_render_with_playwright", return_value={}):
        result = capture_url("https://example.com/no-render", output_dir=temp_output)

    assert result["ok"] is True
    manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
    # raw_html and article_text survive even without Playwright.
    assert "raw_html" in manifest["artifacts"]
    assert "article_text" in manifest["artifacts"]
    # Rendered artifacts absent.
    assert "pdf" not in manifest["artifacts"]
    assert "screenshot" not in manifest["artifacts"]


def test_capture_http_error_is_logged_not_crash(temp_output: Path) -> None:
    import requests as real_requests

    def boom(url, timeout=30, headers=None):
        raise real_requests.ConnectionError("connection refused")

    with mock.patch.object(capture_mod.requests, "get", side_effect=boom):
        result = capture_url("https://example.com/down", output_dir=temp_output)

    assert result["ok"] is False
    assert "fetch failed" in result["error"]


# ---------------------------------------------------------------------------
# Content-type classification helpers
# ---------------------------------------------------------------------------


def test_classify_content_type_html_variants() -> None:
    assert classify_content_type("text/html") == "html"
    assert classify_content_type("text/html; charset=utf-8") == "html"
    assert classify_content_type("application/xhtml+xml") == "html"
    assert classify_content_type("text/plain") == "html"


def test_classify_content_type_binary_variants() -> None:
    assert classify_content_type("application/pdf") == "binary"
    assert classify_content_type("image/png") == "binary"
    assert classify_content_type("image/png; charset=utf-8") == "binary"
    assert classify_content_type("application/zip") == "binary"
    assert classify_content_type("video/mp4") == "binary"
    assert classify_content_type("audio/mpeg") == "binary"
    assert classify_content_type("application/octet-stream") == "binary"
    assert classify_content_type("unknown/thing") == "binary"
    assert classify_content_type(None) == "binary"


# ---------------------------------------------------------------------------
# Binary capture path
# ---------------------------------------------------------------------------


def _fake_binary_fetch(content_type: str):
    """Return a fake requests.get for a binary response."""
    def _fetch(url, timeout=30, headers=None):
        class FakeResp:
            status_code = 200
            headers = {"Content-Type": content_type, "Server": "nginx"}
            content = b"\x89PNG\r\n\x1a\n fake binary payload"

        return FakeResp()

    return _fetch


def test_binary_capture_skips_render_and_extraction(temp_output: Path) -> None:
    fake_fetch = _fake_binary_fetch("image/png")
    render_spy = mock.Mock()
    extract_spy = mock.Mock()

    with mock.patch.object(capture_mod.requests, "get", side_effect=fake_fetch), \
         mock.patch.object(capture_mod, "_render_with_playwright", side_effect=render_spy), \
         mock.patch.object(capture_mod, "_extract_article", side_effect=extract_spy):
        result = capture_url("https://example.com/graph.png", output_dir=temp_output)

    assert result["ok"] is True
    render_spy.assert_not_called()
    extract_spy.assert_not_called()

    manifest_path = Path(result["manifest_path"])
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert manifest["content_type"] == "image/png"
    assert manifest["content_category"] == "binary"
    assert set(manifest["artifacts"]) == {"raw_binary"}

    raw_artifact = manifest["artifacts"]["raw_binary"]
    assert raw_artifact["path"] == "raw.png"
    raw_path = Path(result["artifacts_dir"]) / raw_artifact["path"]
    assert raw_path.exists()
    assert raw_path.read_bytes() == b"\x89PNG\r\n\x1a\n fake binary payload"


def test_binary_capture_unknown_type_uses_raw_bin(temp_output: Path) -> None:
    fake_fetch = _fake_binary_fetch("application/octet-stream")

    with mock.patch.object(capture_mod.requests, "get", side_effect=fake_fetch), \
         mock.patch.object(capture_mod, "_render_with_playwright") as render_spy, \
         mock.patch.object(capture_mod, "_extract_article") as extract_spy:
        result = capture_url("https://example.com/file", output_dir=temp_output)

    assert result["ok"] is True
    render_spy.assert_not_called()
    extract_spy.assert_not_called()

    manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
    assert manifest["content_category"] == "binary"
    assert manifest["artifacts"]["raw_binary"]["path"] == "raw.bin"
