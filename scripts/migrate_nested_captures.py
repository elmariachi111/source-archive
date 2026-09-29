#!/usr/bin/env python3
"""Migrate captures stored under <archive_root>/captures/captures/ up to <archive_root>/captures/.

Background: earlier versions of the documentation suggested ``--output-dir
~/source-archive/archive/captures``, which caused captures to be written to
``archive/captures/captures/<hash>/`` and indexed in a second SQLite database at
``archive/captures/index.db``.  This script merges that nested index back into
``<archive_root>/index.db`` and moves each capture directory up one level.

The script is careful:

* It never deletes a capture directory or manifest.
* It backs up the nested ``index.db`` before removing it.
* It aborts rather than guessing when a manifest path does not match the
  expected nested layout.
* It normalizes pre-existing relative paths in the root database to absolute
  paths.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


EXPECTED_NESTED_PREFIX = "/captures/captures/"


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move nested captures up to the archive root and merge their index rows."
    )
    parser.add_argument(
        "--archive-root",
        default="/home/stadolf/source-archive/archive",
        help="Path to the archive root containing index.db and the captures/ directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned migration without changing any files or the database.",
    )
    return parser.parse_args(argv)


def detect_nested_state(root: Path) -> tuple[Path, Path, list[Path]]:
    """Return (nested_captures_dir, nested_index_db, list of hash_dirs)."""
    nested_root = root / "captures" / "captures"
    nested_index = root / "captures" / "index.db"
    hash_dirs: list[Path] = []
    if nested_root.exists():
        hash_dirs = sorted(
            [p for p in nested_root.iterdir() if p.is_dir()],
            key=lambda p: p.name,
        )
    return nested_root, nested_index, hash_dirs


def is_expected_nested_path(path: str, root: Path) -> bool:
    """Return True if path starts with <root>/captures/captures/<hash>."""
    prefix = str(root / "captures" / "captures") + "/"
    return path.startswith(prefix)


def rewrite_nested_path(path: str, root: Path) -> str:
    """Rewrite <root>/captures/captures/<hash>/... -> <root>/captures/<hash>/...."""
    old_dir = str(root / "captures" / "captures")
    if not path.startswith(old_dir + "/"):
        raise ValueError(f"path is not inside nested captures dir: {path}")
    relative = path[len(old_dir) + 1 :]  # e.g. "<hash>/manifest.json"
    return str(root / "captures" / relative)


def count_target_rows(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM captures").fetchone()
        return row[0] if row else 0


def load_nested_rows(db_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute("SELECT * FROM captures").fetchall())


def load_existing_pairs(db_path: Path) -> set[tuple[str, str]]:
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as conn:
        return set(
            conn.execute("SELECT url, capture_hash FROM captures").fetchall()
        )


def ensure_schema(db_path: Path) -> None:
    """Create the captures table/indexes at db_path if they do not exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = """
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
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)
        conn.commit()


def normalize_relative_paths(db_path: Path, repo_root: Path) -> int:
    """Convert relative manifest_path/artifacts_dir values to absolute paths."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute("SELECT id, manifest_path, artifacts_dir FROM captures").fetchall())
        updated = 0
        for row in rows:
            mp = row["manifest_path"]
            ad = row["artifacts_dir"]
            new_mp = str((repo_root / mp).resolve()) if not Path(mp).is_absolute() else mp
            new_ad = str((repo_root / ad).resolve()) if not Path(ad).is_absolute() else ad
            if new_mp != mp or new_ad != ad:
                conn.execute(
                    "UPDATE captures SET manifest_path = ?, artifacts_dir = ? WHERE id = ?",
                    (new_mp, new_ad, row["id"]),
                )
                updated += 1
        conn.commit()
    return updated


def migrate(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.archive_root).resolve()
    repo_root = Path("/home/stadolf/source-archive")
    dry_run = args.dry_run

    target_db = root / "index.db"
    nested_root, nested_index, hash_dirs = detect_nested_state(root)

    has_nested_db = nested_index.exists()
    has_nested_dirs = bool(hash_dirs)

    print(f"Archive root: {root}")
    print(f"Target index: {target_db}")
    print(f"Nested index: {nested_index} ({'exists' if has_nested_db else 'missing'})")
    print(f"Nested captures dir: {nested_root} ({len(hash_dirs)} hash dirs)")

    if not has_nested_db and not has_nested_dirs:
        print("Nothing to migrate: no nested captures detected.")
        return 0

    # Validate that every nested index row points to the expected nested path.
    if has_nested_db:
        nested_rows = load_nested_rows(nested_index)
        for row in nested_rows:
            for col in ("manifest_path", "artifacts_dir"):
                val = row[col]
                if not is_expected_nested_path(val, root):
                    print(
                        f"ERROR: nested index row id={row['id']} has unexpected {col}: {val}",
                        file=sys.stderr,
                    )
                    print(
                        "Aborting: a nested manifest/artifacts path does not match the "
                        "expected <root>/captures/captures/<hash>/ layout.",
                        file=sys.stderr,
                    )
                    return 1

        # Determine how many rows will actually be merged vs. skipped.
        existing_pairs = load_existing_pairs(target_db)
        to_merge = [
            row
            for row in nested_rows
            if (row["url"], row["capture_hash"]) not in existing_pairs
        ]
        skipped = [row for row in nested_rows if (row["url"], row["capture_hash"]) in existing_pairs]
    else:
        nested_rows = []
        to_merge = []
        skipped = []

    target_before = count_target_rows(target_db)
    print(f"\nSummary:")
    print(f"  Target rows before merge: {target_before}")
    print(f"  Nested rows to merge:     {len(to_merge)}")
    print(f"  Nested rows skipped:      {len(skipped)}")
    print(f"  Capture dirs to move:     {len(hash_dirs)}")

    if dry_run:
        print("\nDry run — no changes made.")
        return 0

    # Validate move destinations before touching anything.
    for src in hash_dirs:
        dest = root / "captures" / src.name
        if dest.exists():
            print(
                f"ERROR: destination already exists, cannot move {src} -> {dest}",
                file=sys.stderr,
            )
            return 1

    # 1. Merge nested index rows into target index with rewritten paths.
    ensure_schema(target_db)
    with sqlite3.connect(target_db) as conn:
        for row in to_merge:
            manifest_path = rewrite_nested_path(row["manifest_path"], root)
            artifacts_dir = rewrite_nested_path(row["artifacts_dir"], root)
            conn.execute(
                """
                INSERT INTO captures
                    (url, capture_hash, captured_at, manifest_path,
                     artifacts_dir, http_status, wayback_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["url"],
                    row["capture_hash"],
                    row["captured_at"],
                    manifest_path,
                    artifacts_dir,
                    row["http_status"],
                    row["wayback_url"],
                ),
            )
        conn.commit()

    # 2. Move each nested capture directory up one level.
    moved = 0
    for src in hash_dirs:
        dest = root / "captures" / src.name
        os.rename(src, dest)
        moved += 1
        print(f"  moved {src.name}: {src} -> {dest}")

    # 3. Normalize pre-existing relative paths in the target index.
    normalized = normalize_relative_paths(target_db, repo_root)
    if normalized:
        print(f"  normalized {normalized} relative path(s) in target index")

    # 4. Backup and remove nested index, then remove empty nested dir.
    if has_nested_db:
        backup_path = Path("/tmp") / f"nested-index-backup-{_now_timestamp()}.db"
        shutil.copy2(nested_index, backup_path)
        print(f"  backed up nested index to {backup_path}")
        nested_index.unlink()
        print(f"  removed {nested_index}")

    if nested_root.exists() and not any(nested_root.iterdir()):
        nested_root.rmdir()
        print(f"  removed empty {nested_root}")

    target_after = count_target_rows(target_db)
    print(f"\nMigration complete.")
    print(f"  Target rows before: {target_before}")
    print(f"  Target rows after:  {target_after}")
    print(f"  Capture dirs moved: {moved}")
    return 0


if __name__ == "__main__":
    sys.exit(migrate())
