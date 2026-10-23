"""Persist discoveries independently from recording, with visible freshness."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path


class LiveDiscovery:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "live-discovery.json"
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.entries = {}
        # A restart may interrupt verification or browser setup. The durable
        # request remains eligible, while the normal database/session checks
        # prevent a second capture of a recording that already began.
        for row in self.entries.values():
            if isinstance(row, dict) and row.get("capture_request_state") == "starting":
                row["capture_request_state"] = "queued"
                row["next_check_epoch"] = 0

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(self.entries, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def remember(self, space_id: str, account: str | None = None) -> dict:
        row = self.entries.setdefault(
            space_id,
            {
                "space_id": space_id,
                "title": "مساحة قيد التحقق",
                "host_username": None,
                "host_display_name": "المضيف قيد التحقق",
                "host_avatar": "",
                "role": "unknown",
                "listeners_count": None,
                "state": "unverified",
                "checked_at_epoch": None,
                "next_check_epoch": 0,
                "url": f"https://x.com/i/spaces/{space_id}",
                "discovered_at": datetime.now(UTC).isoformat(),
                "discovered_via": [],
            },
        )
        if account and account not in row["discovered_via"]:
            row["discovered_via"].append(account)
        self.save()
        return row

    def request_capture(self, space_id: str) -> dict:
        row = self.remember(space_id, "manual_link")
        if row.get("capture_request_state") not in {"queued", "starting", "started"}:
            row["capture_request_state"] = "queued"
            row["capture_requested_at"] = datetime.now(UTC).isoformat()
            # A direct operator request becomes eligible for the next safe
            # verification pass even if a previous inconclusive check was cached.
            row["next_check_epoch"] = 0
        self.save()
        return row

    def cancel_capture_request(self, space_id: str) -> bool:
        row = self.entries.get(space_id)
        if not row or row.get("capture_request_state") != "queued":
            return False
        row["capture_request_state"] = "cancelled"
        self.save()
        return True

    def apply_metadata(self, space_id: str, value: dict | None) -> dict:
        row = self.remember(space_id)
        now = time.time()
        row["next_check_epoch"] = now + 120
        if not value:
            self.save()
            return row
        metadata = value.get("metadata") or {}
        identity = str(value.get("rest_id") or metadata.get("rest_id") or "")
        if identity != space_id:
            self.save()
            return row
        state = {"Running": "live", "Ended": "ended", "NotStarted": "scheduled"}.get(metadata.get("state"))
        if not state:
            self.save()
            return row
        creator = (metadata.get("creator_results") or {}).get("result") or {}
        legacy, core = creator.get("legacy") or {}, creator.get("core") or {}
        username = legacy.get("screen_name") or core.get("screen_name")
        avatar = legacy.get("profile_image_url_https") or (creator.get("avatar") or {}).get("image_url")
        row.update(
            state=state,
            checked_at_epoch=now,
            checked_at=datetime.now(UTC).isoformat(),
            verification_source="x_graphql_AudioSpaceById",
            title=metadata.get("title") or "مساحة مباشرة",
            host_username=username,
            host_display_name=legacy.get("name") or core.get("name") or username or "المضيف غير معروف",
            host_avatar=avatar or "",
            role="host" if username else "unknown",
            listeners_count=metadata.get("total_live_listeners"),
        )
        if state in {"ended", "scheduled"} and row.get("capture_request_state") == "queued":
            row["capture_request_state"] = "not_live"
        self.save()
        return row

    def pending_ids(self) -> list[str]:
        now = time.time()
        due = [
            sid
            for sid, row in self.entries.items()
            if (
                row.get("state") not in {"ended", "scheduled"} or row.get("capture_request_state") == "queued"
            )
            and row.get("next_check_epoch", 0) <= now
        ]
        return sorted(due, key=lambda sid: self.entries[sid].get("capture_request_state") != "queued")

    def feed(self) -> list[dict]:
        now = time.time()
        result = []
        for row in self.entries.values():
            if row.get("state") in {"ended", "scheduled"}:
                continue
            checked = row.get("checked_at_epoch")
            age = now - checked if checked is not None else None
            verification = (
                "confirmed_live"
                if row.get("state") == "live" and age is not None and age <= 180
                else "stale"
                if checked
                else "pending"
            )
            result.append(
                {
                    **row,
                    "verification_state": verification,
                    "host_verified": bool(row.get("host_username") and checked),
                    "age_seconds": round(age, 1) if age is not None else None,
                    "is_recording": False,
                }
            )
        return result
