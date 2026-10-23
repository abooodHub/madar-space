"""Serialize artifact publication in threads and across production processes."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


@contextmanager
def artifact_lock(target: Path):
    key = str(target.resolve())
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.with_name(target.name + ".lock").open("a+b") as handle:
            try:
                import fcntl
            except ImportError:
                fcntl = None
            if fcntl:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
