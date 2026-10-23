"""File-backed deployment gate shared by the app and host tooling."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOCK_NAME = "maintenance.lock"


def lock_path(state_dir: Path) -> Path:
    """Return the persistent maintenance marker path (normally under /state)."""
    return state_dir / LOCK_NAME


def maintenance_active(state_dir: Path) -> bool:
    return lock_path(state_dir).exists()


def arm_maintenance(state_dir: Path) -> dict[str, Any]:
    """Atomically pause new recording discovery without interrupting sessions."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path(state_dir)
    value = {
        "schema_version": 1,
        "lock_id": secrets.token_urlsafe(16),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return value


def release_maintenance(state_dir: Path) -> bool:
    """Remove the maintenance marker; callers should do this after deployment."""
    path = lock_path(state_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
