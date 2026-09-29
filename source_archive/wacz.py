"""Package a WARC into a WACZ (Web Archive Collection Zipped) file.

A WACZ is a plain zip that ReplayWeb.page and pywb open directly. On top of
the WARC it carries a CDXJ index (so replay can seek into the WARC without
reading all of it), a ``pages.jsonl`` page list (what the replay UI shows as
"Pages"), and a ``datapackage.json`` manifest with a SHA-256 for every file.

Spec: https://specs.webrecorder.net/wacz/1.1.1/
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

WACZ_VERSION = "1.1.1"
PAGES_PATH = "pages/pages.jsonl"
INDEX_PATH = "indexes/index.cdx"
ARCHIVE_DIR = "archive"


@dataclass(frozen=True)
class IndexEntry:
    """Location and summary of one ``response`` record inside a WARC file."""

    url: str
    warc_date: str  # ISO-8601, e.g. 2026-09-29T17:44:02Z
    status: int
    mime: str
    digest: str
    offset: int
    length: int
    filename: str


@dataclass(frozen=True)
class Page:
    """One entry of the replay UI's page list."""

    url: str
    ts: str  # must equal the WARC-Date of the page's response record
    title: str | None = None


def wayback_timestamp(iso: str) -> str:
    """``2026-09-29T17:44:02Z`` -> ``20260929174402``."""
    return "".join(ch for ch in iso if ch.isdigit())[:14]


def surt(url: str) -> str:
    """Sort-friendly URI Reordering Transform, as used for CDXJ keys.

    ``https://www.Example.com/a?b=2&a=1`` -> ``com,example)/a?a=1&b=2``
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    key = ",".join(reversed(host.split("."))) if host else ""
    if parts.port and parts.port not in (80, 443):
        key += f":{parts.port}"
    key += ")" + (parts.path or "/").lower()
    if parts.query:
        key += "?" + "&".join(sorted(parts.query.lower().split("&")))
    return key


def _is_replayable(url: str) -> bool:
    return urlsplit(url).scheme in ("http", "https")


def build_cdxj(entries: Iterable[IndexEntry]) -> str:
    """Render index entries as sorted CDXJ lines."""
    lines = []
    for e in entries:
        if not _is_replayable(e.url):
            continue
        fields = {
            "url": e.url,
            "mime": e.mime,
            "status": str(e.status),
            "digest": e.digest,
            "length": str(e.length),
            "offset": str(e.offset),
            "filename": e.filename,
        }
        lines.append(
            f"{surt(e.url)} {wayback_timestamp(e.warc_date)} {json.dumps(fields)}"
        )
    lines.sort()
    return "".join(line + "\n" for line in lines)


def build_pages_jsonl(pages: Iterable[Page]) -> str:
    """Render the page list, header line first, in the given order."""
    lines: list[dict[str, Any]] = [
        {"format": "json-pages-1.0", "id": "pages", "title": "All Pages"}
    ]
    for page in pages:
        entry: dict[str, Any] = {
            "id": hashlib.sha256(f"{page.url} {page.ts}".encode()).hexdigest()[:16],
            "url": page.url,
            "ts": page.ts,
        }
        if page.title:
            entry["title"] = page.title
        lines.append(entry)
    return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _resource(path: str, data: bytes | None = None, file: Path | None = None) -> dict[str, Any]:
    if file is not None:
        digest, size = _sha256_file(file), file.stat().st_size
    else:
        assert data is not None
        digest, size = hashlib.sha256(data).hexdigest(), len(data)
    return {
        "name": Path(path).name,
        "path": path,
        "hash": f"sha256:{digest}",
        "bytes": size,
    }


def write_wacz(
    dest: Path,
    warc_path: Path,
    index: list[IndexEntry],
    pages: list[Page],
    *,
    title: str,
    created: str,
    software: str,
    main_page: Page | None = None,
) -> Path:
    """Write ``dest`` as a WACZ containing ``warc_path`` plus index and pages.

    ``index`` entries must reference ``warc_path.name`` as their filename.
    Everything is stored uncompressed: the WARC is already gzipped per record,
    and replay tools need to seek into it by offset.
    """
    warc_arcname = f"{ARCHIVE_DIR}/{warc_path.name}"
    pages_bytes = build_pages_jsonl(pages).encode("utf-8")
    index_bytes = build_cdxj(index).encode("utf-8")

    datapackage: dict[str, Any] = {
        "profile": "data-package",
        "wacz_version": WACZ_VERSION,
        "title": title,
        "created": created,
        "software": software,
        "resources": [
            _resource(PAGES_PATH, data=pages_bytes),
            _resource(INDEX_PATH, data=index_bytes),
            _resource(warc_arcname, file=warc_path),
        ],
    }
    if main_page is not None:
        datapackage["mainPageURL"] = main_page.url
        datapackage["mainPageDate"] = main_page.ts
    datapackage_bytes = (json.dumps(datapackage, indent=2) + "\n").encode("utf-8")
    digest_bytes = (
        json.dumps(
            {
                "path": "datapackage.json",
                "hash": f"sha256:{hashlib.sha256(datapackage_bytes).hexdigest()}",
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")

    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(PAGES_PATH, pages_bytes)
        zf.writestr(INDEX_PATH, index_bytes)
        zf.write(warc_path, warc_arcname)
        zf.writestr("datapackage.json", datapackage_bytes)
        zf.writestr("datapackage-digest.json", digest_bytes)
    return dest
