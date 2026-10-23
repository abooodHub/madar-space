"""Safe maintenance helpers for organizing historical Space recordings."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from xsa.paths import SpacePaths


def organize_space_folders(
    data_dir: Path,
    spaces: Iterable[dict],
    active_ids: set[str] | None = None,
    metadata_overrides: dict[str, dict[str, str]] | None = None,
) -> dict:
    """Move completed legacy ID-only folders into the descriptive date layout.

    Unknown historical metadata is called out explicitly in the folder name;
    it is never guessed. Active or processing recordings are left in place.
    """
    active_ids = active_ids or set()
    metadata_overrides = metadata_overrides or {}
    result = {"moved": [], "skipped": [], "errors": []}
    unsafe_states = {"detected", "joining", "recording", "processing"}
    known_ids: set[str] = set()

    for record in spaces:
        space_id = str(record.get("space_id") or "")
        if not space_id:
            continue
        known_ids.add(space_id)
        if space_id in active_ids or str(record.get("state") or "").lower() in unsafe_states:
            result["skipped"].append({"space_id": space_id, "reason": "active_or_processing"})
            continue

        current = SpacePaths.find_space(data_dir, space_id)
        if not current.root.is_dir():
            result["skipped"].append({"space_id": space_id, "reason": "folder_not_found"})
            continue

        title = str(record.get("title") or "").strip()
        host = str(record.get("host_display_name") or record.get("host_username") or "").strip()
        metadata_path = current.space_json
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                title = title or str(metadata.get("title") or "").strip()
                host = host or str(metadata.get("host_display_name") or metadata.get("host") or "").strip()
            except (OSError, ValueError, TypeError):
                pass
        override = metadata_overrides.get(space_id, {})
        title = str(override.get("title") or title or "عنوان غير مسجل")
        host = str(override.get("host") or host or "مضيف غير مسجل")

        started_at = (
            record.get("recording_started_at") or record.get("started_at") or record.get("detected_at")
        )
        if not isinstance(started_at, datetime):
            try:
                started_at = datetime.fromisoformat(str(started_at)) if started_at else None
            except ValueError:
                started_at = None
        if started_at is None:
            started_at = datetime.fromtimestamp(current.root.stat().st_mtime, tz=UTC)

        try:
            updated = SpacePaths.for_space(data_dir, space_id, started_at, title=title, host=host)
            if updated.root == current.root:
                result["skipped"].append(
                    {"space_id": space_id, "reason": "already_descriptive", "path": str(current.root)}
                )
            else:
                result["moved"].append(
                    {"space_id": space_id, "from": str(current.root), "to": str(updated.root)}
                )
        except OSError as exc:
            result["errors"].append({"space_id": space_id, "error": str(exc)})

    # Preserve and organize orphaned legacy folders too. A join screenshot's
    # Unix timestamp is a better date anchor than the folder's changing mtime.
    root = data_dir / "spaces"
    if root.exists():
        for current in root.glob("*/*/*"):
            space_id = current.name
            if not current.is_dir() or not space_id.isalnum() or len(space_id) < 10 or space_id in known_ids:
                continue
            if space_id in active_ids:
                result["skipped"].append({"space_id": space_id, "reason": "active"})
                continue
            override = metadata_overrides.get(space_id, {})
            title = str(override.get("title") or "عنوان غير مسجل")
            host = str(override.get("host") or "مضيف غير مسجل")
            started_at = override.get("started_at")
            if not isinstance(started_at, datetime):
                join_screens = sorted(current.glob("screenshots/join_*.png"))
                timestamp_match = None
                if join_screens:
                    import re

                    timestamp_match = re.search(r"join_(\d{9,})\.png$", join_screens[0].name)
                if timestamp_match:
                    started_at = datetime.fromtimestamp(int(timestamp_match.group(1)), tz=UTC)
                else:
                    started_at = datetime.fromtimestamp(current.stat().st_mtime, tz=UTC)
            try:
                updated = SpacePaths.for_space(data_dir, space_id, started_at, title=title, host=host)
                if updated.root != current:
                    result["moved"].append(
                        {"space_id": space_id, "from": str(current), "to": str(updated.root)}
                    )
            except OSError as exc:
                result["errors"].append({"space_id": space_id, "error": str(exc)})

    return result
