"""Non-blocking measurements scoped to the application process."""

from __future__ import annotations

import threading
import time

import psutil

_lock = threading.Lock()
_previous: tuple[float, float] | None = None


def application_metrics() -> dict:
    global _previous
    try:
        process = psutil.Process()
        cpu = process.cpu_times()
        current = (time.monotonic(), cpu.user + cpu.system)
        percent = None
        sample_seconds = None
        with _lock:
            if _previous is not None and current[0] - _previous[0] >= 0.1:
                sample_seconds = current[0] - _previous[0]
                percent = round(max(0, current[1] - _previous[1]) * 100 / sample_seconds, 1)
            if _previous is None or sample_seconds is not None:
                _previous = current
        return {
            "scope": "application_process",
            "rss_mb": round(process.memory_info().rss / 1024**2, 1),
            "cpu_percent_one_core": percent,
            "cpu_sample_seconds": sample_seconds,
            "cpu_basis": "process CPU seconds / elapsed wall seconds; 100% is one logical core",
        }
    except (psutil.Error, OSError):
        return {
            "scope": "application_process",
            "rss_mb": None,
            "cpu_percent_one_core": None,
            "cpu_sample_seconds": None,
            "status": "unavailable",
        }
