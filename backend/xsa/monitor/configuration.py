"""Persist non-secret monitoring limits without replacing capture services."""

from __future__ import annotations

import json
import os
from pathlib import Path

FIELDS = ("max_active_accounts", "poll_interval_seconds", "max_graphql_requests_per_cycle")


def load_monitoring_config(state_dir: Path) -> dict[str, int]:
    try:
        value = json.loads((state_dir / "monitoring-config.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
        limits = {
            "max_active_accounts": (1, 59),
            "poll_interval_seconds": (300, 3600),
            "max_graphql_requests_per_cycle": (1, 1000),
        }
        return {
            key: value[key]
            for key in FIELDS
            if type(value.get(key)) is int and limits[key][0] <= value[key] <= limits[key][1]
        }
    except (OSError, ValueError):
        return {}


def save_monitoring_config(state_dir: Path, values: dict[str, int]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "monitoring-config.json"
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({key: values[key] for key in FIELDS}), encoding="utf-8")
    temporary.replace(path)
