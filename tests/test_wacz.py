"""Tests for source_archive.wacz packaging."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

from warcio.archiveiterator import ArchiveIterator

from source_archive.capture import _write_warc
from source_archive.wacz import Page, build_cdxj, surt, wayback_timestamp, write_wacz


def test_surt() -> None:
    assert surt("https://www.Example.com/A/b?z=1&a=2") == "com,example)/a/b?a=2&z=1"
    assert surt("http://example.com") == "com,example)/"
    assert surt("http://example.com:8080/x") == "com,example:8080)/x"


def test_wayback_timestamp() -> None:
    assert wayback_timestamp("2026-09-29T17:44:02Z") == "20260929174402"


def test_index_offsets_point_at_records(tmp_path: Path) -> None:
    warc = tmp_path / "bundle.warc.gz"
    index = _write_warc(
        warc,
        "https://example.com/",
        [
            {"url": "https://example.com/", "status": 200,
             "headers": {"content-type": "text/html"}, "body": b"<html>a</html>",
             "fetched_at": "2026-09-29T17:44:02Z"},
            {"url": "https://example.com/s.css", "status": 200,
             "headers": {"content-type": "text/css; charset=utf-8"}, "body": b"b{}"},
        ],
        {"software": "test"},
    )

    assert [e.url for e in index] == ["https://example.com/", "https://example.com/s.css"]
    assert index[0].warc_date == "2026-09-29T17:44:02Z"
    assert index[1].mime == "text/css"
    data = warc.read_bytes()
    for entry in index:
        chunk = data[entry.offset : entry.offset + entry.length]
        (record,) = list(ArchiveIterator(BytesIO(chunk)))
        assert record.rec_headers.get_header("WARC-Target-URI") == entry.url
        assert record.rec_headers.get_header("WARC-Date") == entry.warc_date


def test_build_cdxj_skips_non_http_and_sorts(tmp_path: Path) -> None:
    warc = tmp_path / "bundle.warc.gz"
    index = _write_warc(
        warc,
        "https://b.com/",
        [
            {"url": "https://b.com/", "status": 200, "headers": {}, "body": b"b"},
            {"url": "/local/file.html", "status": 200, "headers": {}, "body": b"x"},
            {"url": "https://a.com/", "status": 200, "headers": {}, "body": b"a"},
        ],
        {"software": "test"},
    )
    lines = build_cdxj(index).splitlines()
    assert [line.split(" ")[0] for line in lines] == ["com,a)/", "com,b)/"]
    assert json.loads(lines[0].split(" ", 2)[2])["filename"] == "bundle.warc.gz"


def test_write_wacz_layout(tmp_path: Path) -> None:
    warc = tmp_path / "bundle.warc.gz"
    index = _write_warc(
        warc,
        "https://a.com/",
        [{"url": "https://a.com/", "status": 200, "headers": {}, "body": b"a",
          "fetched_at": "2026-09-29T17:44:02Z"}],
        {"software": "test"},
    )
    page = Page(url="https://a.com/", ts="2026-09-29T17:44:02Z", title="A")
    dest = write_wacz(tmp_path / "out.wacz", warc, index, [page],
                      title="T", created="2026-09-29T17:50:00Z", software="test",
                      main_page=page)

    with zipfile.ZipFile(dest) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        assert set(infos) == {
            "pages/pages.jsonl", "indexes/index.cdx", "archive/bundle.warc.gz",
            "datapackage.json", "datapackage-digest.json",
        }
        # Stored, not deflated: replay seeks into the WARC by offset.
        assert all(i.compress_type == zipfile.ZIP_STORED for i in infos.values())
        assert zf.read("archive/bundle.warc.gz") == warc.read_bytes()
        datapackage = json.loads(zf.read("datapackage.json"))
    assert datapackage["wacz_version"] == "1.1.1"
    assert datapackage["mainPageDate"] == "2026-09-29T17:44:02Z"
