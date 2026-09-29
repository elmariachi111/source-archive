"""Core capture logic: fetch, render, extract, and write artifacts.

``capture_url`` is the single entry point used by both the ``capture`` and
``batch`` CLI commands. It returns a result dict so callers can aggregate
outcomes without re-reading the manifest.
"""

from __future__ import annotations

import http.client
import json
import logging
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
import trafilatura

from . import __version__
from .manifest import (
    artifact_entry,
    build_manifest,
    sha256_text,
    utc_now_iso,
    write_manifest,
)
from .wacz import IndexEntry
from .wayback import save_to_wayback

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_OUTPUT_DIR = Path("./archive")

# HTML-capture artifacts.
WARC_NAME = "capture.warc.gz"
HEADERS_NAME = "headers.json"
SCREENSHOT_NAME = "screenshot.png"
ARTICLE_NAME = "article.txt"
MANIFEST_NAME = "manifest.json"

# Common MIME-type -> file-extension overrides. Used only for binary captures.
# ``mimetypes`` from the stdlib fills the gaps; these overrides guarantee
# predictable, human-friendly extensions for the types we see most often.
COMMON_BINARY_EXTENSIONS = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/octet-stream": "bin",
}

# Headers that describe the on-the-wire encoding of a body. ``requests`` and
# Playwright both hand us the *decoded* body, so storing these verbatim makes
# replay tools try to gunzip / de-chunk plain bytes and break the page. We keep
# the original values under an ``x-archive-orig-`` prefix (pywb convention).
_WIRE_ENCODING_HEADERS = ("content-encoding", "transfer-encoding", "content-length")
ORIG_HEADER_PREFIX = "x-archive-orig-"


def _user_agent() -> str:
    """Return the source-archive HTTP User-Agent string."""
    return (
        f"Mozilla/5.0 (compatible; source-archive/{__version__}; "
        "+https://github.com/climate/source-archive)"
    )


def classify_content_type(content_type: str | None) -> str:
    """Classify a Content-Type header into 'html' or 'binary'.

    HTML/XHTML and ``text/plain`` (kept for backward compatibility) are
    treated as HTML. Anything else, including a missing or unknown type, is
    treated as binary so that we never try to render non-HTML bytes.
    """
    if not content_type:
        return "binary"
    media_type = content_type.split(";")[0].strip().lower()
    if media_type in ("text/html", "application/xhtml+xml", "text/plain"):
        return "html"
    return "binary"


def _extension_for_content_type(content_type: str | None) -> str:
    """Return a short file extension for ``content_type`` (without the dot).

    Uses an explicit mapping for common types, then falls back to the stdlib
    ``mimetypes`` module. Unknown or unparseable types return ``'bin'``.
    """
    if not content_type:
        return "bin"
    media_type = content_type.split(";")[0].strip().lower()
    if media_type in COMMON_BINARY_EXTENSIONS:
        return COMMON_BINARY_EXTENSIONS[media_type]
    ext = mimetypes.guess_extension(media_type)
    if ext:
        return ext.lstrip(".")
    return "bin"


def _raw_binary_name(content_type: str | None) -> str:
    """Return ``raw.<ext>`` for the given binary content type."""
    return f"raw.{_extension_for_content_type(content_type)}"


def normalize_output_dir(output_dir: Path) -> Path:
    """Return the effective archive root for ``output_dir``.

    If the final path component is literally ``captures``, treat the parent
    directory as the archive root.  This prevents accidental double nesting
    when users pass ``--output-dir .../archive/captures`` (the old documented
    example): captures then land in ``<parent>/captures/<hash>`` and the index
    lives at ``<parent>/index.db``.
    """
    if output_dir.name == "captures":
        return output_dir.parent
    return output_dir


def capture_dir_for(output_dir: Path, url: str) -> Path:
    """Return ``<output_dir>/captures/<sha256(url)[:16]>`` for a URL."""
    prefix = sha256_text(url)[:16]
    return output_dir / "captures" / prefix


def _status_reason(status: int) -> str:
    """Return the HTTP reason phrase for ``status``, or a fallback."""
    return http.client.responses.get(status, "Unknown")


def _fetch(url: str, timeout: int) -> tuple[int, str | None, dict[str, str], bytes]:
    """Fetch raw response body + headers with requests. Raises on network failure."""
    resp = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": _user_agent()},
    )
    content_type = resp.headers.get("Content-Type")
    # ``requests`` headers are case-insensitive; convert to a plain dict of
    # lower-cased keys for deterministic manifest storage.
    headers = {k.lower(): v for k, v in resp.headers.items()}
    return resp.status_code, content_type, headers, resp.content


def _classify(
    url: str, timeout: int
) -> tuple[str | None, str | None, dict[str, str], int | None, bytes | None]:
    """Classify a URL as HTML or binary, with optional body fetch.

    First tries a lightweight HEAD request. If that succeeds, the returned
    body is ``None`` and the caller can fetch the full body later. If HEAD
    fails or the server does not advertise a usable Content-Type, falls back
    to a full GET so we can inspect the actual response.
    """
    try:
        resp = requests.head(
            url, timeout=timeout, headers={"User-Agent": _user_agent()}
        )
        content_type = resp.headers.get("Content-Type")
        headers = {k.lower(): v for k, v in resp.headers.items()}
        category = classify_content_type(content_type)
        return category, content_type, headers, resp.status_code, None
    except requests.RequestException as exc:
        log.debug("HEAD classification failed for %s: %s", url, exc)

    # HEAD failed — fall back to a full GET so we can still classify.
    try:
        status, content_type, headers, body = _fetch(url, timeout)
    except requests.RequestException:
        raise
    category = classify_content_type(content_type)
    return category, content_type, headers, status, body


def replay_safe_headers(
    headers: dict[str, str] | list[tuple[str, str]], body_length: int
) -> list[tuple[str, str]]:
    """Make stored HTTP headers consistent with an already-decoded body.

    Wire-encoding headers are renamed to ``x-archive-orig-<name>`` and a
    ``content-length`` matching the stored body is added.
    """
    items = headers.items() if isinstance(headers, dict) else headers
    result: list[tuple[str, str]] = []
    for name, value in items:
        if name.lower() in _WIRE_ENCODING_HEADERS:
            result.append((ORIG_HEADER_PREFIX + name.lower(), value))
        else:
            result.append((name, value))
    result.append(("content-length", str(body_length)))
    return result


def _write_warc(
    dest: Path,
    url: str,
    responses: list[dict[str, Any]],
    metadata: dict[str, str],
) -> list[IndexEntry]:
    """Write a compressed WARC file with a metadata record + response records.

    ``responses`` is a list of dicts with keys ``url``, ``status``, ``headers``,
    ``body`` and optionally ``fetched_at`` (ISO-8601), which becomes the
    record's ``WARC-Date``. ``metadata`` is written as a ``metadata`` record
    using the ``application/warc-fields`` content type.

    Returns one :class:`IndexEntry` per response record, for CDXJ indexing.
    """
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    index: list[IndexEntry] = []
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        writer = WARCWriter(out, gzip=True)

        # Metadata record: plain ``application/warc-fields``, no HTTP envelope.
        meta_body = "".join(
            f"{key}: {value}\n" for key, value in metadata.items()
        ).encode("utf-8")
        meta_record = writer.create_warc_record(
            url,
            "metadata",
            payload=BytesIO(meta_body),
            length=len(meta_body),
            warc_content_type="application/warc-fields",
        )
        writer.write_record(meta_record)

        # Response records for every intercepted HTTP response.
        for item in responses:
            status = item["status"]
            reason = _status_reason(status)
            status_line = f"HTTP/1.1 {status} {reason}"
            body = item.get("body") or b""
            header_list = replay_safe_headers(item.get("headers", {}), len(body))
            http_headers = StatusAndHeaders(status_line, header_list)
            warc_date = item.get("fetched_at") or utc_now_iso()
            record = writer.create_warc_record(
                item["url"],
                "response",
                payload=BytesIO(body),
                length=len(body),
                http_headers=http_headers,
                warc_headers_dict={"WARC-Date": warc_date},
            )
            offset = out.tell()
            writer.write_record(record)
            content_type = http_headers.get_header("content-type") or ""
            index.append(
                IndexEntry(
                    url=item["url"],
                    warc_date=warc_date,
                    status=status,
                    mime=content_type.split(";")[0].strip(),
                    digest=record.rec_headers.get_header("WARC-Payload-Digest") or "",
                    offset=offset,
                    length=out.tell() - offset,
                    filename=dest.name,
                )
            )

    return index


def _write_headers_json(dest_dir: Path, headers: dict[str, str]) -> Path:
    """Write the main response headers to ``headers.json``."""
    headers_path = dest_dir / HEADERS_NAME
    headers_path.write_text(
        json.dumps(headers, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return headers_path


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


# Scroll through the page in viewport-sized steps so lazy-loaded content
# (IntersectionObserver, loading="lazy") is requested. Capped for endless feeds.
_AUTOSCROLL_JS = """
async ({ maxSteps, pauseMs }) => {
  const step = Math.max(window.innerHeight * 0.8, 400);
  for (let i = 0, y = 0; i < maxSteps; i++, y += step) {
    if (y > document.documentElement.scrollHeight) break;
    window.scrollTo(0, y);
    await new Promise((r) => setTimeout(r, pauseMs));
  }
  window.scrollTo(0, 0);
}
"""

# Request every candidate of every srcset (and lazy data-src/data-srcset), plus
# icon and preload links, so
# replay finds whichever image size the *viewer's* browser picks. Candidates
# are split on ", " since CDN URLs (e.g. Substack's) contain bare commas.
_FETCH_IMAGE_VARIANTS_JS = """
async ({ maxUrls }) => {
  const urls = new Set();
  const add = (u) => {
    try {
      const abs = new URL(u, document.baseURI);
      if (abs.protocol === "http:" || abs.protocol === "https:") urls.add(abs.href);
    } catch (e) {}
  };
  for (const el of document.querySelectorAll("img, source")) {
    for (const attr of ["srcset", "data-srcset"]) {
      const value = el.getAttribute(attr);
      if (!value) continue;
      for (const candidate of value.split(/,\\s+/)) {
        const u = candidate.trim().split(/\\s+/)[0];
        if (u) add(u.replace(/,$/, ""));
      }
    }
    for (const attr of ["src", "data-src"]) {
      const value = el.getAttribute(attr);
      if (value && !value.startsWith("data:")) add(value);
    }
  }
  // Icons and preloads: a headless browser skips them, a viewer's doesn't.
  for (const link of document.querySelectorAll(
    'link[rel~="icon"], link[rel="apple-touch-icon"], link[rel="preload"]'
  )) {
    const href = link.getAttribute("href");
    if (href) add(href);
    const srcset = link.getAttribute("imagesrcset");
    if (srcset) for (const c of srcset.split(/,\\s+/)) add(c.trim().split(/\\s+/)[0]);
  }
  const list = [...urls].slice(0, maxUrls);
  await Promise.allSettled(
    list.map((u) => fetch(u, { mode: "no-cors", credentials: "include" }))
  );
  return list.length;
}
"""

AUTOSCROLL_MAX_STEPS = 60
AUTOSCROLL_PAUSE_MS = 250
MAX_IMAGE_VARIANTS = 400
SETTLE_TIMEOUT_MS = 15_000


SETTLE_QUIET_MS = 1_500
SETTLE_POLL_MS = 250


def _wait_until_quiet(page, responses: list[dict[str, Any]]) -> None:
    """Wait until no new response has been recorded for ``SETTLE_QUIET_MS``.

    ``wait_for_load_state("networkidle")`` returns at once if the page was
    idle before, and response handlers only run while Playwright is waiting,
    so we poll: each ``wait_for_timeout`` lets queued handlers read bodies.
    Gives up after ``SETTLE_TIMEOUT_MS`` for pages that never go quiet.
    """
    waited = quiet = 0
    last_count = len(responses)
    while waited < SETTLE_TIMEOUT_MS and quiet < SETTLE_QUIET_MS:
        page.wait_for_timeout(SETTLE_POLL_MS)
        waited += SETTLE_POLL_MS
        if len(responses) == last_count:
            quiet += SETTLE_POLL_MS
        else:
            last_count, quiet = len(responses), 0


def _load_deferred_content(page, responses: list[dict[str, Any]]) -> None:
    """Trigger lazy loading and fetch all responsive image variants.

    Best effort: a page that breaks either step is still captured as-is.
    """
    try:
        page.evaluate(
            _AUTOSCROLL_JS,
            {"maxSteps": AUTOSCROLL_MAX_STEPS, "pauseMs": AUTOSCROLL_PAUSE_MS},
        )
        # Content appended by scrolling (related posts, avatars) needs to land
        # in the DOM before its images can be collected.
        _wait_until_quiet(page, responses)
        fetched = page.evaluate(_FETCH_IMAGE_VARIANTS_JS, {"maxUrls": MAX_IMAGE_VARIANTS})
        log.debug("Requested %s image variants for %s", fetched, page.url)
    except Exception as exc:  # noqa: BLE001
        log.warning("Loading deferred content failed for %s: %s", page.url, exc)
    _wait_until_quiet(page, responses)


def _render_and_capture(
    url: str,
    dest_dir: Path,
    timeout: int,
    *,
    screenshot: bool = True,
) -> dict[str, Any] | None:
    """Render the page with headless Chromium and capture network traffic.

    Intercepts every HTTP response during ``page.goto()`` and returns a dict
    with:

        - ``screenshot``: Path to screenshot.png (``None`` if ``screenshot=False``)
        - ``responses``: list of intercepted response dicts (incl. redirects)
        - ``main_url``: final URL of the main navigation, after redirects
        - ``title``: the rendered page's ``<title>``, or ``None``
        - ``main_response_status``: status code of the main navigation
        - ``main_response_headers``: response headers of the main navigation
        - ``main_content_type``: Content-Type of the main navigation
        - ``html``: rendered DOM HTML from ``page.content()``

    Returns ``None`` if Playwright cannot launch or the page fails to load.
    """
    from playwright.sync_api import sync_playwright

    responses: list[dict[str, Any]] = []
    captured_ok: set[str] = set()
    screenshot_path = dest_dir / SCREENSHOT_NAME if screenshot else None

    def _handle_response(response) -> None:
        # blob:/data: URLs are browser-internal and cannot be replayed.
        if not response.url.startswith(("http://", "https://")):
            return
        # Re-requests (e.g. image variants already on the page) add nothing.
        if response.url in captured_ok:
            return
        fetched_at = utc_now_iso()
        if 300 <= response.status < 400:
            # Redirects have no body, but keeping them lets a cited URL that
            # redirects still resolve to its target during replay.
            body = b""
        else:
            try:
                body = response.body()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Failed to read response body for %s: %s", response.url, exc
                )
                return
        responses.append(
            {
                "url": response.url,
                "status": response.status,
                "headers": dict(response.headers),
                "body": body,
                "fetched_at": fetched_at,
            }
        )
        if 200 <= response.status < 300:
            captured_ok.add(response.url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.on("response", _handle_response)
                main_response = page.goto(
                    url, timeout=timeout * 1000, wait_until="networkidle"
                )

                if main_response is None:
                    log.warning(
                        "Playwright navigation returned no response for %s", url
                    )
                    return None

                _load_deferred_content(page, responses)

                if screenshot_path is not None:
                    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshot_path), full_page=True)
                html = page.content()
                main_response_headers = {
                    k.lower(): v for k, v in main_response.headers.items()
                }

                return {
                    "screenshot": screenshot_path,
                    "responses": responses,
                    "main_url": main_response.url,
                    "title": page.title() or None,
                    "main_response_status": main_response.status,
                    "main_response_headers": main_response_headers,
                    "main_content_type": main_response_headers.get("content-type"),
                    "html": html,
                }
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — broad on purpose, fall back.
        log.warning(
            "Playwright render failed for %s: %s — falling back to requests-only.",
            url,
            exc,
        )
        # Clean up partial screenshot.
        if screenshot_path is not None and screenshot_path.exists():
            try:
                screenshot_path.unlink()
            except OSError:
                pass
        return None


def _requests_only_capture(
    url: str,
    dest_dir: Path,
    status: int,
    headers: dict[str, str],
    content_type: str | None,
    body: bytes,
    captured_at: str,
) -> dict[str, dict[str, Any]]:
    """Build artifacts for the requests-only fallback path (HTML only).

    Writes a minimal WARC, ``headers.json``, and ``article.txt`` when possible.
    Returns the artifacts descriptor dict.
    """
    artifacts_desc: dict[str, dict[str, Any]] = {}

    _write_headers_json(dest_dir, headers)
    artifacts_desc["headers"] = artifact_entry(HEADERS_NAME, dest_dir / HEADERS_NAME)

    _write_warc(
        dest_dir / WARC_NAME,
        url,
        [
            {
                "url": url,
                "status": status,
                "headers": headers,
                "body": body,
            }
        ],
        {
            "software": f"source-archive/{__version__}",
            "format": "WARC File Format 1.0",
            "captured_at": captured_at,
        },
    )
    artifacts_desc["warc"] = artifact_entry(WARC_NAME, dest_dir / WARC_NAME)

    try:
        raw_html_text = body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        raw_html_text = ""
    article_path = _extract_article(raw_html_text, url, dest_dir)
    if article_path is not None:
        artifacts_desc["article_text"] = artifact_entry(ARTICLE_NAME, article_path)

    return artifacts_desc


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

    # --- 1. Classify URL before choosing capture path ----------------------------
    try:
        category, content_type, response_headers, status, raw_bytes = _classify(
            url, timeout
        )
    except requests.RequestException as exc:
        result["error"] = f"fetch failed: {exc}"
        log.error("Capture failed for %s: %s", url, exc)
        return result

    if category is None:
        result["error"] = "fetch failed: unable to classify URL"
        log.error("Capture failed for %s: unable to classify URL", url)
        return result

    artifacts_desc: dict[str, dict[str, Any]] = {}
    wayback_url: str | None = None

    if category == "html":
        # --- HTML path: Playwright render + WARC + screenshot + article text -----
        rendered = _render_and_capture(url, capture_dir, timeout)
        if rendered:
            status = rendered["main_response_status"]
            response_headers = rendered["main_response_headers"]
            content_type = rendered["main_content_type"]

            _write_headers_json(capture_dir, response_headers)
            artifacts_desc["headers"] = artifact_entry(
                HEADERS_NAME, capture_dir / HEADERS_NAME
            )

            _write_warc(
                capture_dir / WARC_NAME,
                url,
                rendered["responses"],
                {
                    "software": f"source-archive/{__version__}",
                    "format": "WARC File Format 1.0",
                    "captured_at": captured_at,
                },
            )
            artifacts_desc["warc"] = artifact_entry(
                WARC_NAME, capture_dir / WARC_NAME
            )
            artifacts_desc["screenshot"] = artifact_entry(
                SCREENSHOT_NAME, rendered["screenshot"]
            )

            article_path = _extract_article(rendered["html"], url, capture_dir)
            if article_path is not None:
                artifacts_desc["article_text"] = artifact_entry(
                    ARTICLE_NAME, article_path
                )
        else:
            # Fallback: requests-only minimal WARC + article text + headers.
            log.warning(
                "Playwright failed for %s — using requests-only fallback.", url
            )
            if raw_bytes is None:
                try:
                    status, content_type, response_headers, raw_bytes = _fetch(
                        url, timeout
                    )
                except requests.RequestException as exc:
                    result["error"] = f"fetch failed: {exc}"
                    log.error("Capture failed for %s: %s", url, exc)
                    return result
                category = classify_content_type(content_type)

            if category == "html":
                artifacts_desc = _requests_only_capture(
                    url,
                    capture_dir,
                    status,
                    response_headers,
                    content_type,
                    raw_bytes,
                    captured_at,
                )
            else:
                # HEAD said HTML but the GET body was binary. Treat as binary.
                _write_headers_json(capture_dir, response_headers)
                artifacts_desc["headers"] = artifact_entry(
                    HEADERS_NAME, capture_dir / HEADERS_NAME
                )
                raw_binary_name = _raw_binary_name(content_type)
                raw_binary_path = capture_dir / raw_binary_name
                raw_binary_path.write_bytes(raw_bytes)
                artifacts_desc["raw_binary"] = artifact_entry(
                    raw_binary_name, raw_binary_path
                )
                _write_warc(
                    capture_dir / WARC_NAME,
                    url,
                    [
                        {
                            "url": url,
                            "status": status,
                            "headers": response_headers,
                            "body": raw_bytes,
                        }
                    ],
                    {
                        "software": f"source-archive/{__version__}",
                        "format": "WARC File Format 1.0",
                        "captured_at": captured_at,
                    },
                )
                artifacts_desc["warc"] = artifact_entry(
                    WARC_NAME, capture_dir / WARC_NAME
                )
    else:
        # --- Binary path: save raw bytes with an appropriate extension ----------
        if raw_bytes is None:
            try:
                status, content_type, response_headers, raw_bytes = _fetch(
                    url, timeout
                )
            except requests.RequestException as exc:
                result["error"] = f"fetch failed: {exc}"
                log.error("Capture failed for %s: %s", url, exc)
                return result

        _write_headers_json(capture_dir, response_headers)
        artifacts_desc["headers"] = artifact_entry(
            HEADERS_NAME, capture_dir / HEADERS_NAME
        )

        raw_binary_name = _raw_binary_name(content_type)
        raw_binary_path = capture_dir / raw_binary_name
        raw_binary_path.write_bytes(raw_bytes)
        artifacts_desc["raw_binary"] = artifact_entry(
            raw_binary_name, raw_binary_path
        )

        # Write a WARC record for the binary response so every capture
        # produces a sealed, replayable archive — not just HTML pages.
        _write_warc(
            capture_dir / WARC_NAME,
            url,
            [
                {
                    "url": url,
                    "status": status,
                    "headers": response_headers,
                    "body": raw_bytes,
                }
            ],
            {
                "software": f"source-archive/{__version__}",
                "format": "WARC File Format 1.0",
                "captured_at": captured_at,
            },
        )
        artifacts_desc["warc"] = artifact_entry(WARC_NAME, capture_dir / WARC_NAME)

    # --- 2. Optional Wayback submission ----------------------------------------
    if wayback:
        wayback_url = save_to_wayback(url)
        if wayback_url is None:
            log.warning("Wayback capture skipped/failed for %s", url)

    # --- 3. Build manifest with artifact descriptors ---------------------------
    manifest = build_manifest(
        url=url,
        captured_at=captured_at,
        http_status=status,
        content_type=content_type,
        content_category=category,
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

    # --- 4. Insert into SQLite index -------------------------------------------
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
