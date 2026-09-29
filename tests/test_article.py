"""Tests for source_archive.article batch source extraction."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest import mock

import pytest
from warcio.archiveiterator import ArchiveIterator

from source_archive import article as article_mod
from source_archive.article import SourceCapture, capture_article_sources


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


@pytest.fixture(autouse=True)
def no_browser():
    """Never launch Playwright in unit tests; the article falls back to requests."""
    with mock.patch.object(article_mod, "_render_and_capture", return_value=None) as m:
        yield m


def _read_bundle(wacz_path: Path) -> tuple[list, list[dict], list[str], dict]:
    """Return (WARC records, pages, CDXJ lines, datapackage) from a WACZ."""
    with zipfile.ZipFile(wacz_path) as zf:
        with zf.open("archive/bundle.warc.gz") as fh:
            records = [
                (r.rec_type, r.rec_headers, r.content_stream().read())
                for r in ArchiveIterator(fh)
            ]
        pages = [json.loads(line) for line in zf.read("pages/pages.jsonl").splitlines()]
        cdxj = zf.read("indexes/index.cdx").decode().splitlines()
        datapackage = json.loads(zf.read("datapackage.json"))
    return records, pages, cdxj, datapackage


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
    assert summary["bundle_wacz"] == "bundle.wacz"
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


def _one(url: str, content_type: str, body: bytes, **kwargs) -> SourceCapture:
    return SourceCapture(
        responses=[
            {"url": url, "status": 200, "headers": {"content-type": content_type}, "body": body}
        ],
        content_type=content_type,
        status=200,
        page_url=url,
        **kwargs,
    )


def _fake_capture_source(url: str, timeout: int) -> SourceCapture:
    """Return deterministic source responses for unit tests."""
    if "other.com" in url:
        capture = _one(url, "text/html; charset=utf-8", b"<html>Other</html>",
                       category="html", title="Other News")
        capture.responses.append(
            {
                "url": f"{url}/style.css",
                "status": 200,
                "headers": {"content-type": "text/css"},
                "body": b"body { color: black; }",
            }
        )
        return capture
    if "another.org" in url:
        return _one(url, "application/pdf", b"%PDF-1.4 fake", category="binary")
    if "cdn.example.com" in url:
        return _one(url, "text/css", b".css {}", category="binary")
    # external.io/dataset
    return _one(url, "text/html; charset=utf-8", b"<html>Dataset</html>", category="html")


def test_full_capture_writes_bundle_wacz_article_html_and_sources_json(
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
    assert result["bundle_wacz_path"] is not None
    assert result["article_html_path"] is not None

    article_dir = Path(result["output_dir"])
    bundle_path = article_dir / "bundle.wacz"
    assert not (article_dir / "bundle.warc.gz").exists()
    html_path = article_dir / "article.html"
    sources_path = article_dir / "sources.json"
    assert bundle_path.exists()
    assert html_path.exists()
    assert sources_path.exists()

    # Standalone article.html matches the fetched bytes.
    assert html_path.read_bytes() == SAMPLE_HTML.encode("utf-8")

    # sources.json uses the new merged-bundle format.
    summary = json.loads(sources_path.read_text("utf-8"))
    assert summary["bundle_wacz"] == "bundle.wacz"
    assert summary["bundle_warc"] == "archive/bundle.warc.gz"
    assert summary["article_html"] == "article.html"
    assert summary["article_title"] == "Article"
    assert summary["summary"]["total_sources"] == 4
    assert summary["summary"]["succeeded"] == 4
    assert summary["summary"]["failed"] == 0
    # article (1) + other.com/news (2) + another.org/paper (1) + cdn css (1) + external.io (1)
    assert summary["summary"]["total_warc_records"] == 6
    assert summary["summary"]["bundle_size_bytes"] == bundle_path.stat().st_size

    records, pages, cdxj, datapackage = _read_bundle(bundle_path)

    # WARC is valid and article HTML is the first response record.
    # metadata + article + 5 source responses (other.com has a subresource)
    assert len(records) == 7
    types = [rec_type for rec_type, _, _ in records]
    assert types[0] == "metadata"
    assert types[1] == "response"
    response_headers = [h for rec_type, h, _ in records if rec_type == "response"]
    assert response_headers[0].get_header("WARC-Target-URI") == SAMPLE_ARTICLE_URL
    warc_response_urls = {h.get_header("WARC-Target-URI") for h in response_headers}
    for url in expected_urls:
        assert url in warc_response_urls

    # Page list: header, then the article first, then one page per source.
    assert pages[0]["format"] == "json-pages-1.0"
    assert pages[1]["url"] == SAMPLE_ARTICLE_URL
    assert pages[1]["title"] == "Article: Article"
    assert [p["url"] for p in pages[2:]] == expected_urls
    assert pages[2]["title"] == "Other News"
    # Each page's ts matches its record's WARC-Date exactly (replay needs this).
    dates = {h.get_header("WARC-Target-URI"): h.get_header("WARC-Date") for h in response_headers}
    for page in pages[1:]:
        assert page["ts"] == dates[page["url"]]

    # Every response record is indexed; the article is the main page.
    assert len(cdxj) == 6
    assert datapackage["mainPageURL"] == SAMPLE_ARTICLE_URL
    assert {r["path"] for r in datapackage["resources"]} == {
        "pages/pages.jsonl",
        "indexes/index.cdx",
        "archive/bundle.warc.gz",
    }


def test_failed_capture_recorded_in_summary(temp_output: Path) -> None:
    def fail_capture(url: str, timeout: int) -> SourceCapture:
        return SourceCapture(error="fetch failed: 404 Not Found")

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

    # Bundle still contains the article record and page only.
    records, pages, _, _ = _read_bundle(Path(result["bundle_wacz_path"]))
    response_headers = [h for rec_type, h, _ in records if rec_type == "response"]
    assert len(response_headers) == 1
    assert response_headers[0].get_header("WARC-Target-URI") == SAMPLE_ARTICLE_URL
    assert [p["url"] for p in pages[1:]] == [SAMPLE_ARTICLE_URL]


def test_cli_article_dry_run(temp_output: Path) -> None:
    from click.testing import CliRunner
    from source_archive.cli import cli

    fake_result = {
        "article_url": SAMPLE_ARTICLE_URL,
        "article_domain": "example.com",
        "output_dir": str(temp_output / "article_abc123"),
        "sources_path": str(temp_output / "article_abc123" / "sources.json"),
        "article_html_path": str(temp_output / "article_abc123" / "article.html"),
        "bundle_wacz_path": None,
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
        "bundle_wacz_path": str(temp_output / "article_abc123" / "bundle.wacz"),
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


def test_noscript_links_are_excluded(temp_output: Path) -> None:
    page = """<html><body>
      <noscript><a href="https://enable-javascript.com/">Enable JS</a></noscript>
      <a href="https://other.com/news">Real citation</a>
    </body></html>"""
    html_path = temp_output / "noscript.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(page, encoding="utf-8")

    result = capture_article_sources(str(html_path), output_dir=temp_output, dry_run=True)

    assert [s["url"] for s in result["sources"]] == ["https://other.com/news"]
    assert result["excluded"]["noscript"] == 1
    assert result["total_links"] == 2


def test_article_is_rendered_and_redirect_target_becomes_page(
    temp_output: Path, no_browser: mock.MagicMock
) -> None:
    """The article is captured with sub-resources; a redirect keeps the cited URL."""
    final_url = "https://example.com/article/"
    no_browser.return_value = {
        "responses": [
            {"url": SAMPLE_ARTICLE_URL, "status": 301,
             "headers": {"location": final_url}, "body": b"",
             "fetched_at": "2026-09-29T17:44:01Z"},
            {"url": final_url, "status": 200,
             "headers": {"content-type": "text/html; charset=utf-8"},
             "body": SAMPLE_HTML.encode(), "fetched_at": "2026-09-29T17:44:02Z"},
            {"url": "https://example.com/style.css", "status": 200,
             "headers": {"content-type": "text/css"}, "body": b"h1 {}",
             "fetched_at": "2026-09-29T17:44:03Z"},
        ],
        "main_url": final_url,
        "title": "Rendered Title",
        "main_response_status": 200,
        "main_content_type": "text/html; charset=utf-8",
    }

    with (
        mock.patch.object(article_mod.requests, "get") as requests_get,
        mock.patch.object(article_mod, "_capture_source",
                          side_effect=lambda url, timeout: SourceCapture(error="skip")),
    ):
        result = capture_article_sources(
            SAMPLE_ARTICLE_URL, output_dir=temp_output, dry_run=False, delay=0.0
        )

    # Rendering succeeded, so the article was not fetched a second time.
    requests_get.assert_not_called()
    # Links are extracted from the rendered article's HTML.
    assert result["remote_sources"] == 4
    assert Path(result["article_html_path"]).read_bytes() == SAMPLE_HTML.encode()

    records, pages, cdxj, datapackage = _read_bundle(Path(result["bundle_wacz_path"]))
    dates = [h.get_header("WARC-Date") for t, h, _ in records if t == "response"]
    # Each record keeps its own fetch time.
    assert dates == ["2026-09-29T17:44:01Z", "2026-09-29T17:44:02Z", "2026-09-29T17:44:03Z"]
    assert pages[1] == {
        "id": pages[1]["id"],
        "url": final_url,
        "ts": "2026-09-29T17:44:02Z",
        "title": "Article: Rendered Title",
    }
    assert datapackage["title"] == "Rendered Title"
    assert datapackage["mainPageURL"] == final_url
    # The redirect is indexed, so the cited URL resolves during replay.
    assert any(json.loads(line.split(" ", 2)[2])["status"] == "301" for line in cdxj)
