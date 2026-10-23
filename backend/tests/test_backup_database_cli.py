from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_backup_database_cli_creates_and_checks_online_backup(tmp_path):
    source = tmp_path / "state" / "xsa.sqlite3"
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parents(id))"
        )
        db.execute("INSERT INTO parents VALUES (1, 'space metadata')")
        db.execute("INSERT INTO children VALUES (1, 1)")

    script = Path(__file__).parents[1] / "scripts" / "backup_database.py"
    backup_dir = tmp_path / "backups"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--source",
            str(source),
            "--backup-dir",
            str(backup_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    report = json.loads(result.stdout)
    backup = Path(report["backup"])
    assert backup.is_file()
    assert report["bytes"] > 0
    assert report["quick_check"] == "ok"
    assert report["foreign_key_violations"] == 0
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT name FROM parents WHERE id=1").fetchone() == ("space metadata",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
