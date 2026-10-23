"""Small, credential-free worker liveness record shared with the read-only API."""

from __future__ import annotations

import json
import os
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_PHASES = {"starting", "polling", "processing", "blocked", "error", "stopped"}
_REASONS = {"model_access_not_configured", "dependencies_missing", "worker_loop_error"}


def read_worker_status(state_dir: Path, *, now: datetime | None = None) -> dict:
    """Missing/stale reports never imply readiness or identify the cause of absence."""
    unavailable = {"status": "unavailable", "reason": "no_recent_heartbeat"}
    try:
        record = json.loads((state_dir / "diarization-worker.json").read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(record["updated_at"])
        age = ((now or datetime.now(UTC)) - stamp).total_seconds()
        if record["phase"] not in _PHASES or not 0 <= age <= 45:
            return unavailable
        return {
            "status": "stopped" if record["phase"] == "stopped" else "online",
            "phase": record["phase"],
            "reason": record.get("reason") if record.get("reason") in _REASONS else None,
            "age_seconds": round(age, 1),
            "updated_at": stamp.isoformat(),
        }
    except (OSError, ValueError, TypeError, KeyError):
        return unavailable


class WorkerHeartbeat:
    """Report every ten seconds, including while idle, blocked, or running inference."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "diarization-worker.json"
        self.phase = "starting"
        self.reason: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _write(self) -> None:
        with self._lock:
            record = {"phase": self.phase, "reason": self.reason, "updated_at": datetime.now(UTC).isoformat()}
            temporary = self.path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(record), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                # Status reporting must never interrupt a claimed analysis job.
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def set_phase(self, phase: str, reason: str | None = None) -> None:
        if phase not in _PHASES or (reason is not None and reason not in _REASONS):
            raise ValueError("Invalid worker status")
        with self._lock:
            self.phase, self.reason = phase, reason
        self._write()

    def _run(self) -> None:
        while not self._stop.wait(10):
            self._write()

    def __enter__(self):
        self._write()
        self._thread.start()
        return self

    def __exit__(self, *_args):
        self._stop.set()
        self._thread.join(timeout=2)
        self.set_phase("stopped")
