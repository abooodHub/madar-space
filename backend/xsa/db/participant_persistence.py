"""Idempotent persistence helpers for identities observed in X Spaces."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from xsa.db.models import Participant, SpaceParticipant

KNOWN_ROLES = {"host", "cohost", "speaker", "listener"}


def persist_space_participant(
    db: Session,
    *,
    space_db_id: int,
    evidence: dict[str, Any],
    observed_at: datetime,
) -> tuple[Participant | None, bool, bool, bool]:
    """Upsert one identity and its Space relationship.

    Returns `(participant, participant_created, relationship_created, conflict)`.
    Unknown roles remain `unknown`; a role label alone is never treated as
    proof that the person was speaking acoustically.
    """
    username = _clean(evidence.get("username"))
    x_user_id = _clean(evidence.get("x_user_id"))
    periscope_id = _clean(evidence.get("periscope_id"))
    display_name = _clean(evidence.get("display_name"))
    avatar_url = _clean(evidence.get("avatar_url") or evidence.get("avatar"))
    if not (username or x_user_id or periscope_id):
        return None, False, False, False

    matches = []
    clauses = []
    if x_user_id:
        clauses.append(Participant.x_user_id == x_user_id)
    if periscope_id:
        clauses.append(Participant.periscope_id == periscope_id)
    if username:
        clauses.append(func.lower(Participant.username) == username.casefold())
    if clauses:
        matches = list(db.scalars(select(Participant).where(or_(*clauses))).all())

    conflict = len({row.id for row in matches}) > 1
    participant = None
    if matches:
        if x_user_id:
            participant = next((row for row in matches if row.x_user_id == x_user_id), None)
        participant = participant or next(
            (
                row
                for row in matches
                if username and row.username and row.username.casefold() == username.casefold()
            ),
            None,
        )
        participant = participant or next(
            (row for row in matches if periscope_id and row.periscope_id == periscope_id), None
        )
        participant = participant or matches[0]

    participant_created = participant is None
    if participant is None:
        participant = Participant(
            x_user_id=x_user_id,
            periscope_id=periscope_id,
            username=username,
            display_name=display_name,
            avatar_url=avatar_url,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
        db.add(participant)
        db.flush()
    else:
        if x_user_id and not participant.x_user_id:
            participant.x_user_id = x_user_id
        if periscope_id and not participant.periscope_id:
            participant.periscope_id = periscope_id
        if username and not conflict:
            participant.username = username
        if display_name and not conflict:
            participant.display_name = display_name
        if avatar_url and not conflict:
            participant.avatar_url = avatar_url
        if observed_at < participant.first_seen_at:
            participant.first_seen_at = observed_at
        if participant.last_seen_at is None or observed_at > participant.last_seen_at:
            participant.last_seen_at = observed_at

    relationship = db.scalar(
        select(SpaceParticipant).where(
            SpaceParticipant.space_id == space_db_id,
            SpaceParticipant.participant_id == participant.id,
        )
    )
    relationship_created = relationship is None
    role = _normalize_role(evidence.get("role"))
    is_muted = evidence.get("is_muted")
    if relationship is None:
        relationship = SpaceParticipant(
            space_id=space_db_id,
            participant_id=participant.id,
            role=role,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            is_muted=is_muted if isinstance(is_muted, bool) else None,
        )
        db.add(relationship)
        db.flush()
    else:
        if observed_at < relationship.first_seen_at:
            relationship.first_seen_at = observed_at
        is_latest = relationship.last_seen_at is None or observed_at >= relationship.last_seen_at
        if is_latest and role != "unknown":
            relationship.role = role
        if is_latest and isinstance(is_muted, bool):
            relationship.is_muted = is_muted
        if relationship.last_seen_at is None or observed_at > relationship.last_seen_at:
            relationship.last_seen_at = observed_at

    return participant, participant_created, relationship_created, conflict


def _normalize_role(value: Any) -> str:
    role = str(value or "unknown").strip().casefold().replace("-", "_")
    if role in {"admin", "host", "creator"}:
        return "host"
    if role in {"cohost", "co_host", "co_host_admin"}:
        return "cohost"
    return role if role in KNOWN_ROLES else "unknown"


def _clean(value: Any) -> str | None:
    result = str(value).strip() if value is not None else ""
    return result or None
