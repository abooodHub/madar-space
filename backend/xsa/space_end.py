"""Conservative, explicit-evidence state machine for one X Space session."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class SpaceEndState(StrEnum):
    LIVE = "LIVE"
    SUSPECTED_ENDED = "SUSPECTED_ENDED"
    ENDED = "ENDED"
    DISCONNECTED = "DISCONNECTED"
    UNKNOWN = "UNKNOWN"


def should_stop_unverified_silence(
    *,
    silence_seconds: float,
    space_state: SpaceEndState,
    unknown_seconds: float,
    silence_limit_seconds: float = 30 * 60,
    unknown_grace_seconds: float = 5 * 60,
) -> bool:
    """Bound runaway captures when sustained silence coincides with no live-state proof.

    This is deliberately not explicit Space-end evidence. Callers must preserve
    the recording as an unverified/interrupted capture rather than claiming X
    reported the Space ended.
    """
    return (
        silence_seconds >= silence_limit_seconds
        and unknown_seconds >= unknown_grace_seconds
        and space_state in {SpaceEndState.UNKNOWN, SpaceEndState.DISCONNECTED}
    )


@dataclass
class SpaceEndDetector:
    """Debounce explicit end evidence; silence and speaker activity are irrelevant."""

    confirm_seconds: float = 2.0
    state: SpaceEndState = SpaceEndState.UNKNOWN
    last_checked_at: str | None = None
    signal_source: str | None = None
    evidence: str | None = None
    confidence: float | None = None
    suspected_since: float | None = None
    detected_at: str | None = None
    space_id: str | None = None
    source_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    confirmation_count: int = 0
    last_confirmed_live_monotonic: float | None = None

    def observe(self, observation: dict[str, Any], now: float) -> SpaceEndState:
        self.last_checked_at = datetime.now(UTC).isoformat()
        raw_state = str(observation.get("state", "UNKNOWN")).upper()
        source = observation.get("source")
        evidence = observation.get("evidence")
        confidence = observation.get("confidence")
        observed_id = observation.get("space_id")
        if self.space_id and observed_id and observed_id != self.space_id:
            return self.state
        if self.space_id and not observed_id and raw_state in {"LIVE", "ENDED"}:
            return self.state
        if observed_id and not self.space_id:
            self.space_id = str(observed_id)

        if self.state == SpaceEndState.ENDED:
            return self.state

        if raw_state == "NOT_CHECKED_THIS_CYCLE":
            return self.state
        for key, check in observation.get("source_checks", {}).items():
            entry = self.source_evidence.setdefault(key, {})
            entry["last_poll_state"] = check["state"]
            if check["state"] != "NOT_CHECKED_THIS_CYCLE":
                entry["last_checked_at"] = self.last_checked_at
                entry["last_state"] = check["state"]
            if (
                check["state"] == "ENDED"
                and check.get("space_id") == self.space_id
                and check.get("confidence", 0) >= 0.95
            ):
                if not entry.get("confirmation_count"):
                    entry["first_seen_at"] = self.last_checked_at
                entry.update(
                    last_seen_at=self.last_checked_at,
                    last_ended_evidence_at=self.last_checked_at,
                    space_id=self.space_id,
                    confirmation_count=entry.get("confirmation_count", 0) + 1,
                )
        source_key = str(source or "unspecified")
        record = self.source_evidence.setdefault(source_key, {})
        record.update(last_state=raw_state, last_checked_at=self.last_checked_at, space_id=self.space_id)
        if raw_state == SpaceEndState.LIVE and float(confidence or 0) < 0.95:
            if (
                self.state == SpaceEndState.LIVE
                and self.last_confirmed_live_monotonic is not None
                and now - self.last_confirmed_live_monotonic <= 15
            ):
                return self.state
            raw_state = "UNKNOWN"

        if raw_state == SpaceEndState.LIVE:
            # Only an authoritative, Space-bound LIVE observation contradicts
            # existing end evidence. A visible dock is not proof of a live Space.
            if self.suspected_since is not None and not (
                source_key == "metadata" and float(confidence or 0) >= 0.95
            ):
                return self.state
            self.suspected_since = None
            self.last_confirmed_live_monotonic = now
            self.signal_source = source_key
            self.evidence = str(evidence) if evidence else None
            self.confidence = float(confidence) if confidence is not None else None
            self.confirmation_count = 0
            for item in self.source_evidence.values():
                item["confirmation_count"] = 0
            self.state = SpaceEndState.LIVE
            return self.state

        if raw_state == SpaceEndState.DISCONNECTED:
            if self.suspected_since is not None:
                return self.state
            self.state = SpaceEndState.DISCONNECTED
            return self.state

        if raw_state == SpaceEndState.ENDED:
            if float(confidence if confidence is not None else 1) < 0.95:
                return self.state
            self.signal_source = source_key
            self.evidence = str(evidence) if evidence else None
            self.confidence = float(confidence) if confidence is not None else None
            if observation.get("source_checks", {}).get(source_key, {}).get("state") != "ENDED":
                if not record.get("confirmation_count"):
                    record["first_seen_at"] = self.last_checked_at
                record.update(
                    last_seen_at=self.last_checked_at,
                    last_ended_evidence_at=self.last_checked_at,
                    confirmation_count=record.get("confirmation_count", 0) + 1,
                )
            self.confirmation_count += 1
            if self.suspected_since is None:
                self.suspected_since = now
                self.state = SpaceEndState.SUSPECTED_ENDED
            elif self.confirmation_count >= 2 and now - self.suspected_since >= self.confirm_seconds:
                self.state = SpaceEndState.ENDED
                self.detected_at = datetime.now(UTC).isoformat()
            else:
                self.state = SpaceEndState.SUSPECTED_ENDED
            return self.state

        if self.suspected_since is not None:
            return self.state
        self.state = SpaceEndState.UNKNOWN
        return self.state

    def snapshot(self) -> dict[str, Any]:
        return {
            "space_state": self.state.value,
            "end_detector_state": self.state.value,
            "last_space_state_check": self.last_checked_at,
            "end_signal_source": self.signal_source,
            "end_signal_evidence": self.evidence,
            "end_signal_confidence": self.confidence,
            "end_detected_at": self.detected_at,
            "end_evidence_by_source": self.source_evidence,
            "end_confirmation_count": self.confirmation_count,
            "locked_space_id": self.space_id,
        }
