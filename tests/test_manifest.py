"""Tests for source_archive.manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from source_archive.manifest import (
    artifact_entry,
    build_manifest,
    load_manifest,
    sha256_file,
    sha256_text,
    utc_now_iso,
    write_manifest,
)


def test_utc_now_iso_format() -> None:
    ts = utc_now_iso()
    assert ts.endswith("Z")
    # YYYY-MM-DDTHH:MM:SSZ — 20 chars.
    assert len(ts) == 20
    assert ts[4] == "-" and ts[7] == "-" and ts[10] == "T" and ts[-1] == "Z"


def test_sha256_text_known_value() -> None:
    # "abc" -> ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad
    assert sha256_text("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_sha256_file(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"abc")
    assert sha256_file(f) == sha256_text("abc")


def test_artifact_entry(tmp_path: Path) -> None:
    f = tmp_path / "raw.html"
    f.write_bytes(b"<html></html>")
    entry = artifact_entry("raw.html", f)
    assert entry["path"] == "raw.html"
    assert entry["size"] == 13
    assert len(entry["sha256"]) == 64


def test_build_manifest_shape() -> None:
    m = build_manifest(
        url="https://example.com/x",
        captured_at="2026-07-01T12:00:00Z",
        http_status=200,
        content_type="text/html",
        response_headers={"server": "nginx"},
        artifacts={"raw_html": {"path": "raw.html", "sha256": "h" * 64, "size": 10}},
        wayback_url=None,
    )
    assert m["url"] == "https://example.com/x"
    assert m["wayback_url"] is None
    assert m["artifacts"]["raw_html"]["size"] == 10


def test_write_and_load_manifest_roundtrip(tmp_path: Path) -> None:
    m = build_manifest(
        url="https://example.com/y",
        captured_at="2026-07-01T12:00:00Z",
        http_status=200,
        content_type="text/html",
        response_headers={"server": "nginx"},
        artifacts={},
        wayback_url="https://web.archive.org/web/123/https://example.com/y",
    )
    dest = tmp_path / "manifest.json"
    write_manifest(m, dest)
    assert dest.exists()
    loaded = load_manifest(dest)
    assert loaded is not None
    assert loaded["url"] == m["url"]
    assert loaded["wayback_url"] == m["wayback_url"]
    # load_manifest on missing path returns None.
    assert load_manifest(tmp_path / "nope.json") is None