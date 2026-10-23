"""FastAPI application: REST API for the dashboard and monitoring control."""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Literal

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, text

from xsa.api.space_status import describe_space_status
from xsa.blocking import run_blocking
from xsa.browser import XBrowserAdapter
from xsa.config import get_settings
from xsa.db import Job, MonitoredAccount, Space, SpeakerEvent, SystemEvent, get_session, init_db
from xsa.logging_setup import get_logger, setup_logging
from xsa.maintenance_gate import arm_maintenance, maintenance_active, release_maintenance
from xsa.manual_recording import space_id_from_url
from xsa.monitor import AccountMonitor
from xsa.monitor.configuration import load_monitoring_config, save_monitoring_config
from xsa.paths import SpacePaths
from xsa.recorder.media import (
    playback_readiness_for_paths,
    playback_readiness_from_metadata,
    read_playback_source_manifest,
)
from xsa.runtime_metrics import application_metrics
from xsa.worker_status import read_worker_status

log = get_logger("API")

_cfg = get_settings()
_adapter: XBrowserAdapter | None = None
_monitor: AccountMonitor | None = None
_monitor_task: asyncio.Task | None = None
_maintenance_watch_task: asyncio.Task | None = None


async def _start_monitoring() -> None:
    """Start browser and monitor only after the persistent boot gate is clear."""
    global _adapter, _monitor, _monitor_task
    if maintenance_active(_cfg.db_path.parent):
        return
    if _monitor_task and not _monitor_task.done():
        return
    _adapter = XBrowserAdapter(
        profile_dir=_cfg.profile_dir,
        display=_cfg.master_display,
        pulse_server=_cfg.pulse_server,
        pulse_sink="xsa_ui",
    )
    await _adapter.start()
    _monitor = AccountMonitor(_adapter, _cfg)
    resume_jobs = getattr(_monitor, "resume_queued_post_processing", None)
    resumed_jobs = resume_jobs() if callable(resume_jobs) else 0
    if resumed_jobs:
        log.info("Resumed durable post-processing jobs", count=resumed_jobs)
    _monitor_task = asyncio.create_task(_monitor.start())
    log.info("Monitor started")


async def _watch_for_maintenance_release() -> None:
    """Wait without starting capture services, then resume after gate removal."""
    while maintenance_active(_cfg.db_path.parent):
        await asyncio.sleep(0.5)
    await _start_monitoring()


def recover_interrupted_join_rows() -> int:
    """Make JOINING rows terminal when their in-memory runtime task died at restart."""
    now = datetime.now(UTC).replace(tzinfo=None)
    with get_session() as db:
        rows = db.query(Space).filter(Space.state == "joining").all()
        for row in rows:
            row.state = "error"
            row.join_outcome = "startup_recovery_interrupted_join"
            row.join_last_error = "Application restarted before join completed; runtime task no longer exists"
            if row.joining_started_at:
                row.join_age_seconds = max(0.0, (now - row.joining_started_at).total_seconds())
            row.join_checkpoint = row.join_checkpoint or "startup_recovery"
            row.join_checkpoint_at = now
            row.last_progress_at = now
            db.add(
                SystemEvent(
                    event_time=now,
                    category="recording",
                    level="error",
                    message="Stale join recovered after application restart",
                    space_id=row.id,
                    details={
                        "reason": "startup_recovery_interrupted_join",
                        "last_checkpoint": row.join_checkpoint,
                        "join_age_seconds": row.join_age_seconds,
                    },
                )
            )
        db.commit()
        return len(rows)


def recover_interrupted_post_processing_jobs() -> int:
    """Requeue post-processing work whose in-memory task ended with the process."""
    with get_session() as db:
        jobs = (
            db.query(Job)
            .filter(
                Job.job_type.in_(("post_process", "prepare_media")),
                Job.state.in_(("running", "cancelling")),
            )
            .all()
        )
        for job in jobs:
            job.state = "cancelled" if job.state == "cancelling" else "queued"
            job.worker_id = None
            job.error = "Recovered after application restart; resuming post-processing"
            job.progress = 0.0
        db.commit()
        return len(jobs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _adapter, _monitor, _monitor_task, _maintenance_watch_task
    setup_logging(_cfg.log_level, _cfg.data_dir / "logs")
    init_db(_cfg.db_path)
    for key, value in load_monitoring_config(_cfg.db_path.parent).items():
        setattr(_cfg, key, value)
    # A process-local post-processing task cannot survive a restart. Clear its
    # transitional Space state even when the maintenance gate delays services.
    with get_session() as db:
        interrupted_processing_rows = (
            db.query(Space)
            .filter(Space.state == "processing")
            .update({Space.state: "error"}, synchronize_session=False)
        )
        db.commit()
    if interrupted_processing_rows:
        log.warning(
            "Marked interrupted post-processing Spaces for recovery", count=interrupted_processing_rows
        )
    interrupted_post_processing = recover_interrupted_post_processing_jobs()
    from xsa.job_ownership import recover_expired_jobs

    recover_expired_jobs()
    if interrupted_post_processing:
        log.warning("Requeued interrupted post-processing jobs", count=interrupted_post_processing)
    with get_session() as db:
        interrupted_recording_rows = (
            db.query(Space)
            .filter(Space.state.in_(("recording", "finalizing")))
            .update({Space.state: "error"}, synchronize_session=False)
        )
        db.commit()
    interrupted_joins = recover_interrupted_join_rows()
    interrupted = interrupted_recording_rows + interrupted_joins
    if interrupted:
        log.warning("Marked interrupted Spaces for recovery", count=interrupted)
    if maintenance_active(_cfg.db_path.parent):
        # This check is deliberately before browser construction and monitor task
        # creation: the persistent lock must prevent all joins at process boot.
        _adapter = None
        _monitor = None
        _monitor_task = None
        log.warning("Maintenance lock present at boot; API is starting without browser or monitor")
        _maintenance_watch_task = asyncio.create_task(_watch_for_maintenance_release())
    else:
        # Recording state cannot survive a process restart; preserve a
        # diagnosable terminal result before the monitor starts again.
        log.info("XSpace Analyzer starting", data_dir=str(_cfg.data_dir))
        await _start_monitoring()

    try:
        yield
    finally:
        if _maintenance_watch_task:
            _maintenance_watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await _maintenance_watch_task
            _maintenance_watch_task = None

        log.info("Shutting down")
        if _monitor:
            await _monitor.stop()
        if _monitor_task:
            _monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await _monitor_task
        if _adapter:
            await _adapter.close()


app = FastAPI(title="XSpace Analyzer API", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def guard_browser_writes(request: Request, call_next):
    """Reject cross-origin writes to the loopback operator UI."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        from urllib.parse import urlsplit

        source = request.headers.get("origin") or request.headers.get("referer")
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
        if source:
            try:
                parsed = urlsplit(source)
                valid = (
                    parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "localhost", "app"}
                    and parsed.port == 8765
                )
            except ValueError:
                valid = False
            if not valid:
                return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AccountIn(BaseModel):
    username: str
    notes: str | None = None


class AccountOut(BaseModel):
    id: int
    username: str
    display_name: str | None
    avatar_url: str | None = None
    active: bool
    priority: str = "P2"
    added_at: datetime
    last_checked_at: datetime | None
    next_check_at: datetime | None = None
    last_poll_outcome: str | None = None
    last_poll_duration_seconds: float | None = None
    last_poll_request_count: int | None = None
    poll_failure_streak: int = 0
    notes: str | None
    tags: list[str] = []
    spaces_count: int = 0
    last_space_at: datetime | None = None
    is_live: bool = False
    is_recording: bool = False

    model_config = {"from_attributes": True}


class AccountTagsIn(BaseModel):
    tags: list[str]


class AccountPriorityIn(BaseModel):
    priority: Literal["P1", "P2", "P3"]


class AccountBulkIn(BaseModel):
    usernames: list[str]
    active: bool


class MonitoringConfigIn(BaseModel):
    max_active_accounts: int = Field(ge=1, le=59)
    poll_interval_seconds: int = Field(ge=300, le=3600)
    max_graphql_requests_per_cycle: int = Field(ge=1, le=1000)


class AdminStopIn(BaseModel):
    reason: str = "Operator stopped the recording"


class AdminMaintenanceIn(BaseModel):
    stop_recordings: bool = False
    reason: str = "Maintenance deployment"


class SpaceOut(BaseModel):
    id: int
    space_id: str
    title: str | None
    state: str
    url: str | None
    detected_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    recording_started_at: datetime | None

    model_config = {"from_attributes": True}


def _account_out(account: MonitoredAccount) -> AccountOut:
    return AccountOut(
        id=account.id,
        username=account.username,
        display_name=account.display_name,
        avatar_url=account.avatar_url,
        active=account.active,
        priority=account.priority,
        added_at=account.added_at,
        last_checked_at=account.last_checked_at,
        next_check_at=account.next_check_at,
        last_poll_outcome=account.last_poll_outcome,
        last_poll_duration_seconds=account.last_poll_duration_seconds,
        last_poll_request_count=account.last_poll_request_count,
        poll_failure_streak=account.poll_failure_streak or 0,
        notes=account.notes,
        tags=[tag.strip() for tag in (account.tags or "").split(",") if tag.strip()],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    maintenance = maintenance_active(_cfg.db_path.parent)
    browser_ok = bool(
        _adapter
        and _adapter._context
        and not _adapter._context.is_closed()
        and _monitor_task
        and not _monitor_task.done()
    )
    active_sessions = _monitor._active_sessions if _monitor else {}
    active = []
    for space_id, session in active_sessions.items():
        try:
            proc = getattr(session._recorder, "_proc", None)
            if proc and proc.poll() is None:
                active.append(space_id)
        except Exception:
            active.append(space_id)
    database = {"healthy": False, "quick_check": "unavailable", "foreign_key_violations": None}
    joining: list[dict] = []
    joining_runtime: dict[str, object] = {}
    jobs: dict[str, int] = {}
    jobs_by_type: dict[str, dict[str, int]] = {}
    account_health = {"total": 0, "active": 0, "by_priority": {}}
    gpu_job = None
    orphan_recordings = []
    expired_jobs = []
    try:
        with get_session() as db:
            quick_check = db.execute(text("PRAGMA quick_check(1)")).scalar()
            fk_rows = db.execute(text("PRAGMA foreign_key_check")).all()
            database = {
                "healthy": quick_check == "ok" and not fk_rows,
                "quick_check": quick_check,
                "foreign_key_violations": len(fk_rows),
            }
            account_groups = (
                db.query(MonitoredAccount.active, MonitoredAccount.priority, func.count(MonitoredAccount.id))
                .group_by(MonitoredAccount.active, MonitoredAccount.priority)
                .all()
            )
            account_health = {
                "total": sum(group[2] for group in account_groups),
                "active": sum(group[2] for group in account_groups if group[0]),
                "by_priority": {
                    priority: sum(group[2] for group in account_groups if group[1] == priority)
                    for priority in ("P1", "P2", "P3")
                },
            }
            now = datetime.now(UTC).replace(tzinfo=None)
            orphan_recordings = [
                row.space_id
                for row in db.query(Space).filter(Space.state.in_(("recording", "finalizing"))).all()
                if row.space_id not in active_sessions
            ]
            expired_jobs = [
                row.id
                for row in db.query(Job).filter(Job.state.in_(("running", "cancelling"))).all()
                if not row.lease_expires_at or row.lease_expires_at < now
            ]
            for row in db.query(Space).filter(Space.state == "joining").all():
                task = _monitor._active_tasks.get(row.space_id) if _monitor else None
                runtime_alive = bool(task and not task.done())
                started = row.joining_started_at
                age = max(0.0, (now - started).total_seconds()) if started else None
                deadline = row.join_deadline_seconds or _cfg.session_start_timeout_seconds
                last_progress_age = (
                    max(0.0, (now - row.last_progress_at).total_seconds()) if row.last_progress_at else None
                )
                session = _monitor._active_sessions.get(row.space_id) if _monitor else None
                joining_runtime[row.space_id] = session
                ffmpeg_exists = False
                if session:
                    try:
                        proc = getattr(session._recorder, "_proc", None)
                        ffmpeg_exists = bool(proc and proc.poll() is None)
                    except Exception:
                        ffmpeg_exists = True
                joining.append(
                    {
                        "space_id": row.space_id,
                        "state": row.state,
                        "joining_started_at": started.isoformat() if started else None,
                        "join_age_seconds": round(age, 1) if age is not None else None,
                        "join_deadline_seconds": deadline,
                        "join_checkpoint": row.join_checkpoint,
                        "join_checkpoint_at": row.join_checkpoint_at.isoformat()
                        if row.join_checkpoint_at
                        else None,
                        "last_progress_at": row.last_progress_at.isoformat()
                        if row.last_progress_at
                        else None,
                        "browser_url": row.join_browser_url or row.url,
                        "browser_title": row.join_browser_title,
                        "space_state": row.join_space_state,
                        "host_verification_state": row.join_host_verification_state,
                        "anonymous_listen_state": row.join_anonymous_listen_state,
                        "last_error": row.join_last_error,
                        "runtime_task_alive": runtime_alive,
                        "last_progress_seconds_ago": round(last_progress_age, 1)
                        if last_progress_age is not None
                        else None,
                        "ffmpeg_exists": ffmpeg_exists,
                    }
                )
            jobs = dict(db.execute(text("SELECT state, COUNT(*) FROM jobs GROUP BY state")).all())
            for job_type, job_state, count in db.execute(
                text("SELECT job_type, state, COUNT(*) FROM jobs GROUP BY job_type, state")
            ).all():
                jobs_by_type.setdefault(job_type, {})[job_state] = count
            gpu_row = (
                db.execute(
                    text(
                        "SELECT id, job_type, state, space_id FROM jobs "
                        "WHERE job_type='diarize' AND state='running' ORDER BY id DESC LIMIT 1"
                    )
                )
                .mappings()
                .first()
            )
            gpu_job = dict(gpu_row) if gpu_row else None
    except Exception as exc:
        log.error("Health database probe failed", error=type(exc).__name__)
    for info in joining:
        session = joining_runtime.get(info["space_id"])
        page = getattr(getattr(session, "adapter", None), "_page", None)
        try:
            if page and not page.is_closed():
                info["browser_url"] = page.url
                info["browser_title"] = await asyncio.wait_for(page.title(), timeout=0.75)
        except Exception:
            pass
    disk_path = _cfg.data_dir if _cfg.data_dir.exists() else _cfg.data_dir.parent
    disk_usage = psutil.disk_usage(str(disk_path))
    free_gb = round(disk_usage.free / 1024**3, 2)
    disk = {
        "free_bytes": disk_usage.free,
        "free_gb": free_gb,
        "used_percent": disk_usage.percent,
        "healthy": free_gb >= _cfg.min_free_disk_gb,
        "minimum_free_gb": _cfg.min_free_disk_gb,
    }
    active_details = []
    for sid in active:
        session = _monitor._active_sessions.get(sid) if _monitor else None
        if not session:
            continue
        rec_status = session._recorder.get_status()
        active_details.append(
            {
                "space_id": sid,
                "host": session.host_username,
                "title": session.space_title,
                "state": "recording" if rec_status.state == "recording" else rec_status.state,
                "started_at": session._started_at.isoformat() if session._started_at else None,
                "duration_s": round(rec_status.duration_seconds, 1),
                "file_bytes": rec_status.file_bytes,
                "browser_connected": bool(session.adapter and session.adapter._context),
                "end_detector": session.end_detector.snapshot(),
                "audio_silence": session._recorder.get_audio_silence_status(),
                "anonymous_listen_state": session.adapter.join_anonymous_listen_state,
                "host_verification_state": session.adapter.join_host_verification_state,
                "ffmpeg_healthy": rec_status.health,
                "last_heartbeat": session.speaker_collector_health.get("last_success_at"),
            }
        )
    try:
        from xsa.recorder.slot_pool import get_slot_pool

        pool = get_slot_pool(_cfg.max_concurrent_recordings)
        busy_slots = [slot.slot_id for slot in pool._slots.values() if slot.is_busy]
        active_slots = {
            getattr(session.slot, "slot_id", None)
            for session in (_monitor._active_sessions.values() if _monitor else [])
        }
        slot_usage = {
            "capacity": pool.max_slots,
            "busy": len(busy_slots),
            "available": pool.max_slots - len(busy_slots),
            "quarantined": [slot_id for slot_id in busy_slots if slot_id not in active_slots],
        }
    except Exception as exc:
        log.warning("Recorder slot health probe failed", error=type(exc).__name__)
        slot_usage = {
            "capacity": _cfg.max_concurrent_recordings,
            "busy": None,
            "available": None,
            "quarantined": [],
        }
    payload = {
        "status": "maintenance"
        if maintenance and not _monitor_task
        else "ok"
        if browser_ok and database["healthy"] and disk["healthy"]
        else "degraded",
        "browser_connected": browser_ok,
        "monitor_running": bool(_monitor_task and not _monitor_task.done()),
        "active_recordings": active,
        "joining_sessions": joining,
        "recording_sessions": active_details,
        "active_post_processing": [
            sid
            for sid, task in _monitor._post_processing_tasks.items()
            if not getattr(task, "_xsa_waiting", False)
        ]
        if _monitor
        else [],
        "processing_jobs": jobs,
        "processing_jobs_by_type": jobs_by_type,
        "monitoring": {
            **account_health,
            "max_active_accounts": _cfg.max_active_accounts,
            "poll_interval_seconds": _cfg.poll_interval_seconds,
            "max_graphql_requests_per_cycle": _cfg.max_graphql_requests_per_cycle,
            "last_cycle_graphql_requests": getattr(_monitor, "_last_cycle_requests", None),
            "last_five_minute_graphql_requests": (
                _monitor._graphql_budget.used()
                if _monitor and getattr(_monitor, "_graphql_budget", None)
                else None
            ),
            "x_authentication_required": bool(
                _monitor
                and getattr(_monitor, "_graphql_budget", None)
                and _monitor._graphql_budget.authentication_required()
            ),
        },
        "gpu_job": gpu_job,
        "recording_ffmpeg_count": _recording_ffmpeg_count(),
        "slot_usage": slot_usage,
        "runtime_metrics": application_metrics(),
        "database": database,
        "disk": disk,
        "maintenance_mode": maintenance,
        "utc": datetime.now(UTC).isoformat(),
    }
    sessions = _monitor._active_sessions if _monitor else {}
    payload["speaker_collectors"] = {
        sid: dict(session.speaker_collector_health) for sid, session in sessions.items()
    }
    payload["speaker_collector_running"] = any(
        item.get("speaker_collector_running") for item in payload["speaker_collectors"].values()
    )
    payload["space_end_detectors"] = {
        sid: {
            **session.end_detector.snapshot(),
            "recording_stop_at": session.recording_stopped_at.isoformat()
            if session.recording_stopped_at
            else None,
            "end_tail_duration": session.end_tail_duration,
        }
        for sid, session in sessions.items()
    }
    capture_issues = [{"space_id": sid, "reason": "orphan_recording_row"} for sid in orphan_recordings]
    from xsa.browser.adapter import viewer_retry_remaining

    retry_after = viewer_retry_remaining()
    payload["x_access"] = {
        "status": (
            "rate_limited"
            if retry_after > 0
            else "authentication_required"
            if _monitor and _monitor._graphql_budget.authentication_required()
            else "not_rate_limited"
        ),
        "operation": "Viewer",
        "retry_after_seconds": round(retry_after, 1),
    }
    if retry_after > 0:
        capture_issues.append(
            {"reason": "x_viewer_rate_limited", "retry_after_seconds": round(retry_after, 1)}
        )
    for item in joining:
        if not item["runtime_task_alive"] or (item["join_age_seconds"] or 0) > item["join_deadline_seconds"]:
            capture_issues.append({"space_id": item["space_id"], "reason": "stale_join"})
        elif (item["last_progress_seconds_ago"] or 0) > 30:
            capture_issues.append(
                {
                    "space_id": item["space_id"],
                    "reason": "join_progress_stalled",
                    "last_progress_seconds_ago": item["last_progress_seconds_ago"],
                    "checkpoint": item["join_checkpoint"],
                }
            )
    for item in active_details:
        sid = item["space_id"]
        audio = item.get("audio_silence", {})
        if not item.get("ffmpeg_healthy"):
            capture_issues.append({"space_id": sid, "reason": "recorder_unhealthy"})
        if audio.get("audio_measurement", "unavailable") != "healthy" and item.get("duration_s", 0) > 15:
            capture_issues.append({"space_id": sid, "reason": "audio_measurement_unavailable"})
        if audio.get("silence_suspected"):
            capture_issues.append({"space_id": sid, "reason": "prolonged_silence"})
        stamp = item.get("last_heartbeat")
        if stamp:
            age = (datetime.now(UTC) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
            if age > 30:
                capture_issues.append({"space_id": sid, "reason": "stale_heartbeat", "age_seconds": age})
        if (
            item.get("end_detector", {}).get("space_state") == "ENDED"
            and not sessions[sid].recording_stopped_at
        ):
            capture_issues.append({"space_id": sid, "reason": "ended_but_capturing"})
    roles = _ffmpeg_role_counts()
    if roles["recording"] != len(active):
        capture_issues.append({"reason": "recorder_ownership_count_mismatch"})
    payload["service_health"] = {
        "status": "maintenance"
        if maintenance
        else "ok"
        if browser_ok and database["healthy"] and payload["monitor_running"] and disk["healthy"]
        else "degraded",
    }
    payload["capture_health"] = {
        "status": "degraded" if capture_issues else "ok",
        "issues": capture_issues,
        "ffmpeg_roles": roles,
        "sessions": active_details,
    }
    diarization_jobs = jobs_by_type.get("diarize", {})
    diarization_worker = read_worker_status(_cfg.db_path.parent)
    payload["diarization_health"] = {
        "status": "blocked"
        if diarization_jobs.get("waiting_for_configuration")
        else "degraded"
        if diarization_jobs.get("failed")
        else "running"
        if diarization_jobs.get("running")
        else "queued"
        if diarization_jobs.get("queued")
        else "unknown",
        "reason": diarization_worker.get("reason")
        or (
            "worker_reported_configuration_block"
            if diarization_jobs.get("waiting_for_configuration")
            else None
        ),
        "worker": diarization_worker,
        "waiting_jobs": sum(
            diarization_jobs.get(state, 0)
            for state in ("queued", "retrying", "waiting_for_resources", "waiting_for_configuration")
        ),
        "running_jobs": diarization_jobs.get("running", 0),
    }
    payload["processing_health"] = {
        "status": "degraded"
        if expired_jobs
        or jobs_by_type.get("post_process", {}).get("failed")
        or jobs_by_type.get("prepare_media", {}).get("failed")
        else "ok",
        "jobs": jobs_by_type,
        "expired_running_leases": expired_jobs,
    }
    payload["processing_ffmpeg_count"] = roles["processing"]
    if payload["status"] != "maintenance" and (
        capture_issues or expired_jobs or payload["service_health"]["status"] == "degraded"
    ):
        payload["status"] = "degraded"
    payload["api_health"] = {"status": "healthy" if database["healthy"] else "blocked"}
    return payload


@app.get("/ready")
async def readiness():
    payload = await health()
    # Docker should restart the API only when its core dependency is unavailable.
    # A temporary X rate limit or absent optional diarizer is reported by
    # /health, but neither prevents this API from serving saved recordings.
    return JSONResponse(payload, status_code=200 if payload["database"]["healthy"] else 503)


def _clean_username(value: str) -> str:
    username = value.strip().lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        raise HTTPException(400, "Invalid X username")
    return username


# --- Monitored accounts ---


@app.get("/accounts", response_model=list[AccountOut])
def list_accounts():
    with get_session() as db:
        accounts = db.query(MonitoredAccount).all()
        from sqlalchemy import func

        space_stats = (
            db.query(
                Space.host_account_id,
                func.count(Space.id).label("cnt"),
                func.max(Space.detected_at).label("last_dt"),
            )
            .filter(Space.host_account_id.isnot(None))
            .group_by(Space.host_account_id)
            .all()
        )
        stats_map = {row[0]: (row[1], row[2]) for row in space_stats}

        active_hosts = set()
        if _monitor:
            for sid in _monitor.active_space_ids:
                sess = _monitor._active_sessions.get(sid)
                if sess and sess.host_username:
                    active_hosts.add(sess.host_username.lower().lstrip("@"))

        feed_hosts = set()
        if _monitor and hasattr(_monitor, "_live_spaces_feed"):
            for f in _monitor._live_spaces_feed.values():
                if f.get("host_username"):
                    feed_hosts.add(f["host_username"].lower().lstrip("@"))

        results = []
        for a in accounts:
            cnt, last_dt = stats_map.get(a.id, (0, None))
            u_clean = a.username.lower().lstrip("@")
            is_rec = u_clean in active_hosts
            is_liv = is_rec or (u_clean in feed_hosts)
            tags_list = (
                [t.strip() for t in (a.tags or "").split(",") if t.strip()]
                if getattr(a, "tags", None)
                else []
            )

            results.append(
                AccountOut(
                    id=a.id,
                    username=a.username,
                    display_name=a.display_name,
                    avatar_url=a.avatar_url,
                    active=a.active,
                    priority=a.priority,
                    added_at=a.added_at,
                    last_checked_at=a.last_checked_at,
                    next_check_at=a.next_check_at,
                    last_poll_outcome=a.last_poll_outcome,
                    last_poll_duration_seconds=a.last_poll_duration_seconds,
                    last_poll_request_count=a.last_poll_request_count,
                    poll_failure_streak=a.poll_failure_streak or 0,
                    notes=a.notes,
                    tags=tags_list,
                    spaces_count=cnt,
                    last_space_at=last_dt,
                    is_live=is_liv,
                    is_recording=is_rec,
                )
            )
        return results


@app.post("/accounts", response_model=AccountOut, status_code=201)
async def add_account(body: AccountIn):
    username = _clean_username(body.username)
    with get_session() as db:
        existing = db.query(MonitoredAccount).filter_by(username=username).first()
        if existing:
            raise HTTPException(409, f"@{username} is already saved; change its priority or active state")
        acc = MonitoredAccount(
            username=username, notes=body.notes, added_at=datetime.now(UTC), active=False, priority="P2"
        )
        db.add(acc)
        db.commit()
        log.info("Account added", username=username)
    asyncio.create_task(_fetch_and_store_avatar(username))
    with get_session() as db:
        account = db.query(MonitoredAccount).filter_by(username=username).first()
        return _account_out(account)


def _fetch_profile_via_api(username: str) -> tuple[str | None, str | None]:
    """جلب الصورة والاسم من API عام (fxtwitter) بدون تسجيل دخول."""
    import json
    import urllib.request

    req = urllib.request.Request(
        f"https://api.fxtwitter.com/{username}",
        headers={"User-Agent": "Mozilla/5.0 XSpaceAnalyzer"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    user = data.get("user") or {}
    avatar = user.get("avatar_url") or None
    if avatar:
        avatar = avatar.replace("_normal.", "_400x400.")
    return avatar, (user.get("name") or None)


async def _fetch_profile_via_browser(username: str) -> tuple[str | None, str | None]:
    """بديل احتياطي: فتح صفحة الحساب في المتصفح الرئيسي المسجّل دخوله."""
    if not _adapter or not _adapter._context:
        return None, None
    page = await _adapter._context.new_page()
    try:
        await page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2500)
        avatar_url = display_name = None
        img = await page.query_selector("img[src*='pbs.twimg.com/profile_images']")
        if img:
            src = await img.get_attribute("src") or ""
            avatar_url = src.replace("_normal.", "_400x400.") if src else None
        user_el = await page.query_selector('[data-testid="UserName"]')
        if user_el:
            lines = (await user_el.inner_text()).split("\n")
            if lines:
                display_name = lines[0].strip()
        return avatar_url, display_name
    finally:
        await page.close()


async def _fetch_and_store_avatar(username: str) -> bool:
    """جلب صورة الحساب وتخزينها. يعيد True عند النجاح."""
    username = username.lstrip("@")
    avatar_url = display_name = None
    try:
        avatar_url, display_name = await run_blocking(_fetch_profile_via_api, username)
    except Exception as e:
        log.debug("API avatar fetch failed, trying browser", username=username, error=str(e))
    if not avatar_url:
        try:
            avatar_url, display_name = await _fetch_profile_via_browser(username)
        except Exception as e:
            log.warning("Failed to fetch avatar", username=username, error=str(e))
    if not (avatar_url or display_name):
        return False
    with get_session() as db:
        acc = db.query(MonitoredAccount).filter_by(username=username).first()
        if acc:
            if avatar_url:
                acc.avatar_url = avatar_url
            if display_name and (not acc.display_name or acc.display_name == username):
                acc.display_name = display_name
            db.commit()
    log.info("Avatar updated", username=username, avatar=bool(avatar_url))
    return bool(avatar_url)


@app.post("/accounts/refresh-avatars")
async def refresh_all_avatars():
    """تحديث صور جميع الحسابات من X — يعمل بالتسلسل في الـ event loop."""
    with get_session() as db:
        accounts = db.query(MonitoredAccount).filter_by(active=True).all()
        usernames = [a.username for a in accounts]

    updated = 0
    for u in usernames:
        result = await _fetch_and_store_avatar(u)
        if result:
            updated += 1
        await asyncio.sleep(0.3)

    return {"status": "done", "total": len(usernames), "updated": updated}


@app.delete("/accounts/{username}", status_code=204)
def remove_account(username: str):
    """Archive an account while preserving its identity and historical Spaces."""
    username = _clean_username(username)
    with get_session() as db:
        acc = db.query(MonitoredAccount).filter_by(username=username).first()
        if not acc:
            raise HTTPException(404, f"@{username} not found")
        acc.active = False
        acc.priority = "P3"
        db.commit()
    log.info("Account archived", username=username)


@app.put("/accounts/{username}/toggle", response_model=AccountOut)
def toggle_account(username: str):
    username = _clean_username(username)
    with get_session() as db:
        acc = db.query(MonitoredAccount).filter_by(username=username).first()
        if not acc:
            raise HTTPException(404, f"@{username} not found")
        if not acc.active:
            if acc.priority == "P3":
                raise HTTPException(409, "Archived P3 accounts must be moved to P2 or P1 first")
            active_count = db.query(MonitoredAccount).filter(MonitoredAccount.active.is_(True)).count()
            if active_count >= _cfg.max_active_accounts:
                raise HTTPException(409, "Active account limit reached")
        acc.active = not acc.active
        db.commit()
        db.refresh(acc)
        return _account_out(acc)


@app.put("/accounts/{username}/priority", response_model=AccountOut)
def update_account_priority(username: str, body: AccountPriorityIn):
    username = _clean_username(username)
    with get_session() as db:
        acc = db.query(MonitoredAccount).filter_by(username=username).first()
        if not acc:
            raise HTTPException(404, f"@{username} not found")
        if body.priority == "P1" and not acc.active:
            active_count = db.query(MonitoredAccount).filter(MonitoredAccount.active.is_(True)).count()
            if active_count >= _cfg.max_active_accounts:
                raise HTTPException(409, "Active account limit reached; pause another account first")
        if acc.priority != body.priority:
            acc.priority = body.priority
            acc.active = body.priority == "P1"
            db.commit()
            db.refresh(acc)
        return _account_out(acc)


@app.put("/accounts/bulk")
def update_accounts_bulk(body: AccountBulkIn):
    if not body.usernames or len(body.usernames) > 59:
        raise HTTPException(400, "Select one to 59 accounts")
    normalized = {_clean_username(username).casefold() for username in body.usernames}
    with get_session() as db:
        rows = db.query(MonitoredAccount).all()
        selected = [row for row in rows if row.username.casefold() in normalized]
        if len(selected) != len(normalized):
            raise HTTPException(404, "One or more selected accounts do not exist")
        if body.active and any(row.priority == "P3" for row in selected):
            raise HTTPException(409, "Move archived P3 accounts to P2 before activating")
        resulting = sum(body.active if row in selected else row.active for row in rows)
        if resulting > _cfg.max_active_accounts:
            raise HTTPException(409, "Active account limit reached")
        for row in selected:
            row.active = body.active
        db.commit()
        return {"status": "updated", "changed": len(selected), "active_accounts": resulting}


@app.put("/accounts/{username}/tags")
def update_account_tags(username: str, body: AccountTagsIn):
    """تحديث العلامات (tags) الخاصة بحساب معين."""
    username = _clean_username(username)
    with get_session() as db:
        acc = db.query(MonitoredAccount).filter_by(username=username).first()
        if not acc:
            raise HTTPException(404, f"@{username} not found")
        clean_tags = [t.strip().replace(",", "") for t in body.tags if t.strip()]
        acc.tags = ",".join(clean_tags)
        db.commit()
        db.refresh(acc)
        return {"status": "ok", "username": acc.username, "tags": clean_tags}


@app.get("/tags")
def list_tags():
    """الحصول على قائمة بجميع العلامات المستخدمة وعدد الحسابات لكل منها."""
    with get_session() as db:
        accounts = db.query(MonitoredAccount).filter(MonitoredAccount.tags.isnot(None)).all()
        counts: dict[str, int] = {}
        for a in accounts:
            for t in (a.tags or "").split(","):
                t = t.strip()
                if t:
                    counts[t] = counts.get(t, 0) + 1
        return [{"name": k, "count": v} for k, v in sorted(counts.items())]


@app.get("/live/feed")
async def get_live_feed():
    """عرض قائمة بجميع المساحات المباشرة الجارية الآن من الحسابات أو الشريط الجانبي."""
    feed = []
    seen_ids = set()

    # 1. المساحات قيد التسجيل حالياً
    if _monitor:
        for sid in _monitor.active_space_ids:
            sess = _monitor._active_sessions.get(sid)
            if sess:
                seen_ids.add(sid)
                rec_status = sess._recorder.get_status()
                with get_session() as db:
                    sp = db.query(Space).filter_by(space_id=sid).first()
                    title = sp.title if sp and sp.title else "مساحة مباشرة"
                    acc = db.query(MonitoredAccount).filter_by(username=sess.host_username).first()
                    avatar = acc.avatar_url if acc else None
                    disp_name = acc.display_name if acc else sess.host_username

                feed.append(
                    {
                        "space_id": sid,
                        "title": title,
                        "host_username": sess.host_username,
                        "host_display_name": disp_name,
                        "host_avatar": avatar,
                        "role": "host",
                        "listeners_count": 0,
                        "is_recording": rec_status.state == "recording",
                        "verification_state": "recording" if rec_status.state == "recording" else "joining",
                        "host_verified": getattr(sess.adapter, "join_host_verification_state", None)
                        == "verified",
                        "duration_s": round(rec_status.duration_seconds, 1),
                        "url": f"https://x.com/i/spaces/{sid}",
                    }
                )

    # Discovery is independent of whether capture was authorized or started.
    discoveries = getattr(_monitor, "live_discovery", None)
    if discoveries:
        for row in discoveries.feed():
            if row["space_id"] not in seen_ids:
                feed.append(row)
        seen_ids.update(discoveries.entries)
    # 2. المساحات المكتشفة من الفيد الخارجي
    if _monitor and hasattr(_monitor, "_live_spaces_feed"):
        for sid, sinfo in _monitor._live_spaces_feed.items():
            if sid not in seen_ids:
                feed.append(sinfo)

    from xsa.browser.adapter import viewer_retry_remaining

    retry_after = viewer_retry_remaining()
    return {
        "feed": feed,
        "count": len(feed),
        "confirmed_count": sum(
            row.get("verification_state") in {"confirmed_live", "recording"} for row in feed
        ),
        "discovery": {
            "status": (
                "rate_limited"
                if retry_after > 0
                else "authentication_required"
                if _monitor and _monitor._graphql_budget.authentication_required()
                else "available"
                if _monitor
                else "offline"
            ),
            "retry_after_seconds": round(retry_after, 1),
        },
    }


# --- Spaces ---


class RecordSpaceIn(BaseModel):
    url: str
    host: str | None = None


class WatchSpacesIn(BaseModel):
    space_ids: list[str]


@app.post("/live/watch", status_code=202)
async def watch_live_spaces(body: WatchSpacesIn):
    """Queue exact Space IDs for verification without bypassing X's cooldown."""
    if not _monitor:
        raise HTTPException(503, "Discovery monitor is unavailable")
    if maintenance_active(_cfg.db_path.parent):
        raise HTTPException(423, "Maintenance is active")
    if not 1 <= len(body.space_ids) <= 10 or any(
        not re.fullmatch(r"[A-Za-z0-9]{1,100}", sid) for sid in body.space_ids
    ):
        raise HTTPException(400, "Provide one to ten valid Space IDs")
    for sid in body.space_ids:
        _monitor.live_discovery.remember(sid, "operator_link")
    from xsa.browser.adapter import viewer_retry_remaining

    return {
        "status": "verification_queued",
        "space_ids": body.space_ids,
        "retry_after_seconds": round(viewer_retry_remaining(), 1),
        "recording_started": False,
    }


def _queue_manual_record(body: RecordSpaceIn) -> tuple[str, dict]:
    if body.host and body.host.strip().lower() != "manual":
        raise HTTPException(400, "The Space creator is verified from X; omit the host field")
    try:
        space_id = space_id_from_url(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if maintenance_active(_cfg.db_path.parent):
        raise HTTPException(423, "Maintenance mode is active")
    if not _monitor or not _adapter:
        raise HTTPException(503, "Recording monitor is unavailable")
    if space_id in _monitor._active_sessions:
        raise HTTPException(409, "This Space is already recording")
    if space_id in getattr(_monitor, "_starting_spaces", {}):
        raise HTTPException(409, "This Space is already starting")
    with get_session() as db:
        existing = db.query(Space).filter_by(space_id=space_id).first()
        if existing and (
            existing.recording_started_at
            or existing.state in {"ended", "completed", "analyzed", "recording", "joining", "finalizing"}
        ):
            raise HTTPException(409, "This Space already has a recording or terminal capture")
    paths = SpacePaths.resolve_existing(_cfg.data_dir, space_id)
    if paths is not None and any(paths.root.glob("recording*.mkv")):
        raise HTTPException(409, "Existing capture files must be preserved")
    row = _monitor.live_discovery.request_capture(space_id)
    return space_id, row


@app.post("/spaces/record-requests", status_code=202)
async def queue_record_space(body: RecordSpaceIn):
    """Persist an operator's recording intent; verification obeys the X cooldown."""
    space_id, row = _queue_manual_record(body)
    from xsa.browser.adapter import viewer_retry_remaining

    return {
        "status": row["capture_request_state"],
        "space_id": space_id,
        "verification_state": row.get("state", "unverified"),
        "retry_after_seconds": round(viewer_retry_remaining(), 1),
    }


@app.post("/spaces/record", status_code=202)
async def trigger_record_space(body: RecordSpaceIn):
    """Verify identity and live state before starting an isolated capture."""
    space_id, row = _queue_manual_record(body)
    from xsa.browser.adapter import viewer_retry_remaining

    if viewer_retry_remaining() == 0:
        await _monitor.verify_manual_capture(space_id)
    current = _monitor.live_discovery.entries[space_id]
    return {
        "status": current.get("capture_request_state", "queued"),
        "space_id": space_id,
        "verification_state": current.get("state", "unverified"),
        "title": current.get("title"),
        "host_username": current.get("host_username"),
        "retry_after_seconds": round(viewer_retry_remaining(), 1),
    }


@app.get("/spaces/record-requests/{space_id}")
async def record_request_status(space_id: str):
    if not re.fullmatch(r"[A-Za-z0-9]{1,100}", space_id):
        raise HTTPException(400, "Invalid Space ID")
    if not _monitor:
        raise HTTPException(503, "Recording monitor is unavailable")
    row = _monitor.live_discovery.entries.get(space_id)
    if not row or not row.get("capture_request_state"):
        raise HTTPException(404, "No manual recording request for this Space")
    session = _monitor._active_sessions.get(space_id)
    with get_session() as db:
        capture = db.query(Space).filter_by(space_id=space_id).first()
        capture_state = capture.state if capture else None
        capture_error = (
            capture.join_last_error if capture and capture.state in {"error", "rejected"} else None
        )
    state = row["capture_request_state"]
    if state == "started" and capture_state in {"error", "rejected", "blocked"}:
        state = "failed"
    elif state == "started" and capture_state in {"ended", "completed", "analyzed"}:
        state = "completed"
    return {
        "space_id": space_id,
        "status": state,
        "verification_state": row.get("state", "unverified"),
        "title": row.get("title"),
        "host_username": row.get("host_username"),
        "is_recording": bool(session and session._recorder.get_status().state == "recording"),
        "capture_state": capture_state,
        "error": capture_error if state == "failed" else None,
        "analysis_url": f"/spaces/{space_id}/analysis",
    }


@app.delete("/spaces/record-requests/{space_id}")
async def cancel_record_request(space_id: str):
    if not re.fullmatch(r"[A-Za-z0-9]{1,100}", space_id):
        raise HTTPException(400, "Invalid Space ID")
    if not _monitor:
        raise HTTPException(503, "Recording monitor is unavailable")
    if not _monitor.live_discovery.cancel_capture_request(space_id):
        raise HTTPException(409, "Only a request still waiting for capture can be cancelled")
    return {"status": "cancelled", "space_id": space_id}


@app.get("/spaces/{space_id}", response_model=SpaceOut)
def get_space(space_id: str):
    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if not space:
            raise HTTPException(404, "Space not found")
        return space


@app.get("/spaces/{space_id}/analysis", response_class=HTMLResponse)
def space_analysis_page(space_id: str):
    """Open the evidence-backed analysis workspace for one Space."""
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    from xsa.api.space_page import SPACE_ANALYSIS_HTML

    return HTMLResponse(SPACE_ANALYSIS_HTML.replace("__SPACE_ID__", space_id))


@app.get("/spaces/{space_id}/analysis-data")
def get_space_analysis_data(space_id: str):
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    paths = SpacePaths.find_space(_cfg.data_dir, space_id)
    report = paths.analysis_dir / "full_analysis.json"
    if not report.is_file():
        raise HTTPException(404, "Analysis has not been generated for this Space yet")
    try:
        return json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "Space analysis artifact is unreadable") from exc


@app.get("/spaces/{space_id}/waveform")
async def get_space_waveform(space_id: str):
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    if _monitor and space_id in _monitor._active_sessions:
        raise HTTPException(409, "Waveform is available after recording finalization")
    paths = SpacePaths.resolve_existing(_cfg.data_dir, space_id)
    if paths is None:
        raise HTTPException(404, "Recording not found")
    audio = next(
        (
            path
            for path in (paths.audio_analysis, paths.audio_original, paths.root / "audio.wav")
            if path.is_file()
        ),
        None,
    )
    if audio is None:
        raise HTTPException(404, "Finalized PCM audio is unavailable")
    from xsa.waveform import read_waveform

    try:
        return await run_blocking(read_waveform, audio, paths.analysis_dir / "waveform.json")
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "Waveform could not be derived from this audio") from exc


@app.get("/spaces/{space_id}/analysis/search")
def search_space_analysis(space_id: str, q: str = "", limit: int = 100):
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    from xsa.analysis_platform import search_space_analysis as search_artifact

    paths = SpacePaths.find_space(_cfg.data_dir, space_id)
    if not (paths.analysis_dir / "timeline.json").is_file():
        raise HTTPException(404, "Timeline is not available for this Space")
    return {"space_id": space_id, "query": q, "results": search_artifact(paths, q, limit=limit)}


@app.get("/spaces/{space_id}/analysis/phrase")
def transcript_phrase_search(space_id: str, q: str, limit: int = 100):
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    if not q.strip() or len(q) > 200 or not 1 <= limit <= 500:
        raise HTTPException(400, "Provide a phrase of 1–200 characters and a result limit of 1–500")
    from xsa.analysis_platform import count_transcript_phrase

    paths = SpacePaths.find_space(_cfg.data_dir, space_id)
    if not paths.transcript_json.is_file() and not paths.transcript_normalized.is_file():
        raise HTTPException(404, "Transcript is not available for this Space")
    return {"space_id": space_id, **count_transcript_phrase(paths, q, limit=limit)}


@app.post("/spaces/{space_id}/analysis/rebuild")
def rebuild_space_analysis(space_id: str):
    """Idempotently refresh derived reports without touching source files."""
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    with get_session() as db:
        row = db.query(Space).filter_by(space_id=space_id).first()
        if not row:
            raise HTTPException(404, "Space not found")
        if row.state in {"joining", "recording", "finalizing"}:
            raise HTTPException(409, "Analysis is unavailable while the Space is active")
        space = {
            "space_id": row.space_id,
            "title": row.title,
            "host": row.host_account.username if row.host_account else row.verified_host_username,
            "url": row.url,
            "state": row.state,
        }
    paths = SpacePaths.find_space(_cfg.data_dir, space_id, title=space.get("title"))
    from xsa.analysis_platform import build_space_analysis, media_inventory_record

    inventory_record = media_inventory_record(paths)
    result = build_space_analysis(paths, space=space, inventory_record=inventory_record)
    return {
        "status": "ok",
        "space_id": space_id,
        "timeline_entries": len(result["timeline"]),
        "topics": len(result["topics"]),
        "url": f"/spaces/{space_id}/analysis",
    }


@app.get("/spaces/{space_id}/events")
def get_space_events(space_id: str):
    """Return recent persisted diagnostics for one Space, newest first."""
    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if not space:
            raise HTTPException(404, "Space not found")
        rows = (
            db.query(SystemEvent)
            .filter_by(space_id=space.id)
            .order_by(SystemEvent.event_time.desc(), SystemEvent.id.desc())
            .limit(50)
            .all()
        )
        return {
            "space_id": space_id,
            "events": [
                {
                    "time": row.event_time.isoformat() if row.event_time else None,
                    "category": row.category,
                    "level": row.level,
                    "message": row.message,
                    "details": row.details or {},
                }
                for row in rows
            ],
        }


@app.get("/spaces/{space_id}/diarization")
def get_space_diarization(space_id: str):
    """Return offline diarization job status and its current timeline artifact."""
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if not space:
            raise HTTPException(404, "Space not found")
        job = db.query(Job).filter_by(space_id=space.id, job_type="diarize").order_by(Job.id.desc()).first()
        job_status = (
            {
                "id": job.id,
                "state": job.state,
                "attempt": job.attempt,
                "progress": job.progress,
                "result": job.result,
                "error": job.error,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            }
            if job
            else None
        )
        title = space.title
    paths = SpacePaths.find_space(_cfg.data_dir, space_id, title=title)
    timeline_path = paths.analysis_dir / "speaker_timeline.json"
    timeline = None
    if timeline_path.is_file():
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(500, "Diarization timeline artifact is unreadable") from exc
    return {"space_id": space_id, "job": job_status, "timeline": timeline}


@app.post("/spaces/{space_id}/process", status_code=202)
def retry_space_processing(space_id: str):
    """Retry media/transcription/analysis processing for a completed recording."""
    if maintenance_active(_cfg.db_path.parent):
        raise HTTPException(423, "Maintenance mode is active; processing is paused")
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    if not _monitor:
        raise HTTPException(503, "Monitor service not ready")
    if space_id in _monitor._active_sessions:
        raise HTTPException(409, "Space is still being recorded")

    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if not space:
            raise HTTPException(404, "Space not found")
        space_db_id = space.id
        started_at = space.started_at or space.detected_at
        db.commit()

    paths = SpacePaths.for_space(_cfg.data_dir, space_id, started_at)
    if not _monitor.schedule_post_processing(space_id, space_db_id, paths, retry=True):
        raise HTTPException(409, "Processing is already running")
    _monitor._set_space_state(space_db_id, "processing")
    log.info("Space processing retry scheduled", space_id=space_id)
    return {"status": "scheduled", "space_id": space_id}


# --- Live status ---


@app.get("/live")
async def live_status():
    active = _monitor.active_space_ids if _monitor else []
    sessions = []
    if _monitor:
        for sid in active:
            sess = _monitor._active_sessions.get(sid)
            if sess:
                rec_status = sess._recorder.get_status()
                sessions.append(
                    {
                        "space_id": sid,
                        "host": sess.host_username,
                        "state": rec_status.state,
                        "duration_s": round(rec_status.duration_seconds, 1),
                        "file_mb": round(rec_status.file_bytes / 1_000_000, 2),
                        "health": rec_status.health,
                    }
                )
    # The isolated worker cannot inspect this container's process namespace.
    # Expose only a count, avoiding PID-namespace or Docker-socket sharing.
    return {
        "active": sessions,
        "recording_ffmpeg_count": _recording_ffmpeg_count(),
        "active_post_processing": [
            sid
            for sid, task in getattr(_monitor, "_post_processing_tasks", {}).items()
            if not task.done() and not getattr(task, "_xsa_waiting", False)
        ],
        "maintenance_mode": maintenance_active(_cfg.db_path.parent),
    }


def _ffmpeg_role_counts() -> dict[str, int]:
    counts = {"recording": 0, "processing": 0}
    for process in psutil.process_iter(attrs=["name"]):
        try:
            name = (process.info.get("name") or "").lower()
            if "ffmpeg" not in name:
                continue
            args = (process.info["cmdline"] if "cmdline" in process.info else process.cmdline()) or []
            role = "recording" if "x11grab" in args and "pulse" in args else "processing"
            counts[role] += 1
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError):
            # Uninspectable FFmpeg cannot establish safe idleness.
            counts["recording"] += 1
    return counts


def _recording_ffmpeg_count() -> int:
    return _ffmpeg_role_counts()["recording"]


@app.get("/maintenance/status")
async def maintenance_status():
    """Deployment preflight; reconcile dead joins and cancel only over-deadline joins."""
    database_healthy = False
    unsafe_spaces: list[dict[str, str]] = []
    running_diarization_jobs = 0
    detected_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
    now = datetime.now(UTC).replace(tzinfo=None)
    expired_live_joins: list[str] = []
    try:
        from xsa.job_ownership import recover_expired_jobs

        recover_expired_jobs()
        with get_session() as db:
            db.execute(text("SELECT 1"))
            database_healthy = True
            # A transition only blocks while a real runtime task is alive and
            # inside its deadline. Reconcile orphaned/expired JOINING rows.
            for row in db.query(Space).filter(Space.state == "joining").all():
                task = _monitor._active_tasks.get(row.space_id) if _monitor else None
                runtime_alive = bool(task and not task.done())
                age = (
                    max(0.0, (now - row.joining_started_at).total_seconds())
                    if row.joining_started_at
                    else None
                )
                deadline = row.join_deadline_seconds or _cfg.session_start_timeout_seconds
                if runtime_alive and age is not None and age <= deadline:
                    continue
                if runtime_alive:
                    expired_live_joins.append(row.space_id)
                    continue
                row.state = "error"
                row.join_outcome = "startup_recovery_interrupted_join"
                row.join_last_error = "JOINING row has no live runtime task"
                row.join_age_seconds = age
                row.join_checkpoint = row.join_checkpoint or "maintenance_reconciliation"
                row.join_checkpoint_at = now
                row.last_progress_at = now
                db.add(
                    SystemEvent(
                        event_time=now,
                        category="recording",
                        level="error",
                        message="Stale JOINING row reconciled for maintenance preflight",
                        space_id=row.id,
                        details={
                            "reason": row.join_outcome,
                            "last_checkpoint": row.join_checkpoint,
                            "join_age_seconds": age,
                            "join_deadline_seconds": deadline,
                        },
                    )
                )
            db.commit()
        for space_id in expired_live_joins:
            if _monitor:
                await _monitor.cancel_join(
                    space_id,
                    "Join task exceeded its deadline during maintenance preflight",
                    outcome="join_timeout",
                )
        with get_session() as db:
            rows = (
                db.query(Space)
                .filter(
                    (Space.state.in_(("joining", "recording", "processing")))
                    | ((Space.state == "detected") & (Space.detected_at >= detected_cutoff))
                )
                .all()
            )
            unsafe_spaces = [
                {
                    "space_id": row.space_id,
                    "state": row.state,
                    "join_checkpoint": row.join_checkpoint,
                    "join_age_seconds": row.join_age_seconds,
                }
                for row in rows
            ]
            running_diarization_jobs = db.query(Job).filter_by(job_type="diarize", state="running").count()
    except Exception as exc:
        log.error("Maintenance preflight database check failed", error=type(exc).__name__)
    active_recordings = []
    if _monitor:
        for space_id, session in _monitor._active_sessions.items():
            proc = getattr(session._recorder, "_proc", None)
            if proc and proc.poll() is None:
                active_recordings.append(space_id)
    active_post_processing = (
        [
            sid
            for sid, task in _monitor._post_processing_tasks.items()
            if not getattr(task, "_xsa_waiting", False)
        ]
        if _monitor
        else []
    )
    ffmpeg_count = _recording_ffmpeg_count()
    gate_active = maintenance_active(_cfg.db_path.parent)
    return {
        "maintenance_mode": gate_active,
        "database_healthy": database_healthy,
        "active_recordings": active_recordings,
        "recording_ffmpeg_count": ffmpeg_count,
        "active_post_processing": active_post_processing,
        "unsafe_spaces": unsafe_spaces,
        "running_diarization_jobs": running_diarization_jobs,
        "safe_to_deploy": bool(
            gate_active
            and database_healthy
            and not active_recordings
            and not ffmpeg_count
            and not active_post_processing
            and not unsafe_spaces
            and not running_diarization_jobs
        ),
    }


@app.post("/admin/recordings/stop-all")
async def admin_stop_all_recordings(payload: AdminStopIn):
    """Gracefully stop all active captures and mark them development_interrupted."""
    if not _monitor:
        raise HTTPException(503, "Monitor service not ready")
    reason = payload.reason.strip()[:250] or "Operator stopped the recording"
    results = await _monitor.stop_all_recordings(reason)
    return {"stopped": list(results), "results": results}


@app.post("/admin/recordings/{space_id}/stop")
async def admin_stop_recording(space_id: str, payload: AdminStopIn):
    """Gracefully stop one active capture without killing Docker or FFmpeg."""
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    if not _monitor:
        raise HTTPException(503, "Monitor service not ready")
    reason = payload.reason.strip()[:250] or "Operator stopped the recording"
    summary = await _monitor.stop_space(space_id, reason)
    if summary is None:
        raise HTTPException(404, "Space is not an active recording")
    return {"space_id": space_id, "state": "development_interrupted", "summary": summary}


@app.delete("/admin/spaces/{space_id}/join")
async def admin_cancel_join(space_id: str, payload: AdminStopIn):
    """Cancel a JOINING task and wait for its page, FFmpeg, and slot cleanup."""
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    if not _monitor:
        raise HTTPException(503, "Monitor service not ready")
    result = await _monitor.cancel_join(
        space_id, payload.reason.strip()[:250] or "Join cancelled by operator"
    )
    if result is None:
        raise HTTPException(409, "Space is not in a live JOINING state")
    if result["ffmpeg_alive"]:
        raise HTTPException(500, "Join cancelled, but FFmpeg is still alive; slot remains quarantined")
    return {**result, "state": "error", "outcome": "cancelled"}


@app.post("/admin/maintenance")
async def admin_enable_maintenance(payload: AdminMaintenanceIn):
    """Arm the persistent deployment gate; optionally stop active captures safely."""
    try:
        gate = arm_maintenance(_cfg.db_path.parent)
    except FileExistsError:
        raise HTTPException(409, "Maintenance mode is already active") from None
    stopped = {}
    if payload.stop_recordings and _monitor:
        reason = payload.reason.strip()[:250] or "Maintenance deployment"
        stopped = await _monitor.stop_all_recordings(reason)
    return {
        "maintenance_mode": True,
        "lock_id": gate["lock_id"],
        "stopped": list(stopped),
        "active_recordings": _monitor.active_space_ids if _monitor else [],
    }


@app.delete("/admin/maintenance")
async def admin_release_maintenance():
    """Release the persistent gate and let the existing monitor resume."""
    if not maintenance_active(_cfg.db_path.parent):
        raise HTTPException(409, "Maintenance mode is not active")
    release_maintenance(_cfg.db_path.parent)
    return {"maintenance_mode": False, "monitor_resuming": True}


@app.delete("/admin/spaces/{space_id}/processing")
async def admin_cancel_processing(space_id: str, payload: AdminStopIn):
    """Cancel the tracked processing task and preserve an operator-visible reason."""
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    if not _monitor:
        raise HTTPException(503, "Monitor service not ready")
    reason = payload.reason.strip()[:250] or "Processing cancelled by operator"
    if not await _monitor.cancel_processing(space_id, reason):
        raise HTTPException(404, "Space not found")
    return {"space_id": space_id, "state": "blocked", "processing": "cancelled"}


@app.post("/admin/spaces/{space_id}/quarantine")
async def admin_quarantine_space(space_id: str, payload: AdminStopIn):
    """Gracefully stop/block a broken Space without moving or deleting its media."""
    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    if not _monitor:
        raise HTTPException(503, "Monitor service not ready")
    reason = payload.reason.strip()[:250] or "Quarantined by operator"
    if not await _monitor.quarantine_space(space_id, reason):
        raise HTTPException(404, "Space not found")
    return {"space_id": space_id, "state": "blocked", "reason": reason}


@app.get("/debug/audio_check")
async def debug_audio_check():
    res = {}
    if _adapter and _adapter._page:
        try:
            m = await _adapter._page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('audio, video')).map(e => ({
                    tag: e.tagName,
                    paused: e.paused,
                    muted: e.muted,
                    volume: e.volume,
                    readyState: e.readyState,
                    src: e.src ? e.src.slice(0, 100) : '',
                    currentSrc: e.currentSrc ? e.currentSrc.slice(0, 100) : '',
                }));
                return {
                    url: window.location.href,
                    title: document.title,
                    elements: els,
                };
            }""")
            res["master_display_99"] = m
        except Exception as e:
            res["master_display_99"] = {"error": str(e)}

    if _monitor:
        for sid, sess in _monitor._active_sessions.items():
            if sess.adapter and sess.adapter._page:
                try:
                    m = await sess.adapter._page.evaluate("""() => {
                        const els = Array.from(document.querySelectorAll('audio, video')).map(e => ({
                            tag: e.tagName,
                            paused: e.paused,
                            muted: e.muted,
                            volume: e.volume,
                            readyState: e.readyState,
                            src: e.src ? e.src.slice(0, 100) : '',
                            currentSrc: e.currentSrc ? e.currentSrc.slice(0, 100) : '',
                        }));
                        return {
                            url: window.location.href,
                            title: document.title,
                            elements: els,
                        };
                    }""")
                    slot_id = getattr(sess.slot, "slot_id", None)
                    res[f"session_{sid}_slot_{slot_id}"] = m
                except Exception as e:
                    res[f"session_{sid}"] = {"error": str(e)}
    return res


@app.post("/debug/unmute_all")
async def debug_unmute_all():
    results = {}
    if _monitor:
        for sid, session in _monitor._active_sessions.items():
            if session.adapter and session.adapter._active_space_id == sid:
                results[sid] = await session.adapter.ensureAudioPlaying()
    return results


# --- Phase 3: Speaker & Participant Endpoints ---


@app.get("/spaces/{space_id}/speakers")
async def get_space_speakers(space_id: str):
    """
    Fetch current speakers for a live Space from GraphQL.
    Works only while the space is active (adapter is on the Space page).
    """
    if not _adapter:
        raise HTTPException(500, "Browser not ready")
    try:
        participants = await _adapter.getParticipantsGQL(space_id)
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return {
        "space_id": space_id,
        "participants": [
            {
                "username": p.username,
                "display_name": p.display_name,
                "role": p.role,
                "avatar_url": p.avatar_url,
                "is_muted": p.is_muted,
                "is_speaking": p.is_speaking,
                "source": p.source,
            }
            for p in participants
        ],
        "count": len(participants),
    }


@app.get("/spaces/{space_id}/speaker-timeline")
def get_speaker_timeline(space_id: str):
    """Return space metadata, timestamped speaker turns, and speaking totals."""
    import json as _json

    if not re.fullmatch(r"[A-Za-z0-9]+", space_id):
        raise HTTPException(400, "Invalid Space ID")
    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if not space:
            raise HTTPException(404, "Space not found")
        row_id = space.id
        title = space.title
        host = space.host_account.username if space.host_account else space.verified_host_username
        url = space.url
        detected_at = space.detected_at
        started_at = space.recording_started_at or space.started_at
        ended_at = space.recording_ended_at or space.ended_at
        db_events = (
            db.query(SpeakerEvent)
            .filter_by(space_id=row_id)
            .order_by(SpeakerEvent.recording_offset_ms, SpeakerEvent.id)
            .all()
        )

    paths = SpacePaths.find_space(_cfg.data_dir, space_id, title=title)
    metadata = {}
    if paths.space_json.is_file():
        with suppress(OSError, ValueError):
            metadata = _json.loads(paths.space_json.read_text(encoding="utf-8"))

    events = []
    if paths.speaker_events_live.is_file():
        try:
            with paths.speaker_events_live.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        try:
                            events.append(_json.loads(line))
                        except ValueError:
                            continue
        except OSError as exc:
            log.warning("Could not read speaker events", space_id=space_id, error=str(exc))
    if not events:
        events = [
            {
                "event": row.event_type,
                "username": row.username,
                "timestamp": row.recording_offset_ms / 1000 if row.recording_offset_ms is not None else None,
                "wall_time": row.wall_time.isoformat() if row.wall_time else None,
                "source": row.source,
                **(row.raw_data or {}),
            }
            for row in db_events
        ]

    live_session = _monitor._active_sessions.get(space_id) if _monitor else None
    from xsa.speaker_timeline import build_speaker_timeline

    return build_speaker_timeline(
        space_id=space_id,
        title=title,
        host=host,
        space_url=url,
        detected_at=detected_at,
        started_at=started_at,
        ended_at=ended_at,
        events=events,
        folder=paths.root,
        data_dir=_cfg.data_dir,
        live_offset_ms=live_session._current_offset_ms() if live_session else None,
        metadata=metadata,
    )


@app.get("/spaces/{space_id}/speaker-timeline.json")
def download_speaker_timeline(space_id: str):
    """Download a portable JSON report with folder, link, speakers, and turns."""
    timeline = get_speaker_timeline(space_id)
    return JSONResponse(
        content=timeline,
        headers={"Content-Disposition": f'attachment; filename="{space_id}-speaker-timeline.json"'},
    )


@app.get("/spaces/{space_id}/stats")
def get_space_stats(space_id: str):
    """
    Return speaker stats from the space.json summary file.
    Available after recording ends.
    """
    import json as _json

    paths = SpacePaths(_cfg.data_dir, space_id)
    space_json = paths.space_json
    if not space_json.exists():
        timeline = get_speaker_timeline(space_id)
        space_info = timeline["space"]
        return {
            **space_info,
            "space_id": space_id,
            "speaker_stats": {
                speaker["username"]: {
                    "speaking_ms": speaker["speaking_ms"],
                    "speaking_seconds": round(speaker["speaking_ms"] / 1000, 1),
                    "turn_count": speaker["turn_count"],
                }
                for speaker in timeline["speakers"]
            },
        }
    try:
        data = _json.loads(space_json.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return data


@app.get("/live/details")
async def live_details():
    """
    Extended live status including speaker stats and current active speaker.
    """
    if not _monitor:
        return {"active": []}
    sessions = []
    for sid in _monitor.active_space_ids:
        sess = _monitor._active_sessions.get(sid)
        if not sess:
            continue
        rec_status = sess._recorder.get_status()

        # Collect worker adapter and active speaker
        adapter = getattr(sess, "adapter", None) or _adapter
        active_speaker = None
        try:
            sp = await adapter.getActiveSpeaker() if adapter else None
            if sp:
                active_speaker = {
                    "username": sp.username,
                    "display_name": sp.display_name,
                    "role": sp.role,
                    "avatar_url": getattr(sp, "avatar_url", "") or "",
                    "source": sp.source,
                }
        except Exception:
            pass

        # Collect Admins (Host & Co-hosts), Live Speakers, and Listeners
        admins = []
        speakers = []
        listeners = []
        seen_usernames = set()

        host_uname = (sess.host_username or "").lstrip("@")
        is_auto_host = host_uname.lower() in ("auto", "auto_detect", "manual", "unknown", "")

        # 1. From session's speaker tracker state
        if (
            hasattr(sess, "_speaker_tracker")
            and sess._speaker_tracker
            and getattr(sess._speaker_tracker, "_state", None)
        ):
            for identity, item in sess._speaker_tracker._state.items():
                p = item.get("person", {})
                uname = (p.get("username") or identity or "").lstrip("@")
                if (
                    not uname
                    or uname.lower() in seen_usernames
                    or uname.lower() in ("auto", "auto_detect", "unknown")
                ):
                    continue
                seen_usernames.add(uname.lower())
                raw_role = (p.get("role") or "").lower().strip()
                avatar = p.get("avatar_url") or p.get("avatar") or ""
                disp = p.get("display_name") or uname
                is_active = bool(
                    active_speaker and active_speaker.get("username", "").lower() == uname.lower()
                )
                is_host_user = bool(host_uname and uname.lower() == host_uname.lower())

                if raw_role in ("host", "المضيف", "مضيف") or is_host_user:
                    role = "host"
                    role_label = "المضيف"
                elif raw_role in ("cohost", "استضافة مشتركة", "مضيف مشارك"):
                    role = "cohost"
                    role_label = "استضافة مشتركة"
                elif raw_role == "admin":
                    # An admin is the primary host if no host is set yet or username matches
                    has_host = any(a.get("role") == "host" for a in admins)
                    if not has_host or is_host_user:
                        role = "host"
                        role_label = "المضيف"
                    else:
                        role = "cohost"
                        role_label = "استضافة مشتركة"
                elif raw_role in ("speaker", "متحدث", "المتحدث") or is_active:
                    role = "speaker"
                    role_label = "متحدث"
                elif raw_role in ("listener", "مستمع", "المستمع"):
                    role = "listener"
                    role_label = "مستمع"
                else:
                    role = "speaker" if is_active else "listener"
                    role_label = "متحدث" if is_active else "مستمع"

                info = {
                    "username": uname,
                    "display_name": disp,
                    "role": role,
                    "role_label": role_label,
                    "avatar_url": avatar
                    or f"https://ui-avatars.com/api/?name={disp}&background=7856ff&color=fff",
                    "is_muted": bool(p.get("is_muted", False)),
                    "is_speaking": is_active,
                }
                if role == "host":
                    admins.insert(0, info)
                elif role == "cohost":
                    admins.append(info)
                elif role == "speaker":
                    speakers.append(info)
                elif role == "listener":
                    listeners.append(info)

        # 2. Ensure host is included in admins (excluding 'auto' placeholder)
        if not is_auto_host and host_uname.lower() not in seen_usernames:
            with get_session() as db:
                acc = db.query(MonitoredAccount).filter_by(username=host_uname).first()
                host_disp = acc.display_name if acc and acc.display_name else host_uname
                host_avatar = (
                    acc.avatar_url
                    if acc and acc.avatar_url
                    else f"https://ui-avatars.com/api/?name={host_disp}&background=7856ff&color=fff"
                )
            admins.insert(
                0,
                {
                    "username": host_uname,
                    "display_name": host_disp,
                    "role": "host",
                    "role_label": "المضيف",
                    "avatar_url": host_avatar,
                    "is_muted": False,
                    "is_speaking": bool(
                        active_speaker and active_speaker.get("username", "").lower() == host_uname.lower()
                    ),
                },
            )
            seen_usernames.add(host_uname.lower())
        elif is_auto_host and len(admins) == 0:
            admins.append(
                {
                    "username": "",
                    "display_name": "جارٍ استخراج بيانات المضيف...",
                    "role": "host",
                    "role_label": "المضيف",
                    "avatar_url": "https://ui-avatars.com/api/?name=Space&background=7856ff&color=fff",
                    "is_muted": False,
                    "is_speaking": False,
                }
            )

        # 3. Fallback: query participants from worker page if speakers list or admins list is incomplete
        if adapter and (
            len(speakers) == 0 or len(admins) == 0 or (len(admins) == 1 and not admins[0].get("username"))
        ):
            try:
                scraped = await adapter.getParticipants()
                for p in scraped:
                    uname = (p.username or "").lstrip("@")
                    key = uname.lower() if uname else (p.display_name or "").lower()
                    if not key or key in seen_usernames or key in ("auto", "unknown"):
                        continue
                    seen_usernames.add(key)
                    raw_role = (p.role or "").lower().strip()
                    is_active = bool(
                        active_speaker and active_speaker.get("username", "").lower() == uname.lower()
                        if uname
                        else False
                    )
                    is_host_user = bool(host_uname and uname.lower() == host_uname.lower())

                    if raw_role in ("host", "المضيف", "مضيف") or is_host_user:
                        role = "host"
                        role_label = "المضيف"
                    elif raw_role in ("cohost", "استضافة مشتركة", "مضيف مشارك"):
                        role = "cohost"
                        role_label = "استضافة مشتركة"
                    elif raw_role == "admin":
                        has_host = any(a.get("role") == "host" for a in admins)
                        if not has_host or is_host_user:
                            role = "host"
                            role_label = "المضيف"
                        else:
                            role = "cohost"
                            role_label = "استضافة مشتركة"
                    elif raw_role in ("speaker", "متحدث", "المتحدث") or is_active:
                        role = "speaker"
                        role_label = "متحدث"
                    elif raw_role in ("listener", "مستمع", "المستمع"):
                        role = "listener"
                        role_label = "مستمع"
                    else:
                        role = "speaker" if is_active else "listener"
                        role_label = "متحدث" if is_active else "مستمع"

                    info = {
                        "username": uname,
                        "display_name": p.display_name or uname,
                        "role": role,
                        "role_label": role_label,
                        "avatar_url": p.avatar_url
                        or f"https://ui-avatars.com/api/?name={p.display_name or uname}&background=1d9bf0&color=fff",
                        "is_muted": p.is_muted,
                        "is_speaking": is_active,
                    }
                    if role == "host":
                        admins = [
                            a
                            for a in admins
                            if a.get("username") != ""
                            or a.get("display_name") != "جارٍ استخراج بيانات المضيف..."
                        ]
                        admins.insert(0, info)
                        if is_auto_host and uname and hasattr(sess, "_adopt_discovered_host"):
                            sess._adopt_discovered_host(uname, p.display_name)
                    elif role == "cohost":
                        admins.append(info)
                    elif role == "speaker":
                        speakers.append(info)
                    elif role == "listener":
                        listeners.append(info)
            except Exception:
                pass

        # 4. Normalize admins: Ensure there is at least one "host" in admins if admins exist
        if admins and not any(a.get("role") == "host" for a in admins):
            admins[0]["role"] = "host"
            admins[0]["role_label"] = "المضيف"
        admins.sort(key=lambda a: 0 if a.get("role") == "host" else 1)

        slot_id = getattr(sess.slot, "slot_id", 0) if getattr(sess, "slot", None) else 0
        display = getattr(sess.slot, "display", ":99") if getattr(sess, "slot", None) else ":99"

        sessions.append(
            {
                "space_id": sid,
                "title": getattr(sess, "space_title", None) or "مساحة مباشرة",
                "host": sess.host_username,
                "admins": admins,
                "speakers": speakers,
                "listener_count": max(len(listeners), getattr(sess, "listener_count", 0)),
                "state": rec_status.state,
                "duration_s": round(rec_status.duration_seconds, 1),
                "file_mb": round(rec_status.file_bytes / 1_000_000, 2),
                "health": rec_status.health,
                "ffmpeg_healthy": rec_status.health,
                "active_speaker": active_speaker,
                "audio_silence": sess._recorder.get_audio_silence_status(),
                "end_detector": sess.end_detector.snapshot(),
                "last_heartbeat": sess.speaker_collector_health.get("last_success_at"),
                "slot_id": slot_id,
                "display": display,
                "viewer_url": f"http://127.0.0.1:{6081 + slot_id}/vnc.html?autoconnect=true&resize=scale",
                "speaker_stats": {
                    username: {
                        "speaking_ms": sess._speaker_durations.get(username, 0),
                        "speaking_seconds": round(sess._speaker_durations.get(username, 0) / 1000, 1),
                        "turn_count": sess._speaker_turn_counts.get(username, 0),
                    }
                    for username in set(
                        list(sess._speaker_durations.keys()) + list(sess._speaker_turn_counts.keys())
                    )
                },
            }
        )
    return {"active": sessions, "max_concurrent_recordings": _cfg.max_concurrent_recordings}


# --- Phase 7: Web Dashboard UI ---


@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
def serve_dashboard():
    from xsa.api.dashboard import DASHBOARD_HTML

    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/workspace/config")
async def workspace_config():
    """Non-secret runtime settings and the existing maintenance gate state."""
    return {
        "poll_interval_seconds": _cfg.poll_interval_seconds,
        "max_active_accounts": _cfg.max_active_accounts,
        "max_graphql_requests_per_cycle": _cfg.max_graphql_requests_per_cycle,
        "account_poll_spacing_seconds": _cfg.account_poll_spacing_seconds,
        "account_poll_jitter_seconds": _cfg.account_poll_jitter_seconds,
        "capture_recovery_timeout_seconds": _cfg.capture_recovery_timeout_seconds,
        "max_capture_recoveries": _cfg.max_capture_recoveries,
        "max_concurrent_recordings": _cfg.max_concurrent_recordings,
        "whisper_model": _cfg.whisper_model,
        "whisper_language": _cfg.whisper_language,
        # The token belongs to the isolated worker; the API cannot inspect it.
        "hf_token_configured": None,
        # A full deployment preflight may reconcile stale rows. This display
        # endpoint must stay read-only and therefore cannot assert readiness.
        "safe_to_deploy": False if not maintenance_active(_cfg.db_path.parent) else None,
    }


@app.put("/workspace/monitoring-config")
def update_monitoring_config(body: MonitoringConfigIn):
    """Apply measured monitoring limits and persist them for the next boot."""
    with get_session() as db:
        active_count = db.query(MonitoredAccount).filter(MonitoredAccount.active.is_(True)).count()
    if active_count > body.max_active_accounts:
        raise HTTPException(409, "Pause accounts before lowering the active limit")
    values = body.model_dump()
    save_monitoring_config(_cfg.db_path.parent, values)
    for key, value in values.items():
        setattr(_cfg, key, value)
    return {"status": "updated", **values, "active_accounts": active_count}


@app.get("/workspace/{section}", response_class=HTMLResponse)
def serve_workspace_section(section: str):
    from xsa.api.workspace_ui import SECTIONS, WORKSPACE_HTML

    if section not in SECTIONS:
        raise HTTPException(404, "Unknown workspace section")
    return HTMLResponse(WORKSPACE_HTML.replace("__SECTION__", section))


# --- Phase 4 & 5: Transcription Endpoints ---


@app.get("/processing/jobs")
def list_processing_jobs(limit: int = 40):
    """Recent durable jobs, including the measured Whisper window count."""
    if not 1 <= limit <= 200:
        raise HTTPException(400, "limit must be between 1 and 200")
    now = datetime.now(UTC).replace(tzinfo=None)
    with get_session() as db:
        rows = (
            db.query(Job, Space.space_id)
            .outerjoin(Space, Job.space_id == Space.id)
            .order_by(Job.id.desc())
            .limit(limit)
            .all()
        )
        return {
            "items": [
                {
                    "id": job.id,
                    "space_id": space_id,
                    "job_type": job.job_type,
                    "state": job.state,
                    "stage": job.result.get("stage") if isinstance(job.result, dict) else None,
                    "progress": job.progress if job.state == "running" else None,
                    "progress_basis": "pipeline_stage",
                    "completed_windows": job.result.get("completed_windows")
                    if isinstance(job.result, dict)
                    else None,
                    "total_windows": job.result.get("total_windows")
                    if isinstance(job.result, dict)
                    else None,
                    "attempt": job.attempt,
                    "max_attempts": job.max_attempts,
                    "worker_id": job.worker_id,
                    "error": job.error,
                    "created_at": job.created_at.isoformat() if job.created_at else None,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                    "age_seconds": max(0.0, (now - job.created_at).total_seconds())
                    if job.created_at
                    else None,
                    "attempt_runtime_seconds": max(
                        0.0, ((job.completed_at or now) - job.started_at).total_seconds()
                    )
                    if job.started_at and (job.completed_at or job.state in {"running", "cancelling"})
                    else None,
                }
                for job, space_id in rows
            ]
        }


@app.get("/spaces")
def list_spaces(include_failed: bool = False):
    """List saved Spaces; optionally include failed/rejected attempts without artifacts."""
    with get_session() as db:
        spaces = db.query(Space).order_by(Space.detected_at.desc()).all()
        processing_by_space: dict[str, dict] = {}
        media_preparation_by_space: dict[str, str] = {}
        for space_id, state in (
            db.query(Space.space_id, Job.state)
            .join(Job, Job.space_id == Space.id)
            .filter(Job.job_type == "prepare_media")
            .order_by(Job.id)
            .all()
        ):
            media_preparation_by_space[space_id] = state
        for space_id, state, progress, result, error, attempt in (
            db.query(Space.space_id, Job.state, Job.progress, Job.result, Job.error, Job.attempt)
            .join(Job, Job.space_id == Space.id)
            .filter(Job.job_type == "post_process")
            .order_by(Job.id)
            .all()
        ):
            processing_by_space[space_id] = {
                "state": state,
                "progress": progress,
                "stage": result.get("stage") if isinstance(result, dict) else None,
                "error": error,
                "attempt": attempt,
            }
        results = []
        # Resolve storage folders as a batch. Calling find_space() for every row
        # repeats a recursive walk for legacy/missing folders and made the
        # dashboard library request take tens of seconds with a large history.
        paths_by_id = SpacePaths.find_spaces(_cfg.data_dir, [space.space_id for space in spaces])
        # Docker Desktop's Windows bind mount has a high per-stat cost. Collect
        # each matched folder's direct files once instead of probing every
        # possible artifact path separately for every card.
        artifacts_by_id: dict[str, dict[str, object]] = {}
        for space_id, resolved_paths in paths_by_id.items():
            files: dict[str, int] = {}
            mtimes: dict[str, int] = {}
            directories: set[str] = set()
            try:
                with os.scandir(resolved_paths.root) as entries:
                    for entry in entries:
                        if entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            files[entry.name] = stat.st_size
                            mtimes[entry.name] = stat.st_mtime_ns
                        elif entry.is_dir(follow_symlinks=False):
                            directories.add(entry.name)
            except OSError:
                pass
            screenshots_have_png = False
            if "screenshots" in directories:
                try:
                    with os.scandir(resolved_paths.screenshots) as entries:
                        screenshots_have_png = any(
                            entry.is_file(follow_symlinks=False) and entry.name.casefold().endswith(".png")
                            for entry in entries
                        )
                except OSError:
                    pass
            artifacts_by_id[space_id] = {
                "files": files,
                "mtimes": mtimes,
                "directories": directories,
                "screenshots_have_png": screenshots_have_png,
            }
        for s in spaces:
            paths = paths_by_id.get(s.space_id)
            if paths is None:
                paths = SpacePaths.planned_path(
                    _cfg.data_dir,
                    s.space_id,
                    s.started_at or s.detected_at or datetime.now(UTC),
                    title=s.title,
                    host=f"@{s.host_account.username or s.verified_host_username}"
                    if s.host_account
                    else (f"@{s.verified_host_username}" if s.verified_host_username else None),
                )
            artifacts = artifacts_by_id.get(s.space_id, {})
            files = artifacts.get("files", {})
            mtimes = artifacts.get("mtimes", {})
            directories = artifacts.get("directories", set())
            recording_names = [name for name in files if re.fullmatch(r"recording_[0-9]{3}\.mkv", name)]
            if "recording.mkv" in files:
                recorded_bytes = int(files.get("recording.mkv", 0))
            else:
                recorded_bytes = sum(int(files[name]) for name in recording_names)
            file_mb = round(recorded_bytes / 1_000_000, 1)

            # Older DB rows often stored the capture clock as the title and
            # did not retain the host. Prefer verified folder/space metadata
            # over those placeholders so historical recordings stay readable.
            folder_parts = paths.root.name.split(" - ")
            folder_title = folder_host = None
            if (
                len(folder_parts) >= 3
                and re.fullmatch(r"\d{6}", folder_parts[0])
                and folder_parts[-1] == s.space_id
            ):
                folder_title = folder_parts[1]
                folder_host = folder_parts[2] if len(folder_parts) >= 4 else None

            file_meta = {}
            if "space.json" in files:
                try:
                    import json as _j

                    file_meta = _j.loads(paths.space_json.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    file_meta = {}

            host_user = s.host_account.username if s.host_account else s.verified_host_username
            host_display_name = s.host_account.display_name if s.host_account else s.verified_host_username
            space_meta = s.space_metadata if isinstance(s.space_metadata, dict) else {}
            host_names = {
                str(value).strip().lstrip("@").casefold()
                for value in (
                    host_user,
                    host_display_name,
                    file_meta.get("host"),
                    file_meta.get("host_display_name"),
                )
                if value
            }

            def usable_space_title(value, known_host_names=host_names):
                candidate = str(value or "").strip()
                if (
                    not candidate
                    or candidate in {"عنوان غير مسجل", "عنوان المساحة غير محفوظ", "مساحة مسجلة"}
                    or re.fullmatch(r"\d{6}", candidate)
                ):
                    return None
                if candidate.lstrip("@").casefold() in known_host_names:
                    return None
                return candidate

            # GraphQL AudioSpace metadata is authoritative. Older releases
            # accidentally saved host names in title fields; never display
            # those as the Space title.
            title = (
                str(space_meta.get("title") or "").strip()
                or usable_space_title(s.title)
                or usable_space_title(file_meta.get("title"))
                or usable_space_title(folder_title)
                or "عنوان المساحة غير محفوظ"
            )
            if not host_display_name or host_display_name == host_user:
                host_display_name = (
                    file_meta.get("host_display_name")
                    or (folder_host if folder_host and not folder_host.startswith("@") else None)
                    or file_meta.get("host")
                    or host_display_name
                )
            host_avatar = s.host_account.avatar_url if s.host_account and s.host_account.avatar_url else None

            # Calculate duration in seconds
            dur_s = 0.0
            if s.started_at and s.ended_at:
                dur_s = (s.ended_at - s.started_at).total_seconds()
            if dur_s <= 0:
                st, et = file_meta.get("started_at"), file_meta.get("ended_at")
                if st and et:
                    try:
                        from datetime import datetime as _dt

                        dur_s = (_dt.fromisoformat(et) - _dt.fromisoformat(st)).total_seconds()
                    except (TypeError, ValueError):
                        pass
            if dur_s <= 0 and recorded_bytes:
                # Fallback duration estimate from media size (~450 kbps average)
                dur_s = round(recorded_bytes / (450_000 / 8), 1)

            has_thumb = files.get("thumbnail.jpg", 0) > 0 or bool(artifacts.get("screenshots_have_png"))
            has_vid = (
                files.get("playback.mp4", 0) > 0
                or files.get("recording.mkv", 0) > 0
                or any(int(files.get(name, 0)) > 0 for name in recording_names)
            )
            has_transcript = any(
                int(files.get(name, 0)) > 0
                for name in ("transcript.json", "transcript.srt", "transcript.vtt")
            )
            has_summary = int(files.get("summary.json", 0)) > 0
            transcript_state = "NOT_TRANSCRIBED"
            transcript_value: dict = {}
            if has_transcript:
                transcript_json_path = paths.root / "transcript.json"
                if transcript_json_path.is_file():
                    try:
                        loaded_transcript = json.loads(transcript_json_path.read_text(encoding="utf-8"))
                        if isinstance(loaded_transcript, dict):
                            transcript_value = loaded_transcript
                        transcript_segments = transcript_value.get("segments") or []
                        transcript_text = str(transcript_value.get("full_text") or "").strip()
                        transcript_state = (
                            "DONE"
                            if transcript_segments or transcript_text
                            else transcript_value.get("recognition_state") or "NO_SPEECH"
                        )
                    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                        transcript_state = "UNREADABLE"
                elif int(files.get("transcript.srt", 0)) or int(files.get("transcript.vtt", 0)):
                    transcript_state = "DONE"
            manifest_value: dict = {}
            try:
                manifest_value = json.loads(
                    (paths.root / "recording_manifest.json").read_text(encoding="utf-8")
                )
                if not isinstance(manifest_value, dict):
                    manifest_value = {}
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                pass
            capture_stop_reason = (file_meta.get("space_end") or {}).get("capture_stop_reason")
            if capture_stop_reason == "audio_silence_timeout":
                quality_status = "SILENT_CAPTURE_STOPPED"
            elif capture_stop_reason == "audio_monitoring_unavailable_timeout":
                quality_status = "AUDIO_MONITORING_UNAVAILABLE"
            elif manifest_value.get("status") == "PARTIAL":
                quality_status = "PARTIAL"
            elif transcript_state == "NO_SPEECH":
                quality_status = "NO_SPEECH"
            elif transcript_state == "DONE":
                quality_status = "SPEECH_DETECTED"
            else:
                quality_status = "NOT_ASSESSED"
            playback_bytes = int(files.get("playback.mp4", 0))
            playback_ready, playback_status = playback_readiness_from_metadata(
                files,
                mtimes,
                read_playback_source_manifest(paths.root) if "playback.mp4.source.json" in files else None,
            )
            has_analysis_artifact = any(
                int(files.get(name, 0)) > 0
                for name in (
                    "analysis.json",
                    "speaker_timeline.json",
                    "speaker_events.jsonl",
                    "speaker_observations.jsonl",
                )
            )
            has_saved_artifact = (
                has_vid or has_thumb or has_transcript or has_summary or has_analysis_artifact
            )
            if (
                not include_failed
                and not has_saved_artifact
                and s.state not in {"recording", "finalizing", "processing", "transcribing", "analyzing"}
            ):
                continue

            results.append(
                {
                    "space_id": s.space_id,
                    "title": title,
                    "host_username": host_user or "unknown",
                    "host_display_name": host_display_name or "",
                    "host_avatar": host_avatar,
                    "state": s.state,
                    "detected_at": s.detected_at.isoformat() if s.detected_at else None,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "recording_started_at": s.recording_started_at.isoformat()
                    if s.recording_started_at
                    else None,
                    "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                    "space_url": s.url or f"https://x.com/i/spaces/{s.space_id}",
                    "duration_seconds": dur_s,
                    "file_mb": file_mb,
                    "playback_mb": round(playback_bytes / 1_000_000, 1),
                    "total_media_mb": round((recorded_bytes + playback_bytes) / 1_000_000, 1),
                    "transcript_status": transcript_state,
                    "recording_quality": quality_status,
                    "recording_manifest_status": manifest_value.get("status", "UNKNOWN"),
                    "capture_stop_reason": capture_stop_reason,
                    "has_transcript": has_transcript,
                    "has_summary": has_summary,
                    "has_thumbnail": has_thumb,
                    "has_video": has_vid,
                    "playback_ready": playback_ready,
                    "playback_status": playback_status,
                    "media_preparation_state": media_preparation_by_space.get(s.space_id),
                    "has_saved_artifact": has_saved_artifact,
                    "join_outcome": s.join_outcome,
                    "join_checkpoint": s.join_checkpoint,
                    "processing_state": processing_by_space.get(s.space_id, {}).get("state"),
                    "processing_progress": processing_by_space.get(s.space_id, {}).get("progress"),
                    "processing_stage": processing_by_space.get(s.space_id, {}).get("stage"),
                    "processing_error": processing_by_space.get(s.space_id, {}).get("error"),
                    "processing_attempt": processing_by_space.get(s.space_id, {}).get("attempt"),
                    "status": describe_space_status(
                        state=s.state,
                        has_video=has_vid,
                        recording_quality=quality_status,
                        transcript_status=transcript_state,
                        has_summary=has_summary,
                        processing_state=processing_by_space.get(s.space_id, {}).get("state"),
                    ),
                    "folder_name": paths.root.name,
                    "folder_path": paths.root.relative_to(_cfg.data_dir).as_posix(),
                }
            )
        return results


@app.api_route("/spaces/{space_id}/thumbnail", methods=["GET", "HEAD"])
def get_space_thumbnail(space_id: str):
    """Serve a prepared thumbnail or a saved screenshot without running FFmpeg."""
    paths = SpacePaths.resolve_existing(_cfg.data_dir, space_id)
    if paths is None:
        raise HTTPException(404, "Thumbnail not found")
    thumb = paths.thumbnail
    if thumb.is_file() and thumb.stat().st_size > 0:
        return FileResponse(thumb, media_type="image/jpeg")

    # Check screenshots
    if paths.screenshots.exists():
        shots = sorted(paths.screenshots.glob("*.png"))
        if shots:
            return FileResponse(shots[-1], media_type="image/png")

    raise HTTPException(404, "Thumbnail not found")


@app.get("/spaces/{space_id}/info")
def get_space_info_document(space_id: str):
    """Return the local HTML information sheet saved beside a Space recording."""
    import json as _json

    from xsa.space_document import write_space_info_html

    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if not space:
            raise HTTPException(404, "Space not found")
        metadata = {
            "space_id": space.space_id,
            "title": space.title or "",
            "url": space.url or f"https://x.com/i/spaces/{space.space_id}",
            "host": space.host_account.username if space.host_account else space.verified_host_username or "",
            "host_display_name": space.host_account.display_name
            if space.host_account
            else space.verified_host_username or "",
            "host_avatar_url": space.host_account.avatar_url if space.host_account else "",
            "started_at": (space.recording_started_at or space.started_at or space.detected_at).isoformat(),
        }
        exact_title = (
            (space.space_metadata or {}).get("title") if isinstance(space.space_metadata, dict) else None
        )
        known_host_names = {
            str(value).strip().lstrip("@").casefold()
            for value in (metadata.get("host"), metadata.get("host_display_name"))
            if value
        }

    paths = SpacePaths.find_space(_cfg.data_dir, space_id)
    if not paths.root.is_dir():
        raise HTTPException(404, "Space folder not found")
    if paths.space_json.is_file():
        try:
            stored = _json.loads(paths.space_json.read_text(encoding="utf-8"))
            metadata = {**metadata, **{k: v for k, v in stored.items() if v not in (None, "")}}
        except (OSError, ValueError, TypeError):
            pass

    parts = paths.root.name.split(" - ")
    if len(parts) >= 4 and re.fullmatch(r"\d{6}", parts[0]) and parts[-1] == space_id:
        folder_title = parts[1]
        if (
            not metadata.get("title")
            or metadata["title"] in {"عنوان غير مسجل", "عنوان المساحة غير محفوظ", "مساحة مسجلة"}
            or re.fullmatch(r"\d{6}", str(metadata["title"]))
            or str(metadata["title"]).strip().lstrip("@").casefold() in known_host_names
        ):
            if folder_title.strip().lstrip("@").casefold() not in known_host_names:
                metadata["title"] = folder_title
            else:
                metadata["title"] = exact_title or "عنوان المساحة غير محفوظ"
        if (
            not metadata.get("host_display_name") or metadata.get("host_display_name") == metadata.get("host")
        ) and not parts[2].startswith("@"):
            metadata["host_display_name"] = parts[2]
    if str(metadata.get("title") or "").strip().lstrip("@").casefold() in known_host_names:
        metadata["title"] = exact_title or "عنوان المساحة غير محفوظ"
    if paths.thumbnail.is_file() and not metadata.get("icon_path"):
        metadata["icon_path"] = "thumbnail.jpg"

    output = write_space_info_html(paths.root, metadata)
    return FileResponse(output, media_type="text/html; charset=utf-8")


@app.api_route("/spaces/{space_id}/video", methods=["GET", "HEAD"])
def stream_space_video(space_id: str, download: bool = False):
    """Serve an already prepared recording without probing or remuxing on GET."""
    paths = SpacePaths.resolve_existing(_cfg.data_dir, space_id)
    if paths is None:
        raise HTTPException(404, "Video recording not found")
    disp_type = "attachment" if download else "inline"
    download_filename = f"{space_id}.mp4" if download else None

    if _monitor and space_id in _monitor._active_sessions:
        raise HTTPException(409, "Recording is active; playback is available after finalization")

    playback_ready, _ = playback_readiness_for_paths(paths)
    if playback_ready:
        return FileResponse(
            paths.playback,
            media_type="video/mp4",
            filename=download_filename,
            content_disposition_type=disp_type,
        )
    raw_sources = [
        path
        for path in (paths.recording, *paths.root.glob("recording_[0-9][0-9][0-9].mkv"))
        if path.is_file() and path.stat().st_size > 0
    ]
    if not raw_sources:
        raise HTTPException(404, "Video recording not found")
    direct_source = paths.recording if paths.recording in raw_sources else raw_sources[0]
    if download and (paths.recording in raw_sources or len(raw_sources) == 1):
        return FileResponse(
            direct_source,
            media_type="video/x-matroska",
            filename=f"{space_id}.mkv",
            content_disposition_type="attachment",
        )
    return JSONResponse(
        {
            "status": "preparing",
            "detail": "Playback MP4 is not ready",
            "prepare_url": f"/spaces/{space_id}/prepare-media",
        },
        status_code=202,
        headers={"Retry-After": "5"},
    )


@app.get("/spaces/{space_id}/media-status")
def get_space_media_status(space_id: str):
    """Report prepared file availability and the durable preparation job."""
    with get_session() as db:
        row = db.query(Space).filter_by(space_id=space_id).first()
        if row is None:
            raise HTTPException(404, "Space not found")
        job = (
            db.query(Job)
            .filter(Job.space_id == row.id, Job.job_type == "prepare_media")
            .order_by(Job.id.desc())
            .first()
        )
        job_state, job_error = (job.state, job.error) if job else (None, None)
    paths = SpacePaths.resolve_existing(_cfg.data_dir, space_id)
    playback_ready, playback_reason = playback_readiness_for_paths(paths) if paths else (False, "missing")
    source_available = bool(
        paths and (paths.recording.is_file() or any(paths.root.glob("recording_[0-9][0-9][0-9].mkv")))
    )
    return {
        "space_id": space_id,
        "status": "ready"
        if playback_ready
        else "preparing"
        if job_state in {"queued", "running"}
        else "failed"
        if job_state == "failed"
        else "needs_preparation"
        if source_available
        else "unavailable",
        "playback_ready": playback_ready,
        "playback_reason": playback_reason,
        "thumbnail_ready": bool(paths and paths.thumbnail.is_file()),
        "source_available": source_available,
        "job_state": job_state,
        "error": job_error,
    }


@app.post("/spaces/{space_id}/prepare-media")
async def prepare_space_media(space_id: str):
    """Queue media-only preparation after capture; never redo transcription."""
    if maintenance_active(_cfg.db_path.parent):
        raise HTTPException(423, "Maintenance mode is active")
    if not _monitor:
        raise HTTPException(503, "Processing service not ready")
    with get_session() as db:
        row = db.query(Space).filter_by(space_id=space_id).first()
        if row is None:
            raise HTTPException(404, "Space not found")
        if row.state in {"joining", "recording", "finalizing"}:
            raise HTTPException(409, "Recording source is still active")
        db_id = row.id
    paths = SpacePaths.resolve_existing(_cfg.data_dir, space_id)
    if paths is None or not (
        paths.recording.is_file() or any(paths.root.glob("recording_[0-9][0-9][0-9].mkv"))
    ):
        raise HTTPException(404, "Recording source not found")
    playback_ready, _ = playback_readiness_for_paths(paths)
    if playback_ready and paths.thumbnail.is_file() and paths.thumbnail.stat().st_size > 0:
        return {"status": "ready", "space_id": space_id}
    queued = _monitor.schedule_media_preparation(space_id, db_id, paths)
    return {"status": "queued" if queued else "already_processing", "space_id": space_id}


@app.post("/spaces/{space_id}/transcribe")
async def trigger_transcribe(space_id: str):
    """Trigger the transcription pipeline in the background."""
    if maintenance_active(_cfg.db_path.parent):
        raise HTTPException(423, "Maintenance mode is active")
    if not _monitor:
        raise HTTPException(503, "Processing service not ready")
    with get_session() as db:
        row = db.query(Space).filter_by(space_id=space_id).first()
        if not row:
            raise HTTPException(404, "Space not found")
        if row.state in {"joining", "recording", "finalizing"}:
            raise HTTPException(409, "Recording source is still active")
        db_id, started_at = row.id, row.started_at or row.detected_at
    paths = SpacePaths.for_space(_cfg.data_dir, space_id, started_at)
    queued = _monitor.schedule_post_processing(space_id, db_id, paths, retry=True)
    return {"status": "queued" if queued else "already_processing", "space_id": space_id}


@app.get("/spaces/{space_id}/transcript")
def get_space_transcript(space_id: str):
    """Return aligned transcript JSON."""
    import json as _json

    paths = SpacePaths(_cfg.data_dir, space_id)
    if not paths.transcript_json.exists():
        return {"space_id": space_id, "segments": [], "message": "Transcript not generated yet"}
    try:
        return _json.loads(paths.transcript_json.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, detail=str(e)) from e


@app.get("/spaces/{space_id}/transcript/partial")
def get_partial_space_transcript(space_id: str):
    """Expose only source-verified Whisper checkpoints as an unfinished draft."""
    from xsa.transcriber.checkpoints import read_partial_transcript

    with get_session() as db:
        if db.query(Space.id).filter_by(space_id=space_id).first() is None:
            raise HTTPException(404, "Space not found")
    paths = SpacePaths.resolve_existing(_cfg.data_dir, space_id)
    if paths is None:
        return {"space_id": space_id, "recognition_state": "NOT_STARTED", "segments": []}
    return {"space_id": space_id, **read_partial_transcript(paths.root)}


@app.get("/spaces/{space_id}/transcript/srt")
def get_space_transcript_srt(space_id: str):
    """Download or view SRT subtitles."""
    paths = SpacePaths(_cfg.data_dir, space_id)
    if not paths.transcript_srt.exists():
        raise HTTPException(404, "SRT file not found")
    content = paths.transcript_srt.read_text(encoding="utf-8")
    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")


@app.get("/spaces/{space_id}/transcript/vtt")
def get_space_transcript_vtt(space_id: str):
    """Download or view WebVTT subtitles."""
    paths = SpacePaths(_cfg.data_dir, space_id)
    if not paths.transcript_vtt.exists():
        raise HTTPException(404, "VTT file not found")
    content = paths.transcript_vtt.read_text(encoding="utf-8")
    return PlainTextResponse(content=content, media_type="text/vtt; charset=utf-8")


# --- Phase 6: AI Analysis Endpoints ---


@app.post("/spaces/{space_id}/analyze")
async def trigger_analyze(space_id: str):
    """Trigger the AI analysis / summarization pipeline."""
    from xsa.analyzer.summarizer import analyze_space

    try:
        res = await analyze_space(space_id, _cfg)
        return {"status": "ok", "result": res}
    except Exception as e:
        log.error("Analysis error", space_id=space_id, error=str(e))
        raise HTTPException(500, detail=str(e)) from e


@app.get("/spaces/{space_id}/summary")
def get_space_summary(space_id: str):
    """Return AI summary and key topics JSON."""
    import json as _json

    paths = SpacePaths(_cfg.data_dir, space_id)
    if not paths.summary_json.exists():
        return {
            "space_id": space_id,
            "executive_summary": "لم يتم توليد الملخص بعد",
            "key_topics": [],
            "questions": [],
        }
    try:
        return _json.loads(paths.summary_json.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, detail=str(e)) from e


@app.get("/debug/inspect_space")
async def debug_inspect_space():
    if not _adapter or not _adapter._page:
        return {"error": "no page"}
    return await _adapter._page.evaluate("""
    () => {
        const buttons = Array.from(document.querySelectorAll('button, [role="button"]')).map(b => ({
            tag: b.tagName,
            aria: b.getAttribute('aria-label') || '',
            testid: b.getAttribute('data-testid') || '',
            text: (b.innerText || '').trim().substring(0, 40),
            className: (b.className || '').toString().substring(0, 40),
            x: Math.round(b.getBoundingClientRect().x),
            y: Math.round(b.getBoundingClientRect().y),
            w: Math.round(b.getBoundingClientRect().width),
            h: Math.round(b.getBoundingClientRect().height),
        })).filter(b => b.w > 0 && b.h > 0);
        return {
            url: window.location.href,
            buttons: buttons,
        };
    }
    """)
