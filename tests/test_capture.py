"""Tests for source_archive.capture with mocked HTTP + Playwright."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from warcio.archiveiterator import ArchiveIterator

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


def _fake_requests_head(url, timeout=30, headers=None):
    """Minimal fake requests.head response object for HTML URLs."""

    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8", "Server": "nginx"}

    return FakeResp()


def _fake_requests_get(url, timeout=30, headers=None):
    """Minimal fake requests.get response object for HTML URLs."""

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


def _fake_render_and_capture(url, dest_dir, timeout):
    """Fake Playwright render + network interception returning WARC-ready data."""
    (dest_dir / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n fake png")
    return {
        "screenshot": dest_dir / "screenshot.png",
        "responses": [
            {
                "url": url,
                "status": 200,
                "headers": {
                    "Content-Type": "text/html; charset=utf-8",
                    "Server": "nginx",
                },
                "body": SAMPLE_HTML.encode("utf-8"),
            },
            {
                "url": f"{url}/style.css",
                "status": 200,
                "headers": {"Content-Type": "text/css"},
                "body": b"body { color: black; }",
            },
        ],
        "main_response_status": 200,
        "main_response_headers": {
            "content-type": "text/html; charset=utf-8",
            "server": "nginx",
        },
        "main_content_type": "text/html; charset=utf-8",
        "html": SAMPLE_HTML,
    }


@pytest.fixture
def temp_output(tmp_path: Path) -> Path:
    """Clean per-test archive directory."""
    return tmp_path / "archive"


def test_capture_url_writes_manifest_and_index(temp_output: Path) -> None:
    with (
        mock.patch.object(capture_mod.requests, "head", side_effect=_fake_requests_head),
        mock.patch.object(capture_mod.requests, "get", side_effect=_fake_requests_get),
        mock.patch.object(
            capture_mod, "_render_and_capture", side_effect=_fake_render_and_capture
        ),
    ):
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
    # Only the four expected HTML artifact categories should be present.
    assert set(manifest["artifacts"]) == {
        "warc",
        "screenshot",
        "article_text",
        "headers",
    }
    # Each artifact has sha256 + size.
    for art in manifest["artifacts"].values():
        assert len(art["sha256"]) == 64
        assert art["size"] > 0

    # WARC file exists and can be read back.
    warc_path = Path(result["artifacts_dir"]) / manifest["artifacts"]["warc"]["path"]
    assert warc_path.exists()
    records = list(ArchiveIterator(warc_path.open("rb")))
    assert len(records) == 3  # metadata + 2 intercepted responses
    types = [r.rec_type for r in records]
    assert "metadata" in types
    assert types.count("response") == 2

    # SQLite index should have the row.
    from source_archive.db import get_db_path, lookup_capture
    row = lookup_capture(get_db_path(temp_output), "https://example.com/article")
    assert row is not None
    assert row["url"] == "https://example.com/article"
    assert row["http_status"] == 200


def test_capture_falls_back_when_playwright_fails(temp_output: Path) -> None:
    with (
        mock.patch.object(capture_mod.requests, "head", side_effect=_fake_requests_head),
        mock.patch.object(capture_mod.requests, "get", side_effect=_fake_requests_get),
        mock.patch.object(capture_mod, "_render_and_capture", return_value=None),
    ):
        result = capture_url("https://example.com/no-render", output_dir=temp_output)

    assert result["ok"] is True
    manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
    # WARC, article text, and headers survive even without Playwright.
    assert set(manifest["artifacts"]) == {"warc", "article_text", "headers"}
    # Screenshot is absent in fallback mode.
    assert "screenshot" not in manifest["artifacts"]

    # Fallback WARC contains the single requests response.
    warc_path = (
        Path(result["artifacts_dir"]) / manifest["artifacts"]["warc"]["path"]
    )
    records = list(ArchiveIterator(warc_path.open("rb")))
    response_records = [r for r in records if r.rec_type == "response"]
    assert len(response_records) == 1


def test_capture_http_error_is_logged_not_crash(temp_output: Path) -> None:
    import requests as real_requests

    def boom(url, timeout=30, headers=None):
        raise real_requests.ConnectionError("connection refused")

    with (
        mock.patch.object(capture_mod.requests, "head", side_effect=boom),
        mock.patch.object(capture_mod.requests, "get", side_effect=boom),
    ):
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


def _fake_binary_head(content_type: str):
    """Return a fake requests.head for a binary response."""

    def _head(url, timeout=30, headers=None):
        class FakeResp:
            status_code = 200
            headers = {"Content-Type": content_type, "Server": "nginx"}

        return FakeResp()

    return _head


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
    fake_head = _fake_binary_head("image/png")
    fake_fetch = _fake_binary_fetch("image/png")
    render_spy = mock.Mock()
    extract_spy = mock.Mock()

    with (
        mock.patch.object(capture_mod.requests, "head", side_effect=fake_head),
        mock.patch.object(capture_mod.requests, "get", side_effect=fake_fetch),
        mock.patch.object(
            capture_mod, "_render_and_capture", side_effect=render_spy
        ),
        mock.patch.object(capture_mod, "_extract_article", side_effect=extract_spy),
    ):
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
    fake_head = _fake_binary_head("application/octet-stream")
    fake_fetch = _fake_binary_fetch("application/octet-stream")

    with (
        mock.patch.object(capture_mod.requests, "head", side_effect=fake_head),
        mock.patch.object(capture_mod.requests, "get", side_effect=fake_fetch),
        mock.patch.object(capture_mod, "_render_and_capture") as render_spy,
        mock.patch.object(capture_mod, "_extract_article") as extract_spy,
    ):
        result = capture_url("https://example.com/file", output_dir=temp_output)

    assert result["ok"] is True
    render_spy.assert_not_called()
    extract_spy.assert_not_called()

    manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
    assert manifest["content_category"] == "binary"
    assert manifest["artifacts"]["raw_binary"]["path"] == "raw.bin"


# ---------------------------------------------------------------------------
# Playwright network interception
# ---------------------------------------------------------------------------


def test_render_and_capture_intercepts_responses(tmp_path: Path) -> None:
    """``_render_and_capture`` registers a response handler and collects bodies."""
    from source_archive.capture import _render_and_capture

    dest_dir = tmp_path / "capture"

    subresource = mock.Mock()
    subresource.url = "https://example.com/style.css"
    subresource.status = 200
    subresource.headers = {"Content-Type": "text/css"}
    subresource.body.return_value = b"body { color: black; }"

    main_response = mock.Mock()
    main_response.url = "https://example.com/article"
    main_response.status = 200
    main_response.headers = {"Content-Type": "text/html; charset=utf-8"}
    main_response.body.return_value = SAMPLE_HTML.encode("utf-8")

    page = mock.Mock()
    registered_handlers: list = []

    def _register_handler(event, handler):
        if event == "response":
            registered_handlers.append(handler)
            # Simulate an intercepted subresource before goto finishes.
            handler(subresource)

    def _goto(url, **kwargs):
        # In real Playwright the main navigation response is also emitted.
        for handler in registered_handlers:
            handler(main_response)
        return main_response

    def _screenshot(path, full_page=True):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n fake png")

    page.on = _register_handler
    page.goto = mock.Mock(side_effect=_goto)
    page.content = mock.Mock(return_value=SAMPLE_HTML)
    page.screenshot = mock.Mock(side_effect=_screenshot)

    browser = mock.Mock()
    browser.new_page.return_value = page

    playwright_obj = mock.Mock()
    playwright_obj.chromium.launch.return_value = browser

    context_manager = mock.Mock()
    context_manager.__enter__ = mock.Mock(return_value=playwright_obj)
    context_manager.__exit__ = mock.Mock(return_value=False)

    with mock.patch(
        "playwright.sync_api.sync_playwright", return_value=context_manager
    ):
        result = _render_and_capture(
            "https://example.com/article", dest_dir, timeout=30
        )

    assert result is not None
    assert len(registered_handlers) == 1
    # The handler should have recorded the subresource and the main response.
    urls = {r["url"] for r in result["responses"]}
    assert "https://example.com/style.css" in urls
    assert "https://example.com/article" in urls
    assert result["main_response_status"] == 200
    assert result["main_content_type"] == "text/html; charset=utf-8"
    assert result["html"] == SAMPLE_HTML
    assert (dest_dir / "screenshot.png").exists()
