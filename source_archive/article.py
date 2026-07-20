"""Batch capture of remote sources cited by an article.

``capture_article_sources`` fetches an article HTML page (or reads a local file),
extracts external links, filters out internal/social/duplicated URLs, and
captures the remaining remote sources into WARC files.
"""

from __future__ import annotations

import html.parser
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from .capture import DEFAULT_OUTPUT_DIR, WARC_NAME, capture_url
from .manifest import sha256_text

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


def _fetch_article_html(url: str, timeout: int) -> str:
    """Fetch article HTML from a remote URL."""
    from .capture import _user_agent

    resp = requests.get(url, timeout=timeout, headers={"User-Agent": _user_agent()})
    resp.raise_for_status()
    return resp.text


def _read_local_html(path: str) -> str:
    """Read article HTML from a local file path."""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def capture_article_sources(
    url_or_path: str,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    wayback: bool = False,
    timeout: int = 30,
    dry_run: bool = False,
    delay: float = 1.0,
) -> dict[str, Any]:
    """Extract remote source URLs from an article and capture each one.

    Parameters
    ----------
    url_or_path:
        HTTP/HTTPS URL to fetch, or a local file path to read.
    output_dir:
        Root archive directory. Captures are stored in
        ``<output_dir>/article_<sha256(url_or_path)[:8]>/``.
    wayback:
        Also submit each captured source to the Wayback Machine.
    timeout:
        HTTP/Playwright timeout in seconds.
    dry_run:
        If True, only list the URLs that would be captured.
    delay:
        Seconds to wait between captures (ignored in dry-run mode).

    Returns
    -------
    A result dictionary with article metadata, exclusion counts, and a list of
    source capture results.
    """
    if url_or_path.startswith(("http://", "https://")):
        article_url = url_or_path
        html = _fetch_article_html(article_url, timeout)
    else:
        article_url = str(Path(url_or_path).resolve())
        html = _read_local_html(url_or_path)

    parsed_article = urlparse(article_url)
    article_domain = _normalize_domain(parsed_article.netloc) if parsed_article.netloc else None

    remote_urls, counts = _extract_remote_sources(html, article_url, article_domain)

    article_hash = sha256_text(article_url)[:8]
    article_dir = Path(output_dir) / f"{ARTICLE_PREFIX}{article_hash}"
    article_dir.mkdir(parents=True, exist_ok=True)
    sources_path = article_dir / "sources.json"

    sources: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0

    if dry_run:
        for url in remote_urls:
            sources.append(
                {
                    "url": url,
                    "ok": None,
                    "status": "dry-run",
                    "error": None,
                    "warc_path": None,
                    "manifest_path": None,
                }
            )
    else:
        # Capture each remote URL through the existing capture pipeline.
        results = _capture_remote_urls(
            remote_urls,
            output_dir=article_dir,
            wayback=wayback,
            timeout=timeout,
            delay=delay,
        )
        for result in results:
            ok = bool(result.get("ok"))
            if ok:
                succeeded += 1
                warc_path = (
                    str(Path(result["artifacts_dir"]) / WARC_NAME)
                    if result.get("artifacts_dir")
                    else None
                )
            else:
                failed += 1
                warc_path = None

            sources.append(
                {
                    "url": result["url"],
                    "ok": ok,
                    "error": result.get("error"),
                    "warc_path": warc_path,
                    "manifest_path": result.get("manifest_path"),
                }
            )

    summary = {
        "article_url": article_url,
        "article_domain": parsed_article.netloc or "local-file",
        "total_links": counts["total"],
        "excluded": {
            "internal": counts["internal"],
            "social": counts["social"],
            "duplicates": counts["duplicates"],
        },
        "remote_sources": len(remote_urls),
        "succeeded": succeeded,
        "failed": failed,
        "sources": sources,
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
        "total_links": counts["total"],
        "excluded": summary["excluded"],
        "remote_sources": len(remote_urls),
        "succeeded": succeeded,
        "failed": failed,
        "sources": sources,
    }


def _capture_remote_urls(
    urls: list[str],
    *,
    output_dir: Path,
    wayback: bool,
    timeout: int,
    delay: float,
) -> list[dict[str, Any]]:
    """Call ``capture_url`` for every URL, waiting ``delay`` seconds between."""
    results: list[dict[str, Any]] = []
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(delay)
        log.info("Capturing article source %d/%d: %s", i + 1, len(urls), url)
        results.append(
            capture_url(
                url,
                output_dir=output_dir,
                wayback=wayback,
                timeout=timeout,
            )
        )
    return results
