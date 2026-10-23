"""Shared durable ownership and lease rules for API and isolated worker jobs."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_

from xsa.db import Job, get_session

OWNER_ID = f"api-{os.getpid()}-{uuid4().hex}"
LEASE_SECONDS = 120


def claim(job: Job, owner_id: str = OWNER_ID) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    job.owner_id = owner_id
    job.attempt_id = uuid4().hex
    job.claimed_at = now
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)


def renew(job_id: int, owner_id: str = OWNER_ID) -> bool:
    now = datetime.now(UTC).replace(tzinfo=None)
    with get_session() as db:
        updated = (
            db.query(Job)
            .filter(Job.id == job_id, Job.owner_id == owner_id, Job.state.in_(("running", "cancelling")))
            .update({Job.heartbeat_at: now, Job.lease_expires_at: now + timedelta(seconds=LEASE_SECONDS)})
        )
        db.commit()
        return bool(updated)


def recover_expired_jobs() -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    with get_session() as db:
        rows = (
            db.query(Job)
            .filter(
                Job.state.in_(("running", "cancelling")),
                or_(Job.lease_expires_at < now, Job.lease_expires_at.is_(None)),
            )
            .all()
        )
        for job in rows:
            job.state = "queued"
            job.owner_id = None
            job.worker_id = None
            job.lease_expires_at = None
            job.error = "Recovered expired job owner; previous attempt interrupted"
        db.commit()
        return len(rows)
