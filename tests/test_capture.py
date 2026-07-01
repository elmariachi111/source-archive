"""Tests for source_archive.capture with mocked HTTP + Playwright."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from source_archive import capture as capture_mod
from source_archive.capture import capture_url


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