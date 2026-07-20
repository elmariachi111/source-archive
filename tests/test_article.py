"""Tests for source_archive.article batch source extraction."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from warcio.archiveiterator import ArchiveIterator

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

    @property
    def content(self) -> bytes:
        return SAMPLE_HTML.encode("utf-8")

    def raise_for_status(self) -> None:
        pass


@pytest.fixture
def temp_output(tmp_path: Path) -> Path:
    """Clean per-test archive directory."""
    return tmp_path / "archive"


def test_extract_filters_remote_url(temp_output: Path) -> None:
    with mock.patch.object(
        article_mod.requests, "get", return_value=_FakeArticleResponse()
    ):
        result = capture_article_sources(
            SAMPLE_ARTICLE_URL,
            output_dir=temp_output,
            dry_run=True,
        )

    assert result["article_url"] == SAMPLE_ARTICLE_URL
    assert result["article_domain"] == "example.com"
    assert result["total_links"] == 17
    assert result["excluded"]["internal"] == 7
    assert result["excluded"]["social"] == 4
    assert result["excluded"]["duplicates"] == 2

    expected_remote = [
        "https://other.com/news",
        "https://another.org/paper",
        "https://cdn.example.com/file.css",
        "https://external.io/dataset",
    ]
    assert [s["url"] for s in result["sources"]] == expected_remote


def test_dry_run_does_not_capture_and_writes_sources_json(temp_output: Path) -> None:
    with mock.patch.object(
        article_mod.requests, "get", return_value=_FakeArticleResponse()
    ):
        result = capture_article_sources(
            SAMPLE_ARTICLE_URL,
            output_dir=temp_output,
            dry_run=True,
        )

    assert result["succeeded"] == 0
    assert result["failed"] == 0

    sources_path = Path(result["sources_path"])
    assert sources_path.exists()
    summary = json.loads(sources_path.read_text("utf-8"))
    assert summary["summary"]["total_sources"] == 4
    assert summary["bundle_warc"] == "bundle.warc.gz"
    assert summary["article_html"] == "article.html"
    for source in summary["sources"]:
        assert source["status"] == "dry-run"
        assert source["warc_records"] == 0


def test_local_file_path_filters_relative_and_social(temp_output: Path) -> None:
    html_path = temp_output / "article.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(SAMPLE_HTML, encoding="utf-8")

    result = capture_article_sources(
        str(html_path),
        output_dir=temp_output,
        dry_run=True,
    )

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


def _fake_capture_source(
    url: str, timeout: int
) -> tuple[list[dict], str | None, str | None, str | None, int | None]:
    """Return deterministic source responses for unit tests."""
    if "other.com" in url:
        return (
            [
                {
                    "url": url,
                    "status": 200,
                    "headers": {"content-type": "text/html; charset=utf-8"},
                    "body": b"<html>Other</html>",
                },
                {
                    "url": f"{url}/style.css",
                    "status": 200,
                    "headers": {"content-type": "text/css"},
                    "body": b"body { color: black; }",
                },
            ],
            None,
            "text/html; charset=utf-8",
            "html",
            200,
        )
    if "another.org" in url:
        return (
            [
                {
                    "url": url,
                    "status": 200,
                    "headers": {"content-type": "application/pdf"},
                    "body": b"%PDF-1.4 fake",
                }
            ],
            None,
            "application/pdf",
            "binary",
            200,
        )
    if "cdn.example.com" in url:
        return (
            [
                {
                    "url": url,
                    "status": 200,
                    "headers": {"content-type": "text/css"},
                    "body": b".css {}",
                }
            ],
            None,
            "text/css",
            "binary",
            200,
        )
    # external.io/dataset
    return (
        [
            {
                "url": url,
                "status": 200,
                "headers": {"content-type": "text/html; charset=utf-8"},
                "body": b"<html>Dataset</html>",
            }
        ],
        None,
        "text/html; charset=utf-8",
        "html",
        200,
    )


def test_full_capture_writes_bundle_warc_article_html_and_sources_json(
    temp_output: Path,
) -> None:
    with (
        mock.patch.object(
            article_mod.requests, "get", return_value=_FakeArticleResponse()
        ),
        mock.patch.object(
            article_mod, "_capture_source", side_effect=_fake_capture_source
        ) as mock_capture,
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
    assert result["bundle_warc_path"] is not None
    assert result["article_html_path"] is not None

    article_dir = Path(result["output_dir"])
    bundle_path = article_dir / "bundle.warc.gz"
    html_path = article_dir / "article.html"
    sources_path = article_dir / "sources.json"
    assert bundle_path.exists()
    assert html_path.exists()
    assert sources_path.exists()

    # Standalone article.html matches the fetched bytes.
    assert html_path.read_bytes() == SAMPLE_HTML.encode("utf-8")

    # sources.json uses the new merged-bundle format.
    summary = json.loads(sources_path.read_text("utf-8"))
    assert summary["bundle_warc"] == "bundle.warc.gz"
    assert summary["article_html"] == "article.html"
    assert summary["summary"]["total_sources"] == 4
    assert summary["summary"]["succeeded"] == 4
    assert summary["summary"]["failed"] == 0
    # article (1) + other.com/news (2) + another.org/paper (1) + cdn css (1) + external.io (1)
    assert summary["summary"]["total_warc_records"] == 6

    # WARC is valid and article HTML is the first response record.
    records = list(ArchiveIterator(bundle_path.open("rb")))
    # metadata + article + 5 source responses (other.com has a subresource)
    assert len(records) == 7
    types = [r.rec_type for r in records]
    assert types[0] == "metadata"
    assert types[1] == "response"

    response_records = [r for r in records if r.rec_type == "response"]
    assert response_records[0].rec_headers.get_header("WARC-Target-URI") == SAMPLE_ARTICLE_URL

    warc_response_urls = {
        r.rec_headers.get_header("WARC-Target-URI") for r in response_records
    }
    assert SAMPLE_ARTICLE_URL in warc_response_urls
    for url in expected_urls:
        assert url in warc_response_urls


def test_failed_capture_recorded_in_summary(temp_output: Path) -> None:
    def fail_capture(
        url: str, timeout: int
    ) -> tuple[list[dict], str | None, str | None, str | None, int | None]:
        return [], "fetch failed: 404 Not Found", None, None, None

    with (
        mock.patch.object(
            article_mod.requests, "get", return_value=_FakeArticleResponse()
        ),
        mock.patch.object(article_mod, "_capture_source", side_effect=fail_capture),
    ):
        result = capture_article_sources(
            SAMPLE_ARTICLE_URL,
            output_dir=temp_output,
            dry_run=False,
            delay=0.0,
        )

    assert result["succeeded"] == 0
    assert result["failed"] == 4
    for source in result["sources"]:
        assert source["status"] == "failed"
        assert "404" in source["error"]
        assert source["warc_records"] == 0

    # Bundle still contains the article index record only.
    bundle_path = Path(result["bundle_warc_path"])
    records = list(ArchiveIterator(bundle_path.open("rb")))
    response_records = [r for r in records if r.rec_type == "response"]
    assert len(response_records) == 1
    assert response_records[0].rec_headers.get_header("WARC-Target-URI") == SAMPLE_ARTICLE_URL


def test_cli_article_dry_run(temp_output: Path) -> None:
    from click.testing import CliRunner
    from source_archive.cli import cli

    fake_result = {
        "article_url": SAMPLE_ARTICLE_URL,
        "article_domain": "example.com",
        "output_dir": str(temp_output / "article_abc123"),
        "sources_path": str(temp_output / "article_abc123" / "sources.json"),
        "article_html_path": str(temp_output / "article_abc123" / "article.html"),
        "bundle_warc_path": None,
        "total_links": 10,
        "excluded": {"internal": 4, "social": 2, "duplicates": 1},
        "remote_sources": 3,
        "succeeded": 0,
        "failed": 0,
        "sources": [
            {"url": "https://a.example", "status": "dry-run", "warc_records": 0},
            {"url": "https://b.example", "status": "dry-run", "warc_records": 0},
            {"url": "https://c.example", "status": "dry-run", "warc_records": 0},
        ],
    }

    runner = CliRunner()
    with mock.patch(
        "source_archive.cli.capture_article_sources", return_value=fake_result
    ):
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
        "article_html_path": str(temp_output / "article_abc123" / "article.html"),
        "bundle_warc_path": str(temp_output / "article_abc123" / "bundle.warc.gz"),
        "total_links": 2,
        "excluded": {"internal": 0, "social": 0, "duplicates": 0},
        "remote_sources": 2,
        "succeeded": 1,
        "failed": 1,
        "sources": [
            {
                "url": "https://ok.example",
                "status": "ok",
                "content_type": "text/html",
                "warc_records": 3,
                "error": None,
            },
            {
                "url": "https://fail.example",
                "status": "failed",
                "content_type": None,
                "warc_records": 0,
                "error": "404",
            },
        ],
    }

    runner = CliRunner()
    with mock.patch(
        "source_archive.cli.capture_article_sources", return_value=fake_result
    ):
        invoke = runner.invoke(cli, ["article", SAMPLE_ARTICLE_URL])

    assert invoke.exit_code == 1, invoke.output
    assert "Done: 1/2 captured successfully, 1 failed" in invoke.output
    assert "https://ok.example" in invoke.output
    assert "https://fail.example" in invoke.output
