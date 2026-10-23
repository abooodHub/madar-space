from __future__ import annotations

import sqlite3

import pytest

from xsa.db import database, init_db, schema_migrations


def test_schema_ledger_rejects_changed_migration_definitions(tmp_path):
    path = tmp_path / "schema.sqlite3"
    init_db(path)
    database._engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]
        db.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=2")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        init_db(path)
    assert database._engine is None and database._SessionLocal is None


def test_failed_additive_migration_rolls_back_columns_and_ledger(tmp_path, monkeypatch):
    path = tmp_path / "schema.sqlite3"
    init_db(path)
    database._engine.dispose()
    migration = {
        "version": 5,
        "name": "controlled_failure",
        "columns": {"monitored_accounts": {"temporary_added_column": "TEXT"}},
        "statements": ["INVALID SQL FOR ROLLBACK TEST"],
    }
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", (*schema_migrations.MIGRATIONS, migration))
    with pytest.raises(Exception, match="syntax error"):
        init_db(path)
    with sqlite3.connect(path) as db:
        assert "temporary_added_column" not in {
            row[1] for row in db.execute("PRAGMA table_info(monitored_accounts)")
        }
        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]
