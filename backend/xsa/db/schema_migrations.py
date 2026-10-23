"""Numbered additive SQLite migrations, applied in one explicit transaction.

The deployment gate must take an online backup before application startup.
Rollback keeps additive columns and runs the previous application image.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import text

MIGRATIONS = (
    {
        "version": 1,
        "name": "legacy_join_participant_and_job_ownership",
        "columns": {
            "monitored_accounts": {"tags": "TEXT"},
            "participants": {"last_seen_at": "DATETIME"},
            "spaces": {
                "transcript_generation_id": "VARCHAR(64)",
                "joining_started_at": "DATETIME",
                "join_checkpoint": "VARCHAR(100)",
                "join_checkpoint_at": "DATETIME",
                "last_progress_at": "DATETIME",
                "join_deadline_seconds": "FLOAT",
                "join_age_seconds": "FLOAT",
                "join_outcome": "VARCHAR(80)",
                "join_last_error": "TEXT",
                "join_host_verification_state": "VARCHAR(40)",
                "join_anonymous_listen_state": "VARCHAR(40)",
                "join_space_state": "VARCHAR(40)",
                "join_browser_url": "TEXT",
                "join_browser_title": "TEXT",
            },
            "jobs": {
                "owner_id": "VARCHAR(100)",
                "claimed_at": "DATETIME",
                "heartbeat_at": "DATETIME",
                "lease_expires_at": "DATETIME",
                "attempt_id": "VARCHAR(64)",
            },
        },
        "statements": [
            "UPDATE participants SET last_seen_at=updated_at WHERE last_seen_at IS NULL",
            "CREATE INDEX IF NOT EXISTS ix_participants_last_seen_at ON participants(last_seen_at)",
        ],
    },
    {
        "version": 2,
        "name": "persisted_account_discovery_schedule",
        "columns": {
            "monitored_accounts": {
                "next_check_at": "DATETIME",
                "last_poll_outcome": "VARCHAR(40)",
                "last_poll_duration_seconds": "FLOAT",
                "last_poll_request_count": "INTEGER",
                "poll_failure_streak": "INTEGER NOT NULL DEFAULT 0",
            }
        },
        "statements": [
            "CREATE INDEX IF NOT EXISTS ix_accounts_discovery_due "
            "ON monitored_accounts(active,next_check_at,last_checked_at)",
        ],
    },
    {
        "version": 3,
        "name": "account_priority_groups",
        "columns": {"monitored_accounts": {"priority": "VARCHAR(2) NOT NULL DEFAULT 'P2'"}},
        "statements": [
            "CREATE INDEX IF NOT EXISTS ix_accounts_priority_due "
            "ON monitored_accounts(priority,active,next_check_at,last_checked_at)",
        ],
    },
    {
        "version": 4,
        "name": "verified_space_host_without_watched_account",
        "columns": {"spaces": {"verified_host_username": "VARCHAR(100)"}},
        "statements": [],
    },
)


def apply_migrations(connection) -> None:
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
    )
    applied = {
        row.version: row.checksum
        for row in connection.execute(text("SELECT version,checksum FROM schema_migrations"))
    }
    known = {migration["version"] for migration in MIGRATIONS}
    if set(applied) - known:
        raise RuntimeError("Database schema is newer than this application; refusing automatic migration")
    for migration in MIGRATIONS:
        version = migration["version"]
        checksum = hashlib.sha256(json.dumps(migration, sort_keys=True).encode()).hexdigest()
        if version in applied and applied[version] != checksum:
            raise RuntimeError(f"Schema migration {version} checksum mismatch")
        for table, additions in migration["columns"].items():
            columns = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}
            missing = set(additions) - columns
            if version in applied and missing:
                raise RuntimeError(f"Schema migration {version} is recorded but {table} columns are missing")
            for column, sql_type in additions.items():
                if column in missing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
        if version not in applied:
            for statement in migration["statements"]:
                connection.execute(text(statement))
            connection.execute(
                text(
                    "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(:version,:name,:checksum,:at)"
                ),
                {
                    "version": version,
                    "name": migration["name"],
                    "checksum": checksum,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
