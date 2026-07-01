"""Manifest creation and artifact hashing utilities.

A manifest is a JSON document describing everything captured for a URL: the
HTTP metadata, the list of artifacts (each with a relative path, SHA-256
hash, and byte size), and an optional Wayback URL.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string ending in ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    """Compute the SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def artifact_entry(rel_path: str, file_path: Path) -> dict[str, Any]:
    """Build a single artifact descriptor (path, sha256, size)."""
    return {
        "path": rel_path,
        "sha256": sha256_file(file_path),
        "size": file_path.stat().st_size,
    }


def build_manifest(
    *,
    url: str,
    captured_at: str,
    http_status: int | None,
    content_type: str | None,
    response_headers: dict[str, str],
    artifacts: dict[str, dict[str, Any]],
    wayback_url: str | None = None,
) -> dict[str, Any]:
    """Assemble the manifest dictionary."""
    return {
        "url": url,
        "captured_at": captured_at,
        "http_status": http_status,
        "content_type": content_type,
        "response_headers": response_headers,
        "artifacts": artifacts,
        "wayback_url": wayback_url,
    }


def write_manifest(manifest: dict[str, Any], dest: Path) -> Path:
    """Write ``manifest`` to ``dest`` as pretty-printed JSON. Returns ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return dest


def load_manifest(path: Path) -> dict[str, Any] | None:
    """Load a manifest from ``path``; return None if the file does not exist."""
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)