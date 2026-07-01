"""SQLite schema and query helpers for the capture index.

A single database (``archive/index.db``) stores one row per capture so the
``list`` and ``lookup`` CLI commands can answer quickly without scanning the
filesystem.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL,
    capture_hash  TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    artifacts_dir TEXT NOT NULL,
    http_status   INTEGER,
    wayback_url   TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_captures_url ON captures(url);
CREATE INDEX IF NOT EXISTS idx_captures_captured_at ON captures(captured_at);
"""


def get_db_path(output_dir: Path) -> Path:
    """Return the path to the SQLite index file inside ``output_dir``."""
    return output_dir / "index.db"


def init_db(db_path: Path) -> None:
    """Create the schema if it does not already exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def insert_capture(
    db_path: Path,
    *,
    url: str,
    capture_hash: str,
    captured_at: str,
    manifest_path: str,
    artifacts_dir: str,
    http_status: int | None = None,
    wayback_url: str | None = None,
) -> int:
    """Insert a capture row and return its row id."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO captures
                (url, capture_hash, captured_at, manifest_path,
                 artifacts_dir, http_status, wayback_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (url, capture_hash, captured_at, manifest_path,
             artifacts_dir, http_status, wayback_url),
        )
        conn.commit()
        last = cur.lastrowid
        return int(last) if last is not None else -1


def list_captures(db_path: Path, url_filter: str | None = None) -> list[dict[str, Any]]:
    """Return all captures, optionally filtered by URL substring."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if url_filter:
            rows = conn.execute(
                "SELECT * FROM captures WHERE url LIKE ? ORDER BY captured_at DESC",
                (f"%{url_filter}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM captures ORDER BY captured_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def lookup_capture(db_path: Path, url: str) -> dict[str, Any] | None:
    """Return the most recent capture for ``url`` (exact match), or None."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM captures WHERE url = ? ORDER BY captured_at DESC LIMIT 1",
            (url,),
        ).fetchone()
        return dict(row) if row else None


def load_manifest(manifest_path: str) -> dict[str, Any] | None:
    """Load a manifest JSON file, returning None if it does not exist."""
    p = Path(manifest_path)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def count_artifacts(manifest: dict[str, Any]) -> int:
    """Return the number of artifacts recorded in a manifest."""
    return len(manifest.get("artifacts", {}))