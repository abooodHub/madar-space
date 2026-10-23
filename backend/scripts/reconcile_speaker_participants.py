"""Idempotently reconcile saved speaker JSONL evidence into participant tables.

Run with `--dry-run` first. This command only writes participants,
space_participants, and missing/linkable speaker_events rows; raw JSONL files are
read-only inputs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xsa.config import get_settings  # noqa: E402
from xsa.db import Space, get_session, init_db  # noqa: E402
from xsa.db.models import SpeakerEvent  # noqa: E402
from xsa.db.participant_persistence import persist_space_participant  # noqa: E402
from xsa.paths import SpacePaths  # noqa: E402


def _read_jsonl(path: Path | None, expected_space_id: str) -> tuple[list[dict[str, Any]], int]:
    if path is None or not path.is_file():
        return [], 0
    records: list[dict[str, Any]] = []
    malformed = 0
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            record_space_id = record.get("space_id")
            if record_space_id and record_space_id != expected_space_id:
                malformed += 1
                continue
            records.append(record)
    return records, malformed


def _timestamp(record: dict[str, Any], space_start: datetime) -> datetime:
    raw = record.get("observed_at") or record.get("wall_time")
    if isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
        except ValueError:
            pass
    offset = record.get("recording_time", record.get("timestamp"))
    try:
        return space_start + timedelta(seconds=max(0.0, float(offset)))
    except (TypeError, ValueError):
        return space_start


def _participant_evidence(records: list[dict[str, Any]]) -> list[tuple[datetime, dict[str, Any]]]:
    evidence: list[tuple[datetime, dict[str, Any]]] = []
    for record in records:
        participants = record.get("participants")
        if isinstance(participants, list):
            for participant in participants:
                if isinstance(participant, dict):
                    evidence.append((_timestamp(record, datetime.now(UTC).replace(tzinfo=None)), participant))
        elif record.get("username") or record.get("x_user_id") or record.get("periscope_id"):
            evidence.append((_timestamp(record, datetime.now(UTC).replace(tzinfo=None)), record))
    return evidence


def _identity_conflicts(evidence: list[tuple[datetime, dict[str, Any]]]) -> list[dict[str, Any]]:
    by_username: dict[str, set[str]] = defaultdict(set)
    by_x_id: dict[str, set[str]] = defaultdict(set)
    for _, row in evidence:
        username = str(row.get("username") or "").strip()
        x_user_id = str(row.get("x_user_id") or "").strip()
        if username and x_user_id:
            by_username[username.casefold()].add(x_user_id)
            by_x_id[x_user_id].add(username.casefold())
    conflicts: list[dict[str, Any]] = []
    conflicts.extend(
        {"kind": "username_maps_to_multiple_x_ids", "username_key": key, "x_user_ids": sorted(values)}
        for key, values in by_username.items()
        if len(values) > 1
    )
    conflicts.extend(
        {"kind": "x_user_id_maps_to_multiple_usernames", "x_user_id": key, "username_keys": sorted(values)}
        for key, values in by_x_id.items()
        if len(values) > 1
    )
    return conflicts


def reconcile(space_id: str, *, data_dir: Path, dry_run: bool) -> dict[str, Any]:
    paths = SpacePaths.resolve_existing(data_dir, space_id)
    if paths is None:
        raise FileNotFoundError(
            f"No existing Space folder or matching sidecar found for {space_id} under {data_dir}"
        )

    observations, malformed_observations = _read_jsonl(paths.root / "speaker_observations.jsonl", space_id)
    events, malformed_events = _read_jsonl(paths.root / "speaker_events.jsonl", space_id)
    evidence = _participant_evidence(observations + events)
    conflicts = _identity_conflicts(evidence)
    identities: dict[str, dict[str, Any]] = {}
    roles: Counter[str] = Counter()
    for _, row in evidence:
        username = str(row.get("username") or "").strip()
        x_user_id = str(row.get("x_user_id") or "").strip()
        key = f"x:{x_user_id}" if x_user_id else f"u:{username.casefold()}" if username else ""
        if key:
            identities.setdefault(key, row)
        role = str(row.get("role") or "unknown").strip().casefold()
        if role:
            roles[role] += 1

    init_db(get_settings().db_path)
    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if space is None:
            raise LookupError(f"Space {space_id} does not exist in the database")
        space_start = space.recording_started_at or space.started_at or space.detected_at
        if space_start.tzinfo is not None:
            space_start = space_start.astimezone(UTC).replace(tzinfo=None)

        report: dict[str, Any] = {
            "space_id": space_id,
            "resolved_path": str(paths.root),
            "mode": "dry-run" if dry_run else "apply",
            "observations": len(observations),
            "events_read": len(events),
            "identities_found": len(identities),
            "usernames": sorted(
                {str(row.get("username")).strip() for _, row in evidence if row.get("username")}
            ),
            "participants_to_create": 0,
            "space_participant_relationships_to_create": 0,
            "speaker_events_to_create": 0,
            "speaker_events_to_link": 0,
            "roles_observed": dict(sorted(roles.items())),
            "conflicts": conflicts,
            "malformed_records": malformed_observations + malformed_events,
        }

        try:
            for observed_at, row in sorted(evidence, key=lambda item: item[0]):
                participant, created, relationship_created, conflict = persist_space_participant(
                    db,
                    space_db_id=space.id,
                    evidence=row,
                    observed_at=observed_at,
                )
                report["participants_to_create"] += int(created)
                report["space_participant_relationships_to_create"] += int(relationship_created)
                if conflict:
                    report["conflicts"].append({"kind": "database_identity_match_conflict"})
                if not participant:
                    continue

            for row in events:
                if not row.get("event") or not row.get("username"):
                    continue
                observed_at = _timestamp(row, space_start)
                offset = row.get("recording_time", row.get("timestamp"))
                try:
                    offset_ms = round(max(0.0, float(offset)) * 1000)
                except (TypeError, ValueError):
                    offset_ms = None
                participant, created, relationship_created, conflict = persist_space_participant(
                    db,
                    space_db_id=space.id,
                    evidence=row,
                    observed_at=observed_at,
                )
                report["participants_to_create"] += int(created)
                report["space_participant_relationships_to_create"] += int(relationship_created)
                if conflict:
                    report["conflicts"].append({"kind": "database_identity_match_conflict"})
                query = db.query(SpeakerEvent).filter_by(
                    space_id=space.id,
                    event_type=str(row["event"]),
                    username=str(row["username"]),
                    recording_offset_ms=offset_ms,
                )
                existing = query.order_by(SpeakerEvent.id).first()
                if existing:
                    if participant and existing.participant_id is None:
                        existing.participant_id = participant.id
                        report["speaker_events_to_link"] += 1
                elif offset_ms is not None:
                    db.add(
                        SpeakerEvent(
                            space_id=space.id,
                            recording_offset_ms=offset_ms,
                            wall_time=observed_at,
                            event_type=str(row["event"]),
                            participant_id=participant.id if participant else None,
                            username=str(row["username"]),
                            x_user_id=row.get("x_user_id"),
                            source=str(row.get("source") or "x_dom"),
                            confidence=row.get("confidence"),
                            raw_data={
                                key: value for key, value in row.items() if key not in {"username", "event"}
                            },
                        )
                    )
                    report["speaker_events_to_create"] += 1

            if dry_run:
                db.rollback()
            else:
                db.commit()
        except Exception:
            db.rollback()
            raise
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("space_id")
    parser.add_argument("--data-dir", type=Path, default=get_settings().data_dir)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = reconcile(args.space_id, data_dir=args.data_dir, dry_run=args.dry_run)
    except Exception as exc:
        print(f"RECONCILIATION_FAILED: {type(exc).__name__}: {exc}")
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
