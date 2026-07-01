"""Command-line interface for source-archive.

Commands::

    source-archive capture <url> [--wayback] [--output-dir DIR]
    source-archive batch   <file> [--wayback] [--output-dir DIR]
    source-archive list    [--url URL]
    source-archive lookup  <url>
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import click

from . import __version__
from .capture import capture_url, capture_urls
from .db import count_artifacts, get_db_path, list_captures, load_manifest, lookup_capture

DEFAULT_OUTPUT_DIR = "./archive"


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@click.group()
@click.version_option(__version__, prog_name="source-archive")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """source-archive: capture and preserve web content at citation time."""
    _setup_logging(verbose)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------
@cli.command()
@click.argument("url")
@click.option("--wayback", is_flag=True, help="Also submit to the Internet Archive Wayback Machine.")
@click.option("--output-dir", default=DEFAULT_OUTPUT_DIR, show_default=True, help="Directory for captures.")
@click.option("--timeout", default=30, show_default=True, help="HTTP/Playwright timeout in seconds.")
def capture(url: str, wayback: bool, output_dir: str, timeout: int) -> None:
    """Capture a single URL into durable local formats."""
    out = Path(output_dir)
    click.echo(f"Capturing {url} → {out}/ ...")
    result = capture_url(url, output_dir=out, wayback=wayback, timeout=timeout)
    if result["ok"]:
        click.secho("OK", fg="green", bold=True)
        click.echo(f"  manifest:  {result['manifest_path']}")
        click.echo(f"  artifacts: {result['artifacts_dir']}")
        if result.get("wayback_url"):
            click.echo(f"  wayback:   {result['wayback_url']}")
    else:
        click.secho("FAILED", fg="red", bold=True)
        click.echo(f"  error: {result['error']}")
        sys.exit(1)


# --------------------------------------------------------------------------
# batch
# --------------------------------------------------------------------------
@cli.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--wayback", is_flag=True, help="Also submit each URL to the Wayback Machine.")
@click.option("--output-dir", default=DEFAULT_OUTPUT_DIR, show_default=True, help="Directory for captures.")
@click.option("--delay", default=2.0, show_default=True, help="Seconds to wait between captures.")
@click.option("--timeout", default=30, show_default=True, help="HTTP/Playwright timeout in seconds.")
def batch(file: Path, wayback: bool, output_dir: str, delay: float, timeout: int) -> None:
    """Capture every URL listed in a text file (one per line; # = comment)."""
    urls: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)

    if not urls:
        click.echo("No URLs found in file.")
        return

    click.echo(f"Capturing {len(urls)} URLs from {file} ...")
    results = capture_urls(
        urls,
        delay=delay,
        output_dir=Path(output_dir),
        wayback=wayback,
        timeout=timeout,
    )

    succeeded = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    click.echo("")
    click.secho(f"Summary: {len(results)} total — {len(succeeded)} succeeded, {len(failed)} failed", bold=True)
    if succeeded:
        click.secho("Succeeded:", fg="green")
        for r in succeeded:
            click.echo(f"  {r['url']}")
    if failed:
        click.secho("Failed:", fg="red")
        for r in failed:
            click.echo(f"  {r['url']} — {r['error']}")

    if failed:
        sys.exit(1)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------
@cli.command(name="list")
@click.option("--url", default=None, help="Filter captures by URL substring.")
@click.option("--output-dir", default=DEFAULT_OUTPUT_DIR, show_default=True, help="Directory containing index.db.")
def list_cmd(url: str | None, output_dir: str) -> None:
    """List all captures in the local index."""
    db_path = get_db_path(Path(output_dir))
    if not db_path.exists():
        click.echo("No captures found (index.db missing).")
        return
    rows = list_captures(db_path, url_filter=url)
    if not rows:
        click.echo("No captures found.")
        return
    click.echo(f"{'URL':<50} {'Captured':<22} {'Artifacts':<10} {'Status'}")
    click.echo("-" * 95)
    for row in rows:
        manifest = load_manifest(row["manifest_path"])
        n = count_artifacts(manifest) if manifest else "?"
        click.echo(
            f"{row['url'][:50]:<50} {row['captured_at']:<22} {str(n):<10} {row.get('http_status') or '-'}"
        )


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------
@cli.command()
@click.argument("url")
@click.option("--output-dir", default=DEFAULT_OUTPUT_DIR, show_default=True, help="Directory containing index.db.")
def lookup(url: str, output_dir: str) -> None:
    """Show the full manifest for the most recent capture of URL."""
    db_path = get_db_path(Path(output_dir))
    if not db_path.exists():
        click.echo("No captures found (index.db missing).")
        return
    row = lookup_capture(db_path, url)
    if not row:
        click.echo(f"No capture found for {url}")
        sys.exit(1)
    manifest = load_manifest(row["manifest_path"])
    if manifest is None:
        click.echo(f"Manifest file missing: {row['manifest_path']}")
        sys.exit(1)
    click.echo(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    cli()