from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from xsa.db import Job, Space, get_session, init_db
from xsa.monitor.monitor import AccountMonitor
from xsa.paths import SpacePaths


@pytest.fixture
def processing_db(tmp_path):
    from xsa.db import database as db_module

    db_path = tmp_path / "state" / "processing.sqlite3"
    init_db(db_path)
    with get_session() as db:
        space = Space(
            space_id="processTest123",
            state="processing",
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(space)
        db.commit()
        db.refresh(space)
        space_db_id = space.id
    yield tmp_path, space_db_id
    if db_module._engine:
        db_module._engine.dispose()
        db_module._engine = None
        db_module._SessionLocal = None


def _monitor(data_dir: Path) -> AccountMonitor:
    monitor = object.__new__(AccountMonitor)
    monitor._cfg = SimpleNamespace(data_dir=data_dir)
    monitor._post_processing_tasks = {}
    monitor._post_processing_lock = asyncio.Lock()
    return monitor


@pytest.mark.asyncio
async def test_post_processing_job_is_persisted_and_completed(processing_db):
    data_dir, space_db_id = processing_db
    monitor = _monitor(data_dir)

    async def finish(*_args):
        return None

    monitor._run_post_processing = finish
    paths = SpacePaths.for_space(data_dir, "processTest123", datetime.now(UTC).replace(tzinfo=None))
    assert monitor.schedule_post_processing("processTest123", space_db_id, paths)
    task = monitor._post_processing_tasks["processTest123"]
    await task

    with get_session() as db:
        jobs = db.query(Job).filter_by(space_id=space_db_id, job_type="post_process").all()
        assert len(jobs) == 1
        assert jobs[0].state == "completed"
        assert jobs[0].attempt == 1
        assert jobs[0].progress == 1.0


def test_post_processing_stage_progress_is_persisted(processing_db):
    data_dir, space_db_id = processing_db
    monitor = _monitor(data_dir)
    with get_session() as db:
        job = Job(space_id=space_db_id, job_type="post_process", state="running")
        db.add(job)
        db.commit()
        job_id = job.id

    monitor._set_post_processing_progress(job_id, 0.35, "transcription")

    with get_session() as db:
        job = db.get(Job, job_id)
        assert job.progress == 0.35
        assert job.result == {"stage": "transcription"}


def test_processing_jobs_exposes_measured_window_progress(processing_db):
    from importlib import import_module

    from fastapi import HTTPException

    _data_dir, space_db_id = processing_db
    monitor = _monitor(_data_dir)
    with get_session() as db:
        job = Job(space_id=space_db_id, job_type="post_process", state="running", attempt=2)
        db.add(job)
        db.commit()
        job_id = job.id
    monitor._set_post_processing_progress(
        job_id, 0.565, "transcription", {"completed_windows": 3, "total_windows": 6}
    )
    api = import_module("xsa.api.app")
    item = api.list_processing_jobs(limit=10)["items"][0]
    assert item["space_id"] == "processTest123"
    assert item["stage"] == "transcription"
    assert (item["completed_windows"], item["total_windows"]) == (3, 6)
    assert item["progress_basis"] == "pipeline_stage"
    with pytest.raises(HTTPException) as error:
        api.list_processing_jobs(limit=201)
    assert error.value.status_code == 400


def test_startup_requeues_interrupted_post_processing(processing_db):
    from xsa.api.app import recover_interrupted_post_processing_jobs

    _data_dir, space_db_id = processing_db
    with get_session() as db:
        db.add(Job(space_id=space_db_id, job_type="post_process", state="running", attempt=1))
        db.commit()

    assert recover_interrupted_post_processing_jobs() == 1
    with get_session() as db:
        job = db.query(Job).filter_by(space_id=space_db_id, job_type="post_process").one()
        assert job.state == "queued"
        assert job.worker_id is None
        assert "Recovered after application restart" in job.error


@pytest.mark.asyncio
async def test_failed_post_processing_can_be_explicitly_retried(processing_db):
    data_dir, space_db_id = processing_db
    monitor = _monitor(data_dir)
    with get_session() as db:
        db.add(Job(space_id=space_db_id, job_type="post_process", state="failed", attempt=1, error="old"))
        db.commit()

    async def finish(*_args):
        return None

    monitor._run_post_processing = finish
    paths = SpacePaths.for_space(data_dir, "processTest123", datetime.now(UTC).replace(tzinfo=None))
    assert monitor.schedule_post_processing("processTest123", space_db_id, paths, retry=True)
    await monitor._post_processing_tasks["processTest123"]

    with get_session() as db:
        job = db.query(Job).filter_by(space_id=space_db_id, job_type="post_process").one()
        assert job.state == "completed"
        assert job.attempt == 1
        assert job.error is None


@pytest.mark.asyncio
async def test_media_preparation_is_durable_and_does_not_rerun_analysis(processing_db):
    data_dir, space_db_id = processing_db
    monitor = _monitor(data_dir)

    async def prepared(*_args):
        return None

    async def should_not_run(*_args):
        pytest.fail("media preparation reran transcription and analysis")

    monitor._run_media_preparation = prepared
    monitor._run_post_processing = should_not_run
    paths = SpacePaths.for_space(data_dir, "processTest123", datetime.now(UTC).replace(tzinfo=None))
    assert monitor.schedule_media_preparation("processTest123", space_db_id, paths)
    await monitor._post_processing_tasks["processTest123"]

    with get_session() as db:
        job = db.query(Job).filter_by(space_id=space_db_id, job_type="prepare_media").one()
        assert job.state == "completed"
        assert job.result["playback_ready"] is True
        assert db.get(Space, space_db_id).state == "processing"


def test_startup_requeues_interrupted_media_preparation(processing_db):
    from xsa.api.app import recover_interrupted_post_processing_jobs

    _data_dir, space_db_id = processing_db
    with get_session() as db:
        db.add(Job(space_id=space_db_id, job_type="prepare_media", state="running", attempt=1))
        db.commit()

    assert recover_interrupted_post_processing_jobs() == 1
    with get_session() as db:
        assert db.query(Job).filter_by(job_type="prepare_media").one().state == "queued"


@pytest.mark.asyncio
async def test_media_status_and_explicit_queue_do_not_start_work_on_get(processing_db, monkeypatch):
    from importlib import import_module

    data_dir, space_db_id = processing_db
    api = import_module("xsa.api.app")
    paths = SpacePaths.for_space(data_dir, "processTest123", datetime.now(UTC).replace(tzinfo=None))
    paths.ensure()
    paths.segment(0).write_bytes(b"fixture media")
    calls = []
    monitor = SimpleNamespace(
        schedule_media_preparation=lambda *args: calls.append(args) or True,
    )
    monkeypatch.setattr(
        api, "_cfg", SimpleNamespace(data_dir=data_dir, db_path=data_dir / "state" / "processing.sqlite3")
    )
    monkeypatch.setattr(api, "_monitor", monitor)

    status = api.get_space_media_status("processTest123")
    assert status["status"] == "needs_preparation"
    assert status["source_available"] is True
    assert calls == []

    queued = await api.prepare_space_media("processTest123")
    assert queued["status"] == "queued"
    assert calls[0][0:2] == ("processTest123", space_db_id)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg unavailable")
@pytest.mark.asyncio
async def test_media_job_prepares_playback_and_thumbnail_from_real_source(processing_db):
    data_dir, space_db_id = processing_db
    paths = SpacePaths.for_space(data_dir, "processTest123", datetime.now(UTC).replace(tzinfo=None))
    paths.ensure()
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(paths.segment(0)),
        ],
        check=True,
        timeout=30,
    )

    monitor = _monitor(data_dir)
    assert monitor.schedule_media_preparation("processTest123", space_db_id, paths)
    await monitor._post_processing_tasks["processTest123"]

    assert paths.playback.stat().st_size > 0
    assert paths.thumbnail.stat().st_size > 0
    with get_session() as db:
        assert db.query(Job).filter_by(job_type="prepare_media").one().state == "completed"
