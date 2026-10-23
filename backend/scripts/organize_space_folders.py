"""Safely organize historical Space folders without breaking DB file paths.

Run in the app container. Preview is the default; --apply requires the
maintenance gate and an independently safe /maintenance/status response.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contextlib

from xsa.config import get_settings
from xsa.db import RecordingSegment, Space, get_session, init_db
from xsa.maintenance_gate import maintenance_active
from xsa.paths import _LOCAL_TZ, _find_space_folder

SAFE_STATES = {"ended", "done", "transcribed", "analyzed", "recovered_stale", "error"}


def _as_utc(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        value = fallback
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _records(data_dir: Path) -> tuple[list[dict[str, Any]], dict[str, set[int]]]:
    records: list[dict[str, Any]] = []
    segment_ids: dict[str, set[int]] = {}
    with get_session() as db:
        for space in db.query(Space).all():
            account = space.host_account
            metadata = space.space_metadata if isinstance(space.space_metadata, dict) else {}
            username = str(account.username if account else metadata.get("host") or "مضيف غير مسجل")
            host = str(
                account.display_name
                if account and account.display_name
                else metadata.get("host_display_name") or username
            )
            records.append(
                {
                    "space_id": space.space_id,
                    "state": (space.state or "").lower(),
                    "title": str(space.title or metadata.get("title") or ""),
                    "host": host,
                    "host_username": username,
                    "started_at": _as_utc(
                        space.recording_started_at or space.started_at or space.detected_at,
                        datetime.fromtimestamp(data_dir.stat().st_mtime, UTC),
                    ),
                }
            )
            segment_ids[space.space_id] = {item.id for item in space.recording_segments}
    return records, segment_ids


def _get_runtime_status() -> dict[str, Any]:
    response = httpx.get("http://127.0.0.1:8765/maintenance/status", timeout=5)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Maintenance status response is invalid")
    return payload


def _existing_folder_details(folder: Path, space_id: str) -> tuple[str | None, str | None, str | None]:
    """Preserve useful title/host/time already present in a folder name."""
    suffix = f" - {space_id}"
    if not folder.name.endswith(suffix):
        return None, None, None
    middle = folder.name[: -len(suffix)]
    match = re.match(r"^(\d{6})(?: - (.*))?$", middle)
    if not match:
        return None, None, None
    details = match.group(2) or ""
    if " - " in details:
        title, host = details.rsplit(" - ", 1)
    else:
        title, host = details, None
    return match.group(1), title or None, host or None


def _make_plan(data_dir: Path, records: list[dict[str, Any]], active_ids: set[str]) -> dict[str, Any]:
    plan: dict[str, Any] = {"moves": [], "skipped": [], "errors": []}
    claimed_targets: set[Path] = set()
    for record in records:
        sid = record["space_id"]
        state = record["state"]
        if sid in active_ids or state not in SAFE_STATES:
            plan["skipped"].append({"space_id": sid, "reason": "active_or_nonterminal", "state": state})
            continue
        source = _find_space_folder(data_dir, sid)
        if source is None:
            plan["skipped"].append({"space_id": sid, "reason": "folder_not_found"})
            continue
        old_clock, old_title, old_host = _existing_folder_details(source, sid)
        sidecar: dict[str, Any] = {}
        try:
            sidecar_value = json.loads((source / "space.json").read_text(encoding="utf-8"))
            if isinstance(sidecar_value, dict):
                sidecar = sidecar_value
        except (OSError, ValueError, TypeError):
            pass

        username = record["host_username"].lower().lstrip("@")
        known_host = record["host"] if record["host"] != "مضيف غير مسجل" else ""
        host = str(sidecar.get("host_display_name") or known_host or old_host or "مضيف غير مسجل")
        title = str(sidecar.get("title") or record["title"] or "")
        if not title or title in {"عنوان غير مسجل", record["host"], record["host_username"]}:
            candidate = old_title or ""
            if candidate.lower().lstrip("@").casefold() in {
                username.casefold(),
                host.lower().lstrip("@").casefold(),
            }:
                candidate = ""
            title = candidate or "عنوان غير مسجل"
        started = record["started_at"]
        sidecar_start = sidecar.get("started_at")
        if sidecar_start:
            try:
                parsed_start = datetime.fromisoformat(str(sidecar_start).replace("Z", "+00:00"))
                started = _as_utc(parsed_start, started)
            except ValueError:
                pass
        started = started.astimezone(_LOCAL_TZ)
        clock = old_clock or f"{started:%H%M%S}"
        name = " - ".join(part for part in (clock, title, host, sid) if part)
        target = data_dir / "spaces" / f"{started:%Y}" / f"{started:%m}" / f"{started:%d}" / name
        if source == target:
            plan["skipped"].append({"space_id": sid, "reason": "already_organized", "path": str(source)})
            continue
        if target in claimed_targets or (target.exists() and target != source):
            plan["errors"].append({"space_id": sid, "reason": "destination_exists", "path": str(target)})
            continue
        claimed_targets.add(target)
        plan["moves"].append(
            {
                "space_id": sid,
                "from": str(source),
                "to": str(target),
            }
        )
    return plan


def _backup_database(db_path: Path, data_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir / "_maintenance" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"xsa-before-folder-organization-{stamp}.sqlite3"
    with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(backup_path)) as target:
        source.backup(target)
    return backup_path


def _rewrite_segment_path(value: str, old_root: Path, new_root: Path) -> str:
    old = old_root.as_posix().rstrip("/")
    raw = value.replace("\\", "/")
    if raw == old:
        return new_root.as_posix()
    if raw.startswith(old + "/"):
        return new_root.as_posix().rstrip("/") + raw[len(old) :]
    return value


def _rewrite_metadata_paths(folder: Path, data_dir: Path, originals: list[tuple[Path, bytes]]) -> None:
    for path in (folder / "space.json", folder / "speaker_timeline.json"):
        if not path.is_file():
            continue
        try:
            original = path.read_bytes()
            value = json.loads(original.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        relative = folder.relative_to(data_dir).as_posix()
        changed = False
        if "folder_relative" in value:
            value["folder_relative"] = relative
            changed = True
        if "folder" in value:
            value["folder"] = relative
            changed = True
        if changed:
            originals.append((path, original))
            temporary = path.with_name(path.name + ".organize.tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)


def _apply(plan: dict[str, Any], segment_ids: dict[str, set[int]], data_dir: Path, db_path: Path) -> None:
    moved: list[tuple[Path, Path]] = []
    metadata_originals: list[tuple[Path, bytes]] = []
    backup_path = _backup_database(db_path, data_dir)
    try:
        for move in plan["moves"]:
            source, target = Path(move["from"]), Path(move["to"])
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            moved.append((source, target))
            _rewrite_metadata_paths(target, data_dir, metadata_originals)

        with get_session() as db:
            all_ids = {seg_id for ids in segment_ids.values() for seg_id in ids}
            segments = (
                db.query(RecordingSegment).filter(RecordingSegment.id.in_(all_ids)).all() if all_ids else []
            )
            by_id = {segment.id: segment for segment in segments}
            for move in plan["moves"]:
                old_root, new_root = Path(move["from"]), Path(move["to"])
                for segment_id in segment_ids.get(move["space_id"], set()):
                    segment = by_id.get(segment_id)
                    if segment:
                        segment.file_path = _rewrite_segment_path(segment.file_path, old_root, new_root)
            db.commit()
    except Exception:
        for path, original in reversed(metadata_originals):
            with contextlib.suppress(OSError):
                path.write_bytes(original)
        for old_root, new_root in reversed(moved):
            try:
                if new_root.exists() and not old_root.exists():
                    old_root.parent.mkdir(parents=True, exist_ok=True)
                    new_root.rename(old_root)
            except OSError:
                pass
        raise
    print(f"Database backup: {backup_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Apply the reviewed plan; default is preview only"
    )
    args = parser.parse_args()

    cfg = get_settings()
    init_db(cfg.db_path)
    records, segment_ids = _records(cfg.data_dir)
    status = _get_runtime_status()
    active_ids = set(status.get("active_recordings") or [])
    plan = _make_plan(cfg.data_dir, records, active_ids)
    print(json.dumps(plan, ensure_ascii=False, indent=2))

    if not args.apply:
        print("PREVIEW_ONLY: no folders or database rows were changed.")
        return 0
    if not maintenance_active(cfg.db_path.parent):
        raise RuntimeError("Apply requires the maintenance gate to be armed.")
    status = _get_runtime_status()
    if (
        not status.get("maintenance_mode")
        or not status.get("safe_to_deploy")
        or status.get("active_recordings")
        or status.get("recording_ffmpeg_count")
        or status.get("active_post_processing")
    ):
        raise RuntimeError("Apply refused: production is not independently confirmed idle.")
    if plan["errors"]:
        raise RuntimeError("Apply refused: resolve destination collisions shown in the plan first.")
    _apply(plan, segment_ids, cfg.data_dir, cfg.db_path)
    print(f"APPLIED: moved {len(plan['moves'])} completed Space folders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
