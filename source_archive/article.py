"""Batch capture of remote sources cited by an article.

``capture_article_sources`` fetches an article HTML page (or reads a local file),
extracts external links, filters out internal/social/duplicated URLs, and
captures the remaining remote sources into a single merged WARC bundle.
"""

from __future__ import annotations

import html.parser
import json
import logging
import tempfile
import time
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

log = logging.getLogger(__name__)

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
BUNDLE_WARC_NAME = "bundle.warc.gz"
SOURCES_NAME = "sources.json"


class _LinkExtractor(html.parser.HTMLParser):
    """Collect ``href`` values from ``<a>`` tags."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_dict = dict(attrs)
        href = attr_dict.get("href")
        if href:
            self.links.append(href)


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

    total = len(parser.links)
    counts: dict[str, int] = {
        "internal": 0,
        "social": 0,
        "duplicates": 0,
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


def _capture_source(
    url: str, timeout: int
) -> tuple[list[dict[str, Any]], str | None, str | None, str | None, int | None]:
    """Capture one remote source and return its WARC-ready responses.

    Returns ``(responses, error, content_type, category, status)``:

        - ``responses``: list of ``{"url", "status", "headers", "body"}`` dicts
        - ``error``: error message, or ``None`` on success
        - ``content_type``: the main response Content-Type, or ``None``
        - ``category``: ``"html"``, ``"binary"``, or ``None``
        - ``status``: the main HTTP status code, or ``None``
    """
    try:
        category, content_type, headers, status, body = _classify(url, timeout)
    except requests.RequestException as exc:
        return [], f"fetch failed: {exc}", None, None, None

    if category == "html":
        # Render with Playwright to capture sub-resources. We do not need a
        # screenshot for bundle sources, so disable it and discard the temp dir.
        with tempfile.TemporaryDirectory() as tmpdir:
            rendered = _render_and_capture(
                url, Path(tmpdir), timeout, screenshot=False
            )
        if rendered:
            return (
                rendered["responses"],
                None,
                rendered["main_content_type"],
                "html",
                rendered["main_response_status"],
            )

        # Playwright failed — fall back to the requests body.
        if body is None:
            try:
                status, content_type, headers, body = _fetch(url, timeout)
            except requests.RequestException as exc:
                return [], f"fetch failed: {exc}", None, None, None

        category = classify_content_type(content_type)
        return (
            [{"url": url, "status": status, "headers": headers, "body": body}],
            None,
            content_type,
            category,
            status,
        )

    # Binary path: ensure we have the body bytes.
    if body is None:
        try:
            status, content_type, headers, body = _fetch(url, timeout)
        except requests.RequestException as exc:
            return [], f"fetch failed: {exc}", None, None, None

    return (
        [{"url": url, "status": status, "headers": headers, "body": body}],
        None,
        content_type,
        "binary",
        status,
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
    if url_or_path.startswith(("http://", "https://")):
        article_url = url_or_path
        html, article_status, article_headers, article_bytes = _fetch_article_html(
            article_url, timeout
        )
    else:
        article_url = str(Path(url_or_path).resolve())
        html, article_headers, article_bytes = _read_local_html(url_or_path)
        article_status = 200

    parsed_article = urlparse(article_url)
    article_domain = (
        _normalize_domain(parsed_article.netloc) if parsed_article.netloc else None
    )

    remote_urls, counts = _extract_remote_sources(html, article_url, article_domain)

    article_hash = sha256_text(article_url)[:8]
    article_dir = Path(output_dir) / f"{ARTICLE_PREFIX}{article_hash}"
    article_dir.mkdir(parents=True, exist_ok=True)

    sources_path = article_dir / SOURCES_NAME
    article_html_path = article_dir / ARTICLE_HTML_NAME
    bundle_warc_path = article_dir / BUNDLE_WARC_NAME

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
        # The article itself is the first (index) response record in the bundle.
        all_responses.append(
            {
                "url": article_url,
                "status": article_status,
                "headers": article_headers,
                "body": article_bytes,
            }
        )

        for i, url in enumerate(remote_urls):
            if i > 0:
                time.sleep(delay)
            log.info("Capturing article source %d/%d: %s", i + 1, len(remote_urls), url)

            responses, error, content_type, category, status = _capture_source(
                url, timeout
            )
            all_responses.extend(responses)

            if error:
                failed += 1
                sources.append(
                    {
                        "url": url,
                        "status": "failed",
                        "content_type": content_type,
                        "warc_records": len(responses),
                        "error": error,
                    }
                )
            else:
                succeeded += 1
                sources.append(
                    {
                        "url": url,
                        "status": "ok",
                        "content_type": content_type,
                        "warc_records": len(responses),
                        "error": None,
                    }
                )

        # Write the single merged WARC bundle: metadata + article + sources.
        _write_warc(
            bundle_warc_path,
            article_url,
            all_responses,
            {
                "software": f"source-archive/{__version__}",
                "format": "WARC File Format 1.0",
                "captured_at": captured_at,
                "article_url": article_url,
            },
        )

    total_warc_records = len(all_responses)
    bundle_size_bytes = (
        bundle_warc_path.stat().st_size if bundle_warc_path.exists() else 0
    )

    summary = {
        "article_url": article_url,
        "article_domain": parsed_article.netloc or "local-file",
        "captured_at": captured_at,
        "total_links_found": counts["total"],
        "excluded": {
            "internal": counts["internal"],
            "social": counts["social"],
            "duplicates": counts["duplicates"],
        },
        "bundle_warc": BUNDLE_WARC_NAME,
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
        "bundle_warc_path": str(bundle_warc_path) if not dry_run else None,
        "total_links": counts["total"],
        "excluded": summary["excluded"],
        "remote_sources": len(remote_urls),
        "succeeded": succeeded,
        "failed": failed,
        "sources": sources,
        "captured_at": captured_at,
        "bundle_size_bytes": bundle_size_bytes,
    }
