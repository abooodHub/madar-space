"""Append-only evidence and conservative participant state transitions.

The tracker only records facts supplied by the browser adapter. In particular,
an unknown speech indicator stays unknown; speaker role is not speech activity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def participant_payload(participant: Any) -> dict[str, Any]:
    """Convert the adapter's participant object to a small JSON-safe record."""
    active = getattr(participant, "active_speaking", None)
    if active is None and getattr(participant, "is_speaking", False):
        # Legacy detection is based on unvalidated UI selectors. Do not promote it
        # to evidence until those indicators have been validated against X's UI.
        active = None
    return {
        "username": getattr(participant, "username", "") or None,
        "display_name": getattr(participant, "display_name", "") or None,
        "avatar_url": getattr(participant, "avatar_url", "") or None,
        "role": getattr(participant, "role", "unknown") or "unknown",
        "active_speaking": active,
        "active_speaking_evidence": getattr(participant, "active_speaking_evidence", None),
        "source": getattr(participant, "source", "unknown"),
        "x_user_id": getattr(participant, "x_user_id", "") or None,
        "is_muted": getattr(participant, "is_muted", None),
    }


@dataclass
class SpeakerObservationTracker:
    """Build observations/events and debounce participant state transitions."""

    space_id: str
    start_debounce_ms: int = 10_000
    stop_debounce_ms: int = 10_000
    _state: dict[str, dict[str, Any]] = field(default_factory=dict)
    _pending: dict[tuple[str, str], tuple[Any, float, int]] = field(default_factory=dict)

    def observe(
        self,
        participants: list[dict[str, Any]],
        *,
        observed_at: datetime,
        recording_time: float,
        source: str,
        complete_snapshot: bool = False,
        active_indicator_validated: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        current = {
            p.get("username") or p.get("display_name") or f"unknown-{i}": p
            for i, p in enumerate(participants)
        }
        observation = {
            "schema_version": 1,
            "observed_at": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "recording_time": round(max(0.0, recording_time), 3),
            "space_time": None,
            "space_id": self.space_id,
            "source": source,
            "complete_snapshot": bool(complete_snapshot),
            "active_speaking_observable": bool(active_indicator_validated),
            "participants": participants,
        }
        events: list[dict[str, Any]] = []
        now = max(0.0, recording_time)
        for identity, person in current.items():
            old = self._state.get(identity)
            if old is None:
                event_type = "host_detected" if person.get("role") == "host" else "participant_seen"
                events.append(self._event(event_type, identity, person, recording_time, observed_at, source))
                self._state[identity] = {"person": person, "last_seen": now}
                continue
            was_present = old.get("present", True)
            if complete_snapshot and not was_present:
                if self._debounced(identity, "presence", True, now, self.start_debounce_ms):
                    events.append(
                        self._event(
                            "participant_joined", identity, person, recording_time, observed_at, source
                        )
                    )
                    old["present"] = True
            elif was_present:
                self._pending.pop((identity, "presence"), None)
            old_person = old["person"]
            old["last_seen"] = now
            if old_person.get("role") == person.get("role"):
                self._pending.pop((identity, "role"), None)
            elif person.get("role") in {"speaker", "listener", "host", "cohost"}:
                event_type = {
                    "host": "host_detected",
                    "cohost": "cohost_detected",
                    "speaker": "became_speaker",
                    "listener": "became_listener",
                }[person["role"]]
                delay = self.stop_debounce_ms if person["role"] == "listener" else self.start_debounce_ms
                if self._debounced(identity, "role", person["role"], now, delay):
                    events.append(
                        self._event(event_type, identity, person, recording_time, observed_at, source)
                    )
                    old["person"] = person
            # Only an explicitly validated DOM signal can generate speech events.
            if active_indicator_validated and person.get("active_speaking") is not None:
                active = bool(person["active_speaking"])
                previous = old_person.get("active_speaking")
                if previous == active:
                    self._pending.pop((identity, "speech"), None)
                else:
                    delay = self.start_debounce_ms if active else self.stop_debounce_ms
                    if self._debounced(identity, "speech", active, now, delay):
                        name = "speaking_started" if active else "speaking_stopped"
                        events.append(
                            self._event(name, identity, person, recording_time, observed_at, source)
                        )
                        old["person"] = {**old["person"], "active_speaking": active}
            old["person"] = {
                **old["person"],
                **{
                    k: v
                    for k, v in person.items()
                    if k not in {"active_speaking", "role"} or old["person"].get(k) == v
                },
            }
        # Only infer join/leave from a source explicitly known to be complete.
        if complete_snapshot:
            for identity in list(self._state):
                if identity not in current and self._state[identity].get("present", True):
                    old_person = self._state[identity]["person"]
                    if self._debounced(identity, "presence", False, now, self.stop_debounce_ms):
                        events.append(
                            self._event(
                                "participant_left", identity, old_person, recording_time, observed_at, source
                            )
                        )
                        self._state[identity]["present"] = False
        return observation, events

    def _debounced(self, identity: str, field_name: str, value: Any, now: float, delay_ms: int) -> bool:
        key = (identity, field_name)
        pending = self._pending.get(key)
        if pending is None or pending[0] != value:
            self._pending[key] = (value, now, 1)
            return delay_ms == 0
        self._pending[key] = (value, pending[1], pending[2] + 1)
        return (now - pending[1]) * 1000 >= delay_ms

    def _event(
        self,
        event: str,
        identity: str,
        person: dict[str, Any],
        recording_time: float,
        observed_at: datetime,
        source: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "space_id": self.space_id,
            "recording_time": round(max(0.0, recording_time), 3),
            "space_time": None,
            "wall_time": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "event": event,
            "username": person.get("username"),
            "display_name": person.get("display_name"),
            "avatar_url": person.get("avatar_url"),
            "role": person.get("role", "unknown"),
            "source": source,
            "confidence": 1.0 if source == "x_gql" else None,
        }


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
