"""Core capture logic: fetch, render, extract, and write artifacts.

``capture_url`` is the single entry point used by both the ``capture`` and
``batch`` CLI commands. It returns a result dict so callers can aggregate
outcomes without re-reading the manifest.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import requests
import trafilatura

from .manifest import (
    artifact_entry,
    build_manifest,
    sha256_text,
    utc_now_iso,
    write_manifest,
)
from .wayback import save_to_wayback

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_OUTPUT_DIR = Path("./archive")

# Artifacts that may still be produced even if Playwright is unavailable.
RAW_HTML_NAME = "raw.html"
HEADERS_NAME = "headers.json"
SINGLEFILE_NAME = "singlefile.html"
PDF_NAME = "output.pdf"
SCREENSHOT_NAME = "screenshot.png"
ARTICLE_NAME = "article.txt"
MANIFEST_NAME = "manifest.json"


def capture_dir_for(output_dir: Path, url: str) -> Path:
    """Return ``<output_dir>/captures/<sha256(url)[:16]>`` for a URL."""
    from .manifest import sha256_text

    prefix = sha256_text(url)[:16]
    return output_dir / "captures" / prefix


def _fetch(url: str, timeout: int) -> tuple[int, str | None, dict[str, str], bytes]:
    """Fetch raw HTML + headers with requests. Raises on network failure."""
    resp = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; source-archive/0.1; "
                "+https://github.com/climate/source-archive)"
            )
        },
    )
    content_type = resp.headers.get("Content-Type")
    # ``requests`` headers are case-insensitive; convert to a plain dict of
    # lower-cased keys for deterministic manifest storage.
    headers = {k.lower(): v for k, v in resp.headers.items()}
    return resp.status_code, content_type, headers, resp.content


def _render_with_playwright(
    url: str, dest_dir: Path, timeout: int
) -> dict[str, Path]:
    """Render the page with headless Chromium and write singlefile/pdf/screenshot.

    Returns a dict mapping artifact-name -> Path. If Playwright cannot launch
    or the page fails to load, returns an empty dict (caller falls back to
    requests-only capture).
    """
    artifacts: dict[str, Path] = {}
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
                # singlefile.html — self-contained HTML with inlined assets.
                singlefile_path = dest_dir / SINGLEFILE_NAME
                # Page.content() returns the DOM HTML; this is a reasonable
                # single-file representation. For true asset inlining a
                # post-processor would be needed, but networkidle ensures most
                # resources are resolved at capture time.
                html = page.content()
                singlefile_path.write_text(html, encoding="utf-8")
                artifacts["singlefile_html"] = singlefile_path

                # PDF print of the rendered page.
                pdf_path = dest_dir / PDF_NAME
                page.pdf(path=str(pdf_path))
                artifacts["pdf"] = pdf_path

                # Full-page screenshot.
                screenshot_path = dest_dir / SCREENSHOT_NAME
                page.screenshot(path=str(screenshot_path), full_page=True)
                artifacts["screenshot"] = screenshot_path
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — broad on purpose, fall back.
        log.warning("Playwright render failed for %s: %s — falling back to requests-only.", url, exc)
        # Clean up partial artifacts.
        for name in (SINGLEFILE_NAME, PDF_NAME, SCREENSHOT_NAME):
            p_ = dest_dir / name
            if p_.exists():
                try:
                    p_.unlink()
                except OSError:
                    pass
        return {}
    return artifacts


def _extract_article(raw_html: str, url: str, dest_dir: Path) -> Path | None:
    """Extract article text with trafilatura. Returns the path, or None."""
    try:
        text = trafilatura.extract(raw_html, url=url, include_links=True) or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("trafilatura extraction failed for %s: %s", url, exc)
        return None
    if not text.strip():
        log.info("No article text extracted for %s", url)
        return None
    article_path = dest_dir / ARTICLE_NAME
    article_path.write_text(text, encoding="utf-8")
    return article_path


def capture_url(
    url: str,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    wayback: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Capture a single URL and write all artifacts + manifest + index row.

    Returns a result dict::

        {
          "ok": bool,
          "url": str,
          "capture_hash": str,
          "captured_at": str,
          "manifest_path": str | None,
          "artifacts_dir": str | None,
          "error": str | None,
        }
    """
    captured_at = utc_now_iso()
    result: dict[str, Any] = {
        "ok": False,
        "url": url,
        "capture_hash": "",
        "captured_at": captured_at,
        "manifest_path": None,
        "artifacts_dir": None,
        "error": None,
    }

    # Determine output directories from the URL hash.
    capture_hash = sha256_text(url)
    capture_dir = capture_dir_for(output_dir, url)
    capture_dir.mkdir(parents=True, exist_ok=True)
    result["capture_hash"] = capture_hash

    # --- 1. Fetch raw HTML + headers ---------------------------------------
    try:
        status, content_type, response_headers, raw_bytes = _fetch(url, timeout)
    except requests.RequestException as exc:
        result["error"] = f"fetch failed: {exc}"
        log.error("Capture failed for %s: %s", url, exc)
        return result

    raw_html_path = capture_dir / RAW_HTML_NAME
    raw_html_path.write_bytes(raw_bytes)
    headers_path = capture_dir / HEADERS_NAME
    import json

    headers_path.write_text(
        json.dumps(response_headers, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Decode raw HTML for text extraction.
    try:
        raw_html_text = raw_bytes.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        raw_html_text = ""

    # --- 2. Render with Playwright ----------------------------------------
    rendered = _render_with_playwright(url, capture_dir, timeout)

    # --- 3. Extract article text -------------------------------------------
    article_path = _extract_article(raw_html_text, url, capture_dir)

    # --- 4. Optional Wayback submission ------------------------------------
    wayback_url: str | None = None
    if wayback:
        wayback_url = save_to_wayback(url)
        if wayback_url is None:
            log.warning("Wayback capture skipped/failed for %s", url)

    # --- 5. Build manifest with artifact descriptors ----------------------
    artifacts_desc: dict[str, dict[str, Any]] = {}
    artifacts_desc["raw_html"] = artifact_entry(RAW_HTML_NAME, raw_html_path)
    if rendered:
        if "singlefile_html" in rendered:
            artifacts_desc["singlefile_html"] = artifact_entry(
                SINGLEFILE_NAME, rendered["singlefile_html"]
            )
        if "pdf" in rendered:
            artifacts_desc["pdf"] = artifact_entry(PDF_NAME, rendered["pdf"])
        if "screenshot" in rendered:
            artifacts_desc["screenshot"] = artifact_entry(
                SCREENSHOT_NAME, rendered["screenshot"]
            )
    if article_path is not None:
        artifacts_desc["article_text"] = artifact_entry(ARTICLE_NAME, article_path)

    manifest = build_manifest(
        url=url,
        captured_at=captured_at,
        http_status=status,
        content_type=content_type,
        response_headers=response_headers,
        artifacts=artifacts_desc,
        wayback_url=wayback_url,
    )

    manifest_path = capture_dir / MANIFEST_NAME
    write_manifest(manifest, manifest_path)

    result.update(
        {
            "ok": True,
            "manifest_path": str(manifest_path),
            "artifacts_dir": str(capture_dir),
        }
    )

    # --- 6. Insert into SQLite index --------------------------------------
    from .db import get_db_path, init_db, insert_capture

    db_path = get_db_path(output_dir)
    init_db(db_path)
    insert_capture(
        db_path,
        url=url,
        capture_hash=capture_hash,
        captured_at=captured_at,
        manifest_path=str(manifest_path),
        artifacts_dir=str(capture_dir),
        http_status=status,
        wayback_url=wayback_url,
    )

    return result


def capture_urls(urls: list[str], delay: float = 2.0, **kwargs: Any) -> list[dict[str, Any]]:
    """Capture a list of URLs sequentially with a delay between captures."""
    import time

    results: list[dict[str, Any]] = []
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(delay)
        log.info("Capturing %d/%d: %s", i + 1, len(urls), url)
        results.append(capture_url(url, **kwargs))
    return results