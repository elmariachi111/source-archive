"""Tests for source_archive.article batch source extraction."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from source_archive import article as article_mod
from source_archive.article import capture_article_sources


SAMPLE_ARTICLE_URL = "https://example.com/article"

SAMPLE_HTML = """
<html>
<head><title>Article</title></head>
<body>
  <h1>Article</h1>
  <p>
    <a href="https://example.com/report.pdf">Local PDF</a>
    <a href="https://www.example.com/about">Local www variant</a>
    <a href="https://other.com/news">External news</a>
    <a href="https://another.org/paper">Another source</a>
    <a href="https://thegoodclimate.substack.com/p/other">Substack same platform</a>
    <a href="https://writer.substack.com/p/post">Other Substack</a>
    <a href="https://www.facebook.com/sharer/sharer.php">Facebook share</a>
    <a href="https://x.com/intent/tweet">X share</a>
    <a href="mailto:editor@example.com">Email</a>
    <a href="tel:+123">Phone</a>
    <a href="#section">Anchor</a>
    <a href="/local/path">Relative</a>
    <a href="https://other.com/news">Duplicate external</a>
    <a href="https://other.com/news#fragment">Duplicate with fragment</a>
    <a href="https://example.com/article">Self link</a>
    <a href="//cdn.example.com/file.css">Protocol-relative (same domain)</a>
    <a href="https://external.io/dataset">Dataset</a>
  </p>
</body>
</html>
"""


class _FakeArticleResponse:
    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    text = SAMPLE_HTML

    def raise_for_status(self) -> None:
        pass


@pytest.fixture
def temp_output(tmp_path: Path) -> Path:
    """Clean per-test archive directory."""
    return tmp_path / "archive"


def test_extract_filters_remote_url(temp_output: Path) -> None:
    with mock.patch.object(article_mod.requests, "get", return_value=_FakeArticleResponse()):
        result = capture_article_sources(
            SAMPLE_ARTICLE_URL,
            output_dir=temp_output,
            dry_run=True,
        )

    assert result["article_url"] == SAMPLE_ARTICLE_URL
    assert result["article_domain"] == "example.com"
    assert result["total_links"] == 17
    assert result["excluded"]["internal"] == 7  # local + substack + self + relative + anchor + proto-relative
    assert result["excluded"]["social"] == 4    # facebook, x, mailto, tel
    assert result["excluded"]["duplicates"] == 2  # duplicate external + duplicate with fragment

    expected_remote = [
        "https://other.com/news",
        "https://another.org/paper",
        "https://cdn.example.com/file.css",
        "https://external.io/dataset",
    ]
    assert [s["url"] for s in result["sources"]] == expected_remote


def test_dry_run_does_not_capture_and_writes_sources_json(temp_output: Path) -> None:
    with (
        mock.patch.object(article_mod.requests, "get", return_value=_FakeArticleResponse()),
        mock.patch.object(article_mod, "capture_url") as mock_capture,
    ):
        result = capture_article_sources(
            SAMPLE_ARTICLE_URL,
            output_dir=temp_output,
            dry_run=True,
        )

    mock_capture.assert_not_called()
    assert result["succeeded"] == 0
    assert result["failed"] == 0

    sources_path = Path(result["sources_path"])
    assert sources_path.exists()
    summary = json.loads(sources_path.read_text("utf-8"))
    assert summary["remote_sources"] == 4
    for source in summary["sources"]:
        assert source["status"] == "dry-run"
        assert source["ok"] is None


def test_local_file_path_filters_relative_and_social(temp_output: Path) -> None:
    html_path = temp_output / "article.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(SAMPLE_HTML, encoding="utf-8")

    with mock.patch.object(article_mod, "capture_url") as mock_capture:
        result = capture_article_sources(
            str(html_path),
            output_dir=temp_output,
            dry_run=True,
        )

    mock_capture.assert_not_called()
    assert result["article_domain"] == "local-file"

    urls = {s["url"] for s in result["sources"]}
    # Absolute external links are kept even though they share a registrable
    # domain with the article, because a local file has no article domain.
    assert "https://example.com/report.pdf" in urls
    assert "https://www.example.com/about" in urls
    # Protocol-relative links cannot be resolved for a local file base, so they
    # are excluded as non-fetchable.
    assert "https://cdn.example.com/file.css" not in urls
    # Social, anchor, relative, and Substack platform links are still excluded.
    assert "https://www.facebook.com/sharer/sharer.php" not in urls
    assert "https://x.com/intent/tweet" not in urls
    assert "https://thegoodclimate.substack.com/p/other" not in urls
    assert "https://writer.substack.com/p/post" not in urls


def test_full_capture_calls_capture_url_for_each_source(temp_output: Path) -> None:
    def fake_capture(url: str, **kwargs: object) -> dict:
        return {
            "ok": True,
            "url": url,
            "capture_hash": "abc",
            "captured_at": "2026-07-20T00:00:00Z",
            "manifest_path": str(temp_output / "captures" / "abc" / "manifest.json"),
            "artifacts_dir": str(temp_output / "captures" / "abc"),
            "error": None,
        }

    with (
        mock.patch.object(article_mod.requests, "get", return_value=_FakeArticleResponse()),
        mock.patch.object(article_mod, "capture_url", side_effect=fake_capture) as mock_capture,
    ):
        result = capture_article_sources(
            SAMPLE_ARTICLE_URL,
            output_dir=temp_output,
            dry_run=False,
            delay=0.0,
        )

    expected_urls = [
        "https://other.com/news",
        "https://another.org/paper",
        "https://cdn.example.com/file.css",
        "https://external.io/dataset",
    ]
    assert mock_capture.call_count == len(expected_urls)
    captured_urls = [call.args[0] for call in mock_capture.call_args_list]
    assert captured_urls == expected_urls

    assert result["succeeded"] == 4
    assert result["failed"] == 0
    for source in result["sources"]:
        assert source["ok"] is True
        assert source["warc_path"] is not None
        assert source["warc_path"].endswith("capture.warc.gz")

    sources_path = Path(result["sources_path"])
    summary = json.loads(sources_path.read_text("utf-8"))
    assert summary["succeeded"] == 4


def test_failed_capture_recorded_in_summary(temp_output: Path) -> None:
    def fake_capture(url: str, **kwargs: object) -> dict:
        return {
            "ok": False,
            "url": url,
            "capture_hash": "abc",
            "captured_at": "2026-07-20T00:00:00Z",
            "manifest_path": None,
            "artifacts_dir": None,
            "error": "fetch failed: 404",
        }

    with (
        mock.patch.object(article_mod.requests, "get", return_value=_FakeArticleResponse()),
        mock.patch.object(article_mod, "capture_url", side_effect=fake_capture),
    ):
        result = capture_article_sources(
            SAMPLE_ARTICLE_URL,
            output_dir=temp_output,
            dry_run=False,
            delay=0.0,
        )

    assert result["succeeded"] == 0
    assert result["failed"] == 4
    assert all(s["ok"] is False for s in result["sources"])
    assert all(s["warc_path"] is None for s in result["sources"])


def test_cli_article_dry_run(temp_output: Path) -> None:
    from click.testing import CliRunner
    from source_archive.cli import cli

    fake_result = {
        "article_url": SAMPLE_ARTICLE_URL,
        "article_domain": "example.com",
        "output_dir": str(temp_output / "article_abc123"),
        "sources_path": str(temp_output / "article_abc123" / "sources.json"),
        "total_links": 10,
        "excluded": {"internal": 4, "social": 2, "duplicates": 1},
        "remote_sources": 3,
        "succeeded": 0,
        "failed": 0,
        "sources": [
            {"url": "https://a.example", "ok": None, "status": "dry-run"},
            {"url": "https://b.example", "ok": None, "status": "dry-run"},
            {"url": "https://c.example", "ok": None, "status": "dry-run"},
        ],
    }

    runner = CliRunner()
    with mock.patch("source_archive.cli.capture_article_sources", return_value=fake_result):
        invoke = runner.invoke(cli, ["article", SAMPLE_ARTICLE_URL, "--dry-run"])

    assert invoke.exit_code == 0, invoke.output
    assert "Found 10 total links" in invoke.output
    assert "Dry run — would capture 3 remote sources" in invoke.output
    assert "https://a.example" in invoke.output


def test_cli_article_capture_exits_nonzero_on_failure(temp_output: Path) -> None:
    from click.testing import CliRunner
    from source_archive.cli import cli

    fake_result = {
        "article_url": SAMPLE_ARTICLE_URL,
        "article_domain": "example.com",
        "output_dir": str(temp_output / "article_abc123"),
        "sources_path": str(temp_output / "article_abc123" / "sources.json"),
        "total_links": 2,
        "excluded": {"internal": 0, "social": 0, "duplicates": 0},
        "remote_sources": 2,
        "succeeded": 1,
        "failed": 1,
        "sources": [
            {
                "url": "https://ok.example",
                "ok": True,
                "warc_path": str(temp_output / "captures" / "ok" / "capture.warc.gz"),
            },
            {"url": "https://fail.example", "ok": False, "error": "404"},
        ],
    }

    runner = CliRunner()
    with mock.patch("source_archive.cli.capture_article_sources", return_value=fake_result):
        invoke = runner.invoke(cli, ["article", SAMPLE_ARTICLE_URL])

    assert invoke.exit_code == 1, invoke.output
    assert "Done: 1/2 captured successfully, 1 failed" in invoke.output
