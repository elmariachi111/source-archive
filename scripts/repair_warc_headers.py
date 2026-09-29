#!/usr/bin/env python3
"""Repair WARCs written before wire-encoding headers were neutralized.

Background: ``requests`` and Playwright return *decoded* response bodies, but
older versions of ``_write_warc`` stored the original ``Content-Encoding``,
``Transfer-Encoding`` and ``Content-Length`` headers alongside them. Replay
tools (ReplayWeb.page, pywb) then try to gunzip / de-chunk plain bytes and the
page fails to render. Older metadata records also carried a bogus ``200 OK``
HTTP envelope.

This script rewrites a WARC so its headers match the stored payloads. Payload
bytes, ``WARC-Record-ID``, ``WARC-Date`` and ``WARC-Target-URI`` are preserved;
block digests are recomputed. The original file is never modified — output
goes to ``<name>.repaired.warc.gz`` unless ``--output`` is given.
"""

from __future__ import annotations

import argparse
import sys
from io import BytesIO
from pathlib import Path

from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source_archive.capture import replay_safe_headers  # noqa: E402

PRESERVED_WARC_HEADERS = ("WARC-Record-ID", "WARC-Date", "WARC-Target-URI")
BOGUS_META_ENVELOPE = b"200 OK\r\n\r\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("warc", type=Path, help="WARC file to repair")
    parser.add_argument("-o", "--output", type=Path, help="Output path")
    return parser.parse_args(argv)


def default_output(src: Path) -> Path:
    name = src.name
    for suffix in (".warc.gz", ".warc"):
        if name.endswith(suffix):
            return src.with_name(name[: -len(suffix)] + ".repaired" + suffix)
    return src.with_name(name + ".repaired")


def repair(src: Path, dest: Path) -> dict[str, int]:
    stats = {"records": 0, "responses_fixed": 0, "metadata_fixed": 0}
    with src.open("rb") as inp, dest.open("wb") as out:
        writer = WARCWriter(out, gzip=dest.name.endswith(".gz"))
        for record in ArchiveIterator(inp):
            stats["records"] += 1
            payload = record.content_stream().read()
            warc_headers = {
                name: record.rec_headers.get_header(name)
                for name in PRESERVED_WARC_HEADERS
                if record.rec_headers.get_header(name)
            }
            uri = record.rec_headers.get_header("WARC-Target-URI")

            if record.rec_type == "response" and record.http_headers:
                old = record.http_headers
                new_headers = replay_safe_headers(old.headers, len(payload))
                if new_headers != old.headers:
                    stats["responses_fixed"] += 1
                new = writer.create_warc_record(
                    uri,
                    "response",
                    payload=BytesIO(payload),
                    length=len(payload),
                    http_headers=StatusAndHeaders(
                        old.statusline, new_headers, protocol=old.protocol
                    ),
                    warc_headers_dict=warc_headers,
                )
            elif record.rec_type == "metadata":
                if payload.startswith(BOGUS_META_ENVELOPE):
                    payload = payload[len(BOGUS_META_ENVELOPE):]
                    stats["metadata_fixed"] += 1
                if uri and b"article_url:" not in payload:
                    payload += f"article_url: {uri}\n".encode("utf-8")
                new = writer.create_warc_record(
                    uri,
                    "metadata",
                    payload=BytesIO(payload),
                    length=len(payload),
                    warc_content_type=record.rec_headers.get_header("Content-Type"),
                    warc_headers_dict=warc_headers,
                )
            else:
                # Anything we don't know how to fix is copied as-is.
                new = writer.create_warc_record(
                    uri,
                    record.rec_type,
                    payload=BytesIO(payload),
                    length=len(payload),
                    http_headers=record.http_headers,
                    warc_content_type=record.rec_headers.get_header("Content-Type"),
                    warc_headers_dict=warc_headers,
                )

            writer.write_record(new)
    return stats


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    src: Path = args.warc
    dest: Path = args.output or default_output(src)
    if dest.resolve() == src.resolve():
        print("Refusing to overwrite the input file; pick another --output.", file=sys.stderr)
        return 2
    stats = repair(src, dest)
    print(
        f"{src} -> {dest}: {stats['records']} records, "
        f"{stats['responses_fixed']} responses fixed, "
        f"{stats['metadata_fixed']} metadata records fixed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
