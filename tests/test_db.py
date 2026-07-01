"""Tests for source_archive.db."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from source_archive.db import (
    count_artifacts,
    get_db_path,
    init_db,
    insert_capture,
    list_captures,
    load_manifest,
    lookup_capture,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = get_db_path(tmp_path)
    init_db(p)
    return p


def _insert(db_path, url, captured_at="2026-07-01T12:00:00Z", status=200, wayback=None):
    return insert_capture(
        db_path,
        url=url,
        capture_hash="abc123",
        captured_at=captured_at,
        manifest_path=f"/archive/captures/abc123/manifest.json",
        artifacts_dir="/archive/captures/abc123",
        http_status=status,
        wayback_url=wayback,
    )


def test_init_db_creates_schema(db_path: Path) -> None:
    assert db_path.exists()
    # init_db is idempotent.
    init_db(db_path)


def test_insert_and_lookup(db_path: Path) -> None:
    _insert(db_path, "https://example.com/a")
    row = lookup_capture(db_path, "https://example.com/a")
    assert row is not None
    assert row["url"] == "https://example.com/a"
    assert row["http_status"] == 200


def test_lookup_missing_returns_none(db_path: Path) -> None:
    assert lookup_capture(db_path, "https://nope.example") is None


def test_list_captures_filter(db_path: Path) -> None:
    _insert(db_path, "https://example.com/climate", captured_at="2026-07-01T10:00:00Z")
    _insert(db_path, "https://other.com/news", captured_at="2026-07-01T11:00:00Z")
    _insert(db_path, "https://example.com/weather", captured_at="2026-07-01T12:00:00Z")

    all_rows = list_captures(db_path)
    assert len(all_rows) == 3
    # Most recent first.
    assert all_rows[0]["captured_at"] == "2026-07-01T12:00:00Z"

    filtered = list_captures(db_path, url_filter="example.com")
    assert len(filtered) == 2
    assert all("example.com" in r["url"] for r in filtered)


def test_lookup_returns_most_recent(db_path: Path) -> None:
    _insert(db_path, "https://example.com/x", captured_at="2026-07-01T10:00:00Z")
    _insert(db_path, "https://example.com/x", captured_at="2026-07-01T12:00:00Z")
    row = lookup_capture(db_path, "https://example.com/x")
    assert row["captured_at"] == "2026-07-01T12:00:00Z"


def test_load_manifest_and_count(tmp_path: Path) -> None:
    m = {
        "url": "https://example.com/a",
        "artifacts": {"raw_html": {}, "pdf": {}, "article_text": {}},
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m))
    loaded = load_manifest(p)
    assert loaded is not None
    assert count_artifacts(loaded) == 3
    assert load_manifest(tmp_path / "nope.json") is None