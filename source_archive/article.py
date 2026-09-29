"""Batch capture of remote sources cited by an article.

``capture_article_sources`` fetches an article HTML page (or reads a local file),
extracts external links, filters out internal/social/duplicated URLs, and
captures the article plus the remaining remote sources into a single WACZ
bundle: one WARC with every record, a CDXJ index, and a page list that puts
the article first.
"""

from __future__ import annotations

import html
import html.parser
import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from . import __version__
from .capture import (
    DEFAULT_OUTPUT_DIR,
    _classify,
    _fetch,
    _render_and_capture,
    _user_agent,
    _write_warc,
    classify_content_type,
)
from .manifest import sha256_text, utc_now_iso
from .wacz import Page, write_wacz

log = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Social platforms and share-link hosts to ignore.
SOCIAL_HOSTS = {
    "facebook.com",
    "twitter.com",
    "x.com",
    "threads.com",
    "linkedin.com",
    "reddit.com",
    "whatsapp.com",
    "telegram.org",
}

# Non-fetchable schemes.
IGNORED_SCHEMES = {"mailto", "tel", "javascript", "data"}

# Canonical article subdirectory prefix.
ARTICLE_PREFIX = "article_"

ARTICLE_HTML_NAME = "article.html"
BUNDLE_WACZ_NAME = "bundle.wacz"
# Name of the WARC inside the WACZ (at ``archive/bundle.warc.gz``).
BUNDLE_WARC_NAME = "bundle.warc.gz"
SOURCES_NAME = "sources.json"


class _LinkExtractor(html.parser.HTMLParser):
    """Collect ``href`` values from ``<a>`` tags.

    Links inside ``<noscript>`` are boilerplate ("please enable JavaScript"),
    never citations, so they are counted separately and not collected.
    """

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.noscript_links = 0
        self._noscript_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "noscript":
            self._noscript_depth += 1
            return
        if tag != "a":
            return
        attr_dict = dict(attrs)
        href = attr_dict.get("href")
        if not href:
            return
        if self._noscript_depth:
            self.noscript_links += 1
        else:
            self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "noscript" and self._noscript_depth:
            self._noscript_depth -= 1


def _normalize_domain(hostname: str) -> str:
    """Lowercase and strip a leading ``www.`` for comparison."""
    host = hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_substack(hostname: str) -> bool:
    """Return True if the host is any substack.com property."""
    norm = _normalize_domain(hostname)
    return norm == "substack.com" or norm.endswith(".substack.com")


def _is_social(hostname: str) -> bool:
    """Return True if the host is a known social/share platform."""
    return _normalize_domain(hostname) in SOCIAL_HOSTS


def _resolve_link(href: str, article_url: str) -> str | None:
    """Resolve and canonicalize a single ``<a href>`` value.

    Returns a normalized HTTP/HTTPS URL with the fragment removed, or ``None``
    if the link is not a remote fetchable URL.
    """
    href = href.strip()
    if not href or href.startswith("#"):
        return None

    joined = urljoin(article_url, href)
    parsed = urlparse(joined)

    if parsed.scheme in IGNORED_SCHEMES:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    # Drop fragments; they do not change the captured resource.
    no_fragment = parsed._replace(fragment="")
    return no_fragment.geturl()


def _extract_remote_sources(
    html: str,
    article_url: str,
    article_domain: str | None,
) -> tuple[list[str], dict[str, int]]:
    """Extract and filter ``<a>`` links from article HTML.

    Returns ``(remote_urls, excluded_counts)`` where ``excluded_counts`` maps
    reasons to the number of links excluded.
    """
    parser = _LinkExtractor()
    parser.feed(html)

    total = len(parser.links) + parser.noscript_links
    counts: dict[str, int] = {
        "internal": 0,
        "social": 0,
        "duplicates": 0,
        "noscript": parser.noscript_links,
    }

    seen: set[str] = set()
    remote: list[str] = []
    article_url_norm = urldefrag(article_url.lower())[0]

    for raw in parser.links:
        resolved = _resolve_link(raw, article_url)
        if resolved is None:
            # Anchor-only, relative without domain, or non-HTTP scheme.
            # Treat mailto:/tel: and known social hosts as social, everything
            # else as internal/navigation.
            raw_lower = raw.lower()
            if (
                raw_lower.startswith(("mailto:", "tel:"))
                or _is_social(urlparse(raw).netloc)
            ):
                counts["social"] += 1
            else:
                counts["internal"] += 1
            continue

        normalized = resolved.lower()

        # Exact self-link to the article itself.
        if urldefrag(normalized)[0] == article_url_norm:
            counts["internal"] += 1
            continue

        parsed = urlparse(resolved)
        link_domain = _normalize_domain(parsed.netloc)

        # Same domain as the article (including ``www.`` variants).
        if article_domain is not None and link_domain == article_domain:
            counts["internal"] += 1
            continue

        # Substack platform links (the article itself lives on Substack).
        if _is_substack(parsed.netloc):
            counts["internal"] += 1
            continue

        # Social media / share links.
        if _is_social(parsed.netloc):
            counts["social"] += 1
            continue

        # Duplicate URLs (fragment-stripped).
        if resolved in seen:
            counts["duplicates"] += 1
            continue
        seen.add(resolved)

        remote.append(resolved)

    return remote, {"total": total, **counts}


def _fetch_article_html(
    url: str, timeout: int
) -> tuple[str, int, dict[str, str], bytes]:
    """Fetch article HTML from a remote URL.

    Returns the decoded text, HTTP status, lower-cased headers, and raw bytes.
    """
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": _user_agent()})
    resp.raise_for_status()
    headers = {k.lower(): v for k, v in resp.headers.items()}
    return resp.text, resp.status_code, headers, resp.content


def _read_local_html(
    path: str,
) -> tuple[str, dict[str, str], bytes]:
    """Read article HTML from a local file path.

    Returns the decoded text, synthetic headers, and UTF-8 bytes. Local files
    have no real HTTP response, so we supply minimal ``text/html`` headers.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    headers = {"content-type": "text/html; charset=utf-8"}
    return text, headers, text.encode("utf-8", errors="replace")


@dataclass
class SourceCapture:
    """Everything captured for one URL, ready to go into the bundle.

    ``responses`` are ``{"url", "status", "headers", "body", "fetched_at"}``
    dicts. ``page_url`` is the URL of the main document after redirects;
    it may differ from the URL that was requested.
    """

    responses: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    content_type: str | None = None
    category: str | None = None
    status: int | None = None
    page_url: str | None = None
    title: str | None = None

    def main_response(self) -> dict[str, Any] | None:
        """The main document's (non-redirect) response record, if captured."""
        for item in self.responses:
            if item["url"] == self.page_url and not 300 <= item["status"] < 400:
                return item
        return None


def _html_title(body: bytes) -> str | None:
    """Extract ``<title>`` from raw HTML bytes, or ``None``."""
    match = _TITLE_RE.search(body[:200_000].decode("utf-8", errors="replace"))
    if not match:
        return None
    title = " ".join(html.unescape(match.group(1)).split())
    return title or None


def _decode_html(body: bytes, content_type: str | None) -> str:
    """Decode HTML bytes using the Content-Type charset, defaulting to UTF-8."""
    charset = "utf-8"
    for param in (content_type or "").split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip("\"'")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _single_response_capture(
    url: str,
    status: int,
    content_type: str | None,
    headers: dict[str, str],
    body: bytes,
    category: str | None,
) -> SourceCapture:
    """Wrap a plain ``requests`` fetch as a one-record capture."""
    return SourceCapture(
        responses=[
            {
                "url": url,
                "status": status,
                "headers": headers,
                "body": body,
                "fetched_at": utc_now_iso(),
            }
        ],
        content_type=content_type,
        category=category,
        status=status,
        page_url=url,
        title=_html_title(body) if category == "html" else None,
    )


def _render(url: str, timeout: int) -> SourceCapture | None:
    """Render ``url`` with Playwright, capturing all sub-resources."""
    # No screenshot for bundle pages, so the temp dir is discarded.
    with tempfile.TemporaryDirectory() as tmpdir:
        rendered = _render_and_capture(url, Path(tmpdir), timeout, screenshot=False)
    if not rendered:
        return None
    return SourceCapture(
        responses=rendered["responses"],
        content_type=rendered["main_content_type"],
        category="html",
        status=rendered["main_response_status"],
        page_url=rendered["main_url"],
        title=rendered["title"],
    )


def _capture_source(url: str, timeout: int) -> SourceCapture:
    """Capture one remote source. Never raises; failures set ``error``."""
    try:
        category, content_type, headers, status, body = _classify(url, timeout)
    except requests.RequestException as exc:
        return SourceCapture(error=f"fetch failed: {exc}")

    if category == "html":
        rendered = _render(url, timeout)
        if rendered:
            return rendered
        # Playwright failed — fall back to the requests body below.

    if body is None:
        try:
            status, content_type, headers, body = _fetch(url, timeout)
        except requests.RequestException as exc:
            return SourceCapture(error=f"fetch failed: {exc}")

    return _single_response_capture(
        url, status, content_type, headers, body, classify_content_type(content_type)
    )


def _capture_article(url: str, timeout: int) -> tuple[SourceCapture, str, bytes]:
    """Capture the article page itself, rendered like any other page.

    Returns the capture plus the article's decoded HTML and raw bytes (used
    for link extraction and ``article.html``). Falls back to a plain
    ``requests`` fetch if rendering fails; raises if the article can't be
    fetched at all.
    """
    rendered = _render(url, timeout)
    if rendered and rendered.status is not None and rendered.status < 400:
        main = rendered.main_response()
        if main is not None and main["body"]:
            body = main["body"]
            return rendered, _decode_html(body, rendered.content_type), body

    text, status, headers, body = _fetch_article_html(url, timeout)
    capture = _single_response_capture(
        url, status, headers.get("content-type"), headers, body, "html"
    )
    return capture, text, body


def _source_entry(url: str, capture: SourceCapture) -> dict[str, Any]:
    """One ``sources.json`` entry for a captured (or failed) source."""
    main = capture.main_response()
    return {
        "url": url,
        "status": "failed" if capture.error else "ok",
        "content_type": capture.content_type,
        "warc_records": len(capture.responses),
        "error": capture.error,
        "page_url": capture.page_url,
        "title": capture.title,
        "fetched_at": main.get("fetched_at") if main else None,
    }


def _page_for(capture: SourceCapture, label: str | None = None) -> Page | None:
    """The replay page-list entry for a capture, if it has a replayable page."""
    main = capture.main_response()
    if main is None or not main["url"].startswith(("http://", "https://")):
        return None
    title = capture.title or main["url"]
    return Page(
        url=main["url"],
        ts=main["fetched_at"],
        title=f"{label}: {title}" if label else title,
    )


def capture_article_sources(
    url_or_path: str,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    wayback: bool = False,
    timeout: int = 30,
    dry_run: bool = False,
    delay: float = 1.0,
) -> dict[str, Any]:
    """Extract remote source URLs from an article and capture them as one bundle.

    Parameters
    ----------
    url_or_path:
        HTTP/HTTPS URL to fetch, or a local file path to read.
    output_dir:
        Root archive directory. Output is stored in
        ``<output_dir>/article_<sha256(url_or_path)[:8]>/``.
    wayback:
        Ignored for bundled article sources (kept for CLI compatibility).
    timeout:
        HTTP/Playwright timeout in seconds.
    dry_run:
        If True, only list the URLs that would be captured.
    delay:
        Seconds to wait between source fetches (ignored in dry-run mode).

    Returns
    -------
    A result dictionary with article metadata, exclusion counts, source capture
    results, and paths to the generated bundle files.
    """
    article_capture: SourceCapture | None = None
    if url_or_path.startswith(("http://", "https://")):
        article_url = url_or_path
        if dry_run:
            html_text, _, _, article_bytes = _fetch_article_html(article_url, timeout)
        else:
            article_capture, html_text, article_bytes = _capture_article(
                article_url, timeout
            )
    else:
        article_url = str(Path(url_or_path).resolve())
        html_text, article_headers, article_bytes = _read_local_html(url_or_path)
        article_capture = _single_response_capture(
            article_url,
            200,
            article_headers["content-type"],
            article_headers,
            article_bytes,
            "html",
        )

    parsed_article = urlparse(article_url)
    article_domain = (
        _normalize_domain(parsed_article.netloc) if parsed_article.netloc else None
    )

    remote_urls, counts = _extract_remote_sources(
        html_text, article_url, article_domain
    )

    article_hash = sha256_text(article_url)[:8]
    article_dir = Path(output_dir) / f"{ARTICLE_PREFIX}{article_hash}"
    article_dir.mkdir(parents=True, exist_ok=True)

    sources_path = article_dir / SOURCES_NAME
    article_html_path = article_dir / ARTICLE_HTML_NAME
    bundle_wacz_path = article_dir / BUNDLE_WACZ_NAME

    captured_at = utc_now_iso()

    sources: list[dict[str, Any]] = []
    all_responses: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0

    # Save the article HTML as a standalone readable file.
    article_html_path.write_bytes(article_bytes)

    if dry_run:
        for url in remote_urls:
            sources.append(
                {
                    "url": url,
                    "status": "dry-run",
                    "content_type": None,
                    "warc_records": 0,
                    "error": None,
                }
            )
    else:
        assert article_capture is not None
        # The article's records come first in the bundle.
        all_responses.extend(article_capture.responses)
        source_captures: list[SourceCapture] = []

        for i, url in enumerate(remote_urls):
            if i > 0:
                time.sleep(delay)
            log.info("Capturing article source %d/%d: %s", i + 1, len(remote_urls), url)

            capture = _capture_source(url, timeout)
            all_responses.extend(capture.responses)
            if capture.error:
                failed += 1
            else:
                succeeded += 1
                source_captures.append(capture)
            sources.append(_source_entry(url, capture))

        # Page timestamps must equal their record's WARC-Date, so every record
        # needs a fetch time before pages are derived from them.
        for item in all_responses:
            item.setdefault("fetched_at", captured_at)

        article_page = _page_for(article_capture, label="Article")
        pages = [
            page
            for page in [article_page, *(_page_for(c) for c in source_captures)]
            if page is not None
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            warc_path = Path(tmpdir) / BUNDLE_WARC_NAME
            index = _write_warc(
                warc_path,
                article_url,
                all_responses,
                {
                    "software": f"source-archive/{__version__}",
                    "format": "WARC File Format 1.0",
                    "captured_at": captured_at,
                    "article_url": article_url,
                },
            )
            write_wacz(
                bundle_wacz_path,
                warc_path,
                index,
                pages,
                title=article_capture.title or article_url,
                created=captured_at,
                software=f"source-archive/{__version__}",
                main_page=article_page,
            )

    total_warc_records = len(all_responses)
    bundle_size_bytes = (
        bundle_wacz_path.stat().st_size if bundle_wacz_path.exists() else 0
    )

    summary = {
        "article_url": article_url,
        "article_title": article_capture.title if article_capture else None,
        "article_domain": parsed_article.netloc or "local-file",
        "captured_at": captured_at,
        "total_links_found": counts["total"],
        "excluded": {
            "internal": counts["internal"],
            "social": counts["social"],
            "duplicates": counts["duplicates"],
            "noscript": counts["noscript"],
        },
        "bundle_wacz": BUNDLE_WACZ_NAME,
        "bundle_warc": f"archive/{BUNDLE_WARC_NAME}",
        "article_html": ARTICLE_HTML_NAME,
        "sources": sources,
        "summary": {
            "total_sources": len(remote_urls),
            "succeeded": succeeded,
            "failed": failed,
            "total_warc_records": total_warc_records,
            "bundle_size_bytes": bundle_size_bytes,
        },
    }

    sources_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return {
        "article_url": article_url,
        "article_domain": parsed_article.netloc or "local-file",
        "output_dir": str(article_dir),
        "sources_path": str(sources_path),
        "article_html_path": str(article_html_path),
        "bundle_wacz_path": str(bundle_wacz_path) if not dry_run else None,
        "total_links": counts["total"],
        "excluded": summary["excluded"],
        "remote_sources": len(remote_urls),
        "succeeded": succeeded,
        "failed": failed,
        "sources": sources,
        "captured_at": captured_at,
        "bundle_size_bytes": bundle_size_bytes,
    }
