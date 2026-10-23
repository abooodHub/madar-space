"""Build a compact, replayable speaking-turn timeline from live observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOCAL_TZ = timezone(timedelta(hours=3), "Asia/Riyadh")


def _local_time(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(LOCAL_TZ).isoformat()
    except (TypeError, ValueError):
        return None


def build_speaker_timeline(
    *,
    space_id: str,
    title: str | None,
    host: str | None,
    space_url: str | None,
    detected_at: datetime | None,
    started_at: datetime | None,
    ended_at: datetime | None,
    events: list[dict],
    folder: Path,
    data_dir: Path,
    live_offset_ms: int | None = None,
    metadata: dict | None = None,
) -> dict:
    """Pair start/stop events and calculate turns, totals, and folder metadata."""
    metadata = metadata or {}
    events.sort(
        key=lambda item: (
            float(item.get("timestamp")) if isinstance(item.get("timestamp"), (int, float)) else float("inf"),
            item.get("wall_time") or "",
        )
    )
    turns: list[dict] = []
    active: dict[str, dict] = {}

    def close_turn(username: str, end_event: dict, is_open: bool = False) -> None:
        turn = active.pop(username, None)
        if not turn:
            return
        start_ms = turn["start_ms"]
        timestamp = end_event.get("timestamp")
        end_ms = int(float(timestamp) * 1000) if isinstance(timestamp, (int, float)) else start_ms
        if end_event.get("duration_ms") is not None:
            end_ms = max(end_ms, start_ms + int(end_event["duration_ms"]))
        end_ms = max(start_ms, end_ms)
        turns.append(
            {
                **turn,
                "end_ms": end_ms,
                "duration_ms": end_ms - start_ms,
                "end_at": _local_time(end_event.get("wall_time")),
                "is_open": is_open,
            }
        )

    for event in events:
        username = event.get("username")
        kind = event.get("event") or event.get("event_type")
        timestamp = event.get("timestamp")
        if not username or not isinstance(timestamp, (int, float)):
            continue
        offset_ms = max(0, int(float(timestamp) * 1000))
        if kind == "speaker_started":
            for previous in list(active):
                close_turn(previous, {**event, "timestamp": timestamp})
            active[username] = {
                "username": username,
                "display_name": event.get("display_name") or username,
                "role": event.get("role") or "speaker",
                "source": event.get("source") or "x_dom",
                "start_ms": offset_ms,
                "start_at": _local_time(event.get("wall_time")),
                "end_ms": None,
                "end_at": None,
                "duration_ms": 0,
                "is_open": True,
            }
        elif kind == "speaker_stopped":
            close_turn(username, {**event, "timestamp": timestamp})

    if started_at and ended_at:
        recording_end_ms = max(0, int((ended_at - started_at).total_seconds() * 1000))
    else:
        recording_end_ms = live_offset_ms or 0
    for username in list(active):
        start_ms = active[username]["start_ms"]
        close_turn(username, {"timestamp": max(start_ms, recording_end_ms) / 1000}, is_open=True)

    turns.sort(key=lambda item: (item["start_ms"], item["username"]))
    totals: dict[str, dict] = {}
    for turn in turns:
        speaker = totals.setdefault(
            turn["username"],
            {
                "username": turn["username"],
                "display_name": turn["display_name"],
                "role": turn["role"],
                "speaking_ms": 0,
                "turn_count": 0,
                "turns": [],
            },
        )
        speaker["speaking_ms"] += turn["duration_ms"]
        speaker["turn_count"] += 1
        speaker["turns"].append(turn)

    try:
        folder_relative = folder.relative_to(data_dir).as_posix()
    except ValueError:
        folder_relative = folder.as_posix()
    return {
        "space_id": space_id,
        "space": {
            "title": title or metadata.get("title") or "مساحة بدون عنوان",
            "host": host or metadata.get("host") or "unknown",
            "url": space_url or metadata.get("url") or f"https://x.com/i/spaces/{space_id}",
            "detected_at": _local_time(detected_at),
            "recording_started_at": _local_time(started_at) or _local_time(metadata.get("started_at")),
            "recording_ended_at": _local_time(ended_at) or _local_time(metadata.get("ended_at")),
            "timezone": "Asia/Riyadh",
            "folder": folder_relative,
            "folder_absolute": str(folder),
        },
        "speakers": sorted(totals.values(), key=lambda item: item["speaking_ms"], reverse=True),
        "turns": turns,
        "events": events,
        "event_count": len(events),
        "turn_count": len(turns),
        "attribution": "x_dom",
        "attribution_note": "أوقات المتحدثين مبنية على رصد واجهة X؛ قد لا تُلتقط التحولات القصيرة أو غير الظاهرة.",
    }
