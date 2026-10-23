"""Create and verify a consistent SQLite backup for an XSA deployment."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def _validate(path: Path) -> tuple[str, int]:
    with closing(sqlite3.connect(path)) as db:
        quick_check = db.execute("PRAGMA quick_check").fetchone()
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
    if not quick_check or quick_check[0] != "ok":
        raise RuntimeError(f"SQLite quick_check failed for {path}")
    if violations:
        raise RuntimeError(f"SQLite foreign_key_check found {len(violations)} violation(s) in {path}")
    return str(quick_check[0]), len(violations)


def create_backup(source: Path, backup_dir: Path) -> Path:
    source = source.resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise FileNotFoundError(f"SQLite source is missing or empty: {source}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"xsa-before-deploy-{stamp}.sqlite3"
    partial = target.with_suffix(".sqlite3.partial")
    if target.exists() or partial.exists():
        raise FileExistsError(f"Backup target already exists: {target}")

    try:
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(partial)) as dst:
            src.backup(dst)
        _validate(partial)
        if partial.stat().st_size == 0:
            raise RuntimeError("SQLite backup is empty")
        os.replace(partial, target)
        _validate(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/state/xsa.sqlite3"))
    parser.add_argument("--backup-dir", type=Path, default=Path("/data/_maintenance/backups"))
    args = parser.parse_args()

    target = create_backup(args.source, args.backup_dir)
    quick_check, violations = _validate(target)
    print(
        json.dumps(
            {
                "backup": str(target),
                "bytes": target.stat().st_size,
                "quick_check": quick_check,
                "foreign_key_violations": violations,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
