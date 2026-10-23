"""Regressions for the production defects in PROJECT_AUDIT_2026-10-14."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xsa.config import Settings
from xsa.db import Job, MonitoredAccount, Space, get_session, init_db
from xsa.paths import SpacePaths
from xsa.space_end import SpaceEndDetector, SpaceEndState


def _native_writer(started: str, completed: str):
    Path(started).write_text(str(os.getpid()))
    time.sleep(10)
    Path(completed).write_text("must not be written after cancellation")


@pytest.mark.asyncio
async def test_a06_owned_native_worker_is_killed_and_reaped_before_ack(tmp_path):
    import psutil

    from xsa.owned_process import run_owned_process

    started, completed = tmp_path / "started", tmp_path / "completed"
    task = asyncio.create_task(run_owned_process(_native_writer, str(started), str(completed)))
    async with asyncio.timeout(10):
        while not started.exists():
            await asyncio.sleep(0.05)
    pid = int(started.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not psutil.pid_exists(pid)
    assert not completed.exists()


@pytest.fixture
def audit_db(tmp_path):
    from xsa.db import database

    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "xsa.sqlite3")
    cfg.data_dir.mkdir()
    init_db(cfg.db_path)
    with get_session() as db:
        db.add(MonitoredAccount(username="audit_host", active=True))
        db.add(Space(space_id="auditSpace", state="ended"))
        db.commit()
    yield cfg
    database._engine.dispose()
    database._engine = None
    database._SessionLocal = None


def test_a01_metadata_end_survives_thirteen_mixed_poll_cycles():
    detector = SpaceEndDetector(space_id="auditSpace")
    ended = {"state": "ENDED", "source": "metadata", "confidence": 0.99, "space_id": "auditSpace"}
    for second in range(61):
        detector.observe(
            ended
            if second % 5 == 0
            else {
                "state": "UNKNOWN",
                "source": "x_dom",
                "space_id": "auditSpace",
                "source_checks": {"metadata": {"state": "NOT_CHECKED_THIS_CYCLE"}},
            },
            now=second,
        )
        if second == 5:
            assert detector.state == SpaceEndState.ENDED
    snapshot = detector.snapshot()
    assert snapshot["end_confirmation_count"] == 2
    assert snapshot["end_evidence_by_source"]["metadata"]["space_id"] == "auditSpace"
    assert snapshot["end_evidence_by_source"]["metadata"]["last_ended_evidence_at"]


def test_a01_only_reliable_same_space_live_clears_suspicion():
    detector = SpaceEndDetector(space_id="auditSpace")
    detector.observe({"state": "ENDED", "source": "metadata", "space_id": "auditSpace"}, 0)
    for source, confidence, sid in (("x_dom", 0.75, "auditSpace"), ("metadata", 0.99, "other")):
        assert (
            detector.observe(
                {"state": "LIVE", "source": source, "confidence": confidence, "space_id": sid}, 1
            )
            == SpaceEndState.SUSPECTED_ENDED
        )
    assert (
        detector.observe(
            {"state": "LIVE", "source": "metadata", "confidence": 0.99, "space_id": "auditSpace"}, 2
        )
        == SpaceEndState.LIVE
    )


def test_a01_visible_dock_cannot_extend_stale_live_metadata_forever():
    detector = SpaceEndDetector(space_id="auditSpace")
    detector.observe({"state": "LIVE", "source": "metadata", "confidence": 0.99, "space_id": "auditSpace"}, 0)
    dock = {"state": "LIVE", "source": "x_dom", "confidence": 0.75, "space_id": "auditSpace"}
    assert detector.observe(dock, 5) == SpaceEndState.LIVE
    assert detector.observe(dock, 20) == SpaceEndState.UNKNOWN


@pytest.mark.parametrize("timestamp", ["-0.0464583", "+0.5", "1", ".25", "1e-2"])
def test_a02_signed_silence_preserves_media_time(tmp_path, monkeypatch, timestamp):
    from xsa.recorder.recorder import DockerRecorder

    recorder = DockerRecorder()
    recorder._proc = SimpleNamespace(poll=lambda: None)
    recorder._output = tmp_path / "recording_000.mkv"
    recorder._start_wall = 0
    (tmp_path / "ffmpeg_0.log").write_text(f"silence_start: {timestamp}\n")
    monkeypatch.setattr("xsa.recorder.recorder.time.monotonic", lambda: 100)
    value = recorder.get_audio_silence_status()
    assert value["audio_measurement"] == "healthy"
    assert value["audio_silent"] is True
    assert value["silence_start_original_seconds"] == float(timestamp)
    assert value["silence_start_media_seconds"] == max(0, float(timestamp))
    assert value["silence_duration_seconds"] == round(100 - max(0, float(timestamp)), 1)


def test_a02_unmeasured_audio_is_not_claimed_audible(tmp_path):
    from xsa.recorder.recorder import DockerRecorder

    recorder = DockerRecorder()
    recorder._proc = SimpleNamespace(poll=lambda: None)
    recorder._output = tmp_path / "recording_000.mkv"
    (tmp_path / "ffmpeg_0.log").write_text("no measurements\n")
    assert recorder.get_audio_silence_status()["audio_silent"] is None
    assert recorder.get_audio_silence_status()["audio_measurement"] == "unavailable"


def test_a02_audio_readiness_requires_chrome_on_reserved_sink(monkeypatch):
    from xsa.recorder import recorder

    sinks = [{"name": "reserved", "index": 7}, {"name": "unrelated", "index": 8}]
    inputs = [{"sink": 8, "corked": False, "properties": {"application.process.binary": "chrome"}}]
    monkeypatch.setattr(
        recorder.subprocess,
        "check_output",
        lambda command, **_k: json.dumps(sinks if command[-1] == "sinks" else inputs).encode(),
    )
    assert not recorder.browser_audio_route_ready("reserved")
    inputs[0]["sink"] = 7
    assert recorder.browser_audio_route_ready("reserved")
    inputs[0]["corked"] = True
    assert not recorder.browser_audio_route_ready("reserved")


@pytest.mark.asyncio
async def test_a03_failed_anonymous_toggle_never_uses_normal_listen(tmp_path):
    from xsa.browser.adapter import XBrowserAdapter, _Sel

    toggle = SimpleNamespace(
        get_attribute=AsyncMock(return_value="false"),
        click=AsyncMock(side_effect=RuntimeError("click failed")),
    )

    async def selector(value):
        return toggle if value == _Sel.ANONYMOUS_TOGGLE else None

    page = SimpleNamespace(
        url="https://x.com/i/spaces/auditSpace/peek",
        goto=AsyncMock(),
        title=AsyncMock(return_value="Space"),
        query_selector=selector,
        wait_for_selector=AsyncMock(),
    )
    adapter = XBrowserAdapter(tmp_path)
    adapter._page = page
    adapter.get_space_end_evidence = AsyncMock(return_value={"state": "LIVE"})
    assert not await adapter.openSpace("https://x.com/i/spaces/auditSpace")
    assert adapter.join_anonymous_listen_state == "ANONYMOUS_ACTIVATION_FAILED"
    assert not any(call.args[0] == _Sel.START_LISTENING for call in page.wait_for_selector.call_args_list)


@pytest.mark.asyncio
async def test_a04_manual_and_automatic_share_one_start_reservation(audit_db, monkeypatch):
    from xsa.browser import SpaceState
    from xsa.monitor.monitor import AccountMonitor

    api = importlib.import_module("xsa.api.app")
    monitor = AccountMonitor(SimpleNamespace(), audit_db)
    barrier = asyncio.Event()
    starts = []

    async def prepare(username, state, *, manual_capture=False):
        starts.append(state.space_id)
        await barrier.wait()
        monitor._active_sessions[state.space_id] = SimpleNamespace()
        return state.space_id

    monitor._prepare_detected_space = prepare
    monkeypatch.setattr(api, "_monitor", monitor)
    monkeypatch.setattr(api, "_adapter", monitor._adapter)
    monkeypatch.setattr(api, "_cfg", audit_db)
    automatic = asyncio.create_task(
        monitor._start_detected_space("audit_host", SpaceState(space_id="raceSpace"))
    )
    await asyncio.sleep(0)
    with pytest.raises(api.HTTPException) as duplicate:
        await api.trigger_record_space(api.RecordSpaceIn(url="https://x.com/i/spaces/raceSpace"))
    assert duplicate.value.status_code == 409
    barrier.set()
    assert await automatic == "raceSpace"
    assert starts == ["raceSpace"]
    assert len(monitor._active_sessions) == len(monitor._active_tasks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,outcome", [(429, "graphql_failure"), (None, "browser_failure")])
async def test_a03_unrendered_space_ui_is_not_anonymous_unavailable(tmp_path, monkeypatch, status, outcome):
    from xsa.browser import adapter as module

    monkeypatch.setattr(module, "_viewer_backoff_path", lambda: tmp_path / "state" / "x-viewer-backoff.json")
    monkeypatch.setattr(module, "_viewer_retry_until", 0.0)
    monkeypatch.setattr(module, "_viewer_429_streak", 0)
    adapter = module.XBrowserAdapter(tmp_path)

    async def navigate(*_args, **_kwargs):
        if status:
            adapter._bootstrap_error = module.observe_viewer_response(
                SimpleNamespace(
                    url="https://x.com/i/api/graphql/operation/Viewer?private=not-for-output",
                    status=status,
                    headers={"retry-after": "120"},
                )
            )

    page = SimpleNamespace(
        url="https://x.com/i/spaces/auditSpace/peek",
        goto=navigate,
        title=AsyncMock(return_value=""),
        query_selector=AsyncMock(return_value=None),
        wait_for_selector=AsyncMock(side_effect=TimeoutError()),
    )
    adapter._page = page
    assert not await adapter.openSpace(page.url)
    assert adapter.join_outcome == outcome
    assert adapter.join_anonymous_listen_state == "ANONYMOUS_STATE_UNKNOWN"
    assert "private" not in (adapter.join_last_error or "")
    assert adapter.join_checkpoint == "space_ui_unavailable"


@pytest.mark.asyncio
async def test_x_viewer_rate_limit_backs_off_discovery_without_stopping_capture(audit_db, monkeypatch):
    from xsa.browser import adapter as module
    from xsa.monitor.monitor import AccountMonitor

    monkeypatch.setattr(
        module,
        "_viewer_backoff_path",
        lambda: audit_db.db_path.parent / "x-viewer-backoff.json",
    )
    monkeypatch.setattr(module, "_viewer_retry_until", 0.0)
    monkeypatch.setattr(module, "_viewer_429_streak", 0)
    assert (
        module.observe_viewer_response(
            SimpleNamespace(
                url="https://unrelated.example/Viewer",
                status=429,
                headers={},
            )
        )
        is None
    )
    assert module.viewer_retry_remaining() == 0
    module.observe_viewer_response(
        SimpleNamespace(
            url="https://x.com/i/api/graphql/operation/Viewer",
            status=429,
            headers={"retry-after": "120"},
        )
    )
    adapter = SimpleNamespace(ensureLoggedIn=AsyncMock())
    monitor = AccountMonitor(adapter, audit_db)
    session = SimpleNamespace(stop=AsyncMock())
    monitor._active_sessions["realCapture"] = session
    monitor._get_polling_page = AsyncMock()
    await monitor._scan_live_sidebar()
    await monitor._poll_all_accounts()
    adapter.ensureLoggedIn.assert_not_called()
    monitor._get_polling_page.assert_not_called()
    session.stop.assert_not_called()
    assert 0 < module.viewer_retry_remaining() <= 120
    assert not module.viewer_poll_allowed()
    persisted = json.loads(module._viewer_backoff_path().read_text(encoding="utf-8"))
    assert persisted["retry_until_epoch"] > time.time()

    # A new app process must restore the cooldown and keep profile discovery closed.
    module._viewer_retry_until = 0.0
    module._viewer_429_streak = 0
    module._load_viewer_backoff_state(force=True)
    assert 0 < module.viewer_retry_remaining() <= 120
    assert not module.viewer_poll_allowed()
    module.observe_viewer_response(
        SimpleNamespace(
            url="https://x.com/i/api/graphql/operation/Viewer",
            status=200,
            headers={},
        )
    )
    assert module._viewer_429_streak == 0
    assert module.viewer_retry_remaining() > 0
    assert json.loads(module._viewer_backoff_path().read_text(encoding="utf-8"))["rate_limit_streak"] == 0
    api = importlib.import_module("xsa.api.app")
    monkeypatch.setattr(api, "_monitor", monitor)
    monkeypatch.setattr(api, "_adapter", adapter)
    monkeypatch.setattr(api, "_cfg", audit_db)
    queued = await api.trigger_record_space(api.RecordSpaceIn(url="https://x.com/i/spaces/freshAuditSpace"))
    assert queued["status"] == "queued"
    assert queued["retry_after_seconds"] > 0
    assert not monitor._starting_spaces and not monitor._active_tasks


def test_x_viewer_embedded_apierror_429_starts_shared_backoff(tmp_path, monkeypatch):
    from xsa.browser import adapter as module

    monkeypatch.setattr(module, "_viewer_backoff_path", lambda: tmp_path / "x-viewer-backoff.json")
    monkeypatch.setattr(module, "_viewer_retry_until", 0.0)
    monkeypatch.setattr(module, "_viewer_429_streak", 0)
    failure = module.observe_viewer_page_error(
        "ApiError: https://x.com/i/api/graphql/TpQxQtWMTMnIAMPlMGMqNw/Viewer HTTP-429 codes:[1003]"
    )
    assert failure == {"operation": "Viewer", "http_status": 429, "retry_after_seconds": 900.0}
    assert 0 < module.viewer_retry_remaining() <= 900
    assert module.observe_viewer_page_error("Unrelated Javascript error") is None


@pytest.mark.asyncio
async def test_a04_preparation_is_inside_outer_deadline(audit_db, monkeypatch):
    from xsa.browser import SpaceState
    from xsa.monitor.monitor import AccountMonitor

    cfg = SimpleNamespace(**audit_db.model_dump())
    cfg.session_start_timeout_seconds = 0.04
    monitor = AccountMonitor(SimpleNamespace(), cfg)

    async def stalled(_username, _state, *, manual_capture=False):
        with get_session() as db:
            row = db.query(Space).filter_by(space_id="auditSpace").one()
            monitor._begin_join(row.id, datetime.now(UTC), 0.04)
        await asyncio.Event().wait()

    monitor._prepare_detected_space = stalled
    assert await monitor._start_detected_space("audit_host", SpaceState(space_id="auditSpace")) is None
    assert monitor._active_tasks == monitor._starting_spaces == {}
    with get_session() as db:
        row = db.query(Space).filter_by(space_id="auditSpace").one()
        assert row.join_outcome == "join_timeout" and row.state == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "automatic", "existing_media", "started_capture"])
async def test_a04_retry_only_explicit_failed_pre_capture_join(audit_db, mode):
    from xsa.browser import SpaceState
    from xsa.db import SystemEvent
    from xsa.monitor.monitor import AccountMonitor

    with get_session() as db:
        row = db.query(Space).filter_by(space_id="auditSpace").one()
        row.state = "rejected"
        row.join_outcome = "anonymous_listening_unavailable"
        row.join_checkpoint = "anonymous_listening"
        if mode == "started_capture":
            row.recording_started_at = datetime.now(UTC)
        db.commit()
    if mode == "existing_media":
        paths = SpacePaths.for_space(audit_db.data_dir, "auditSpace", datetime.now(UTC))
        paths.root.mkdir(parents=True)
        (paths.root / "recording_000.mkv").write_bytes(b"existing capture")
    monitor = AccountMonitor(SimpleNamespace(), audit_db)
    seen = []

    async def prepare(_username, _state, *, manual_capture=False):
        with get_session() as db:
            row = db.query(Space).filter_by(space_id="auditSpace").one()
            seen.append((row.state, row.join_outcome))
        return None

    monitor._prepare_detected_space = prepare
    await monitor._start_detected_space(
        "audit_host", SpaceState(space_id="auditSpace"), manual_retry=mode != "automatic"
    )
    assert (
        seen == [("detected", None)]
        if mode == "manual"
        else seen == [("rejected", "anonymous_listening_unavailable")]
    )
    with get_session() as db:
        events = db.query(SystemEvent).all()
        assert len(events) == (1 if mode == "manual" else 0)
        if events:
            assert events[0].details["previous_outcome"] == "anonymous_listening_unavailable"
    assert not monitor._starting_spaces and not monitor._active_tasks


@pytest.mark.asyncio
async def test_a04_full_start_path_allocates_one_slot_browser_and_join_task(audit_db, monkeypatch):
    from xsa.browser import SpaceState
    from xsa.monitor import monitor as module
    from xsa.recorder import slot_pool

    cfg = SimpleNamespace(**audit_db.model_dump())
    cfg.session_start_timeout_seconds = 1.0
    counts = {"slot": 0, "browser": 0, "join": 0, "release": 0}
    adapter = SimpleNamespace(
        _page=None,
        close=AsyncMock(),
        join_checkpoint="waiting_for_player",
        join_outcome=None,
        join_host_verification_state="verified",
        join_anonymous_listen_state="ANONYMOUS_CONFIRMED",
        join_space_state="live",
    )

    async def allocate():
        counts["slot"] += 1
        return SimpleNamespace(display=":test", sink_name="test", slot_id=0)

    async def release(_slot):
        counts["release"] += 1

    async def spawn(**_kwargs):
        counts["browser"] += 1
        await asyncio.sleep(0.4)
        return adapter

    async def join():
        counts["join"] += 1
        await asyncio.Event().wait()

    def capture(**kwargs):
        return SimpleNamespace(
            **kwargs,
            start=join,
            abort_join=AsyncMock(),
            join_outcome=None,
            join_last_error=None,
            _recorder=SimpleNamespace(_proc=None),
            report_join_progress=AsyncMock(),
        )

    monkeypatch.setattr(
        slot_pool, "get_slot_pool", lambda **_k: SimpleNamespace(acquire_slot=allocate, release_slot=release)
    )
    monkeypatch.setattr(module, "SpaceSession", capture)
    monitor = module.AccountMonitor(SimpleNamespace(spawn_worker_adapter=spawn), cfg)
    state = SpaceState(space_id="fullRace", url="https://x.com/i/spaces/fullRace", state="live")
    began = asyncio.get_running_loop().time()
    results = await asyncio.gather(
        monitor._start_detected_space("audit_host", state), monitor._start_detected_space("audit_host", state)
    )
    assert results.count("fullRace") == 1
    tasks = list(monitor._active_tasks.values())
    assert len(tasks) == len(monitor._active_sessions) == 1
    await asyncio.gather(*tasks)
    assert asyncio.get_running_loop().time() - began < 1.3
    assert counts == {"slot": 1, "browser": 1, "join": 1, "release": 1}
    assert not monitor._active_sessions and not monitor._active_tasks
    with get_session() as db:
        row = db.query(Space).filter_by(space_id="fullRace").one()
        assert row.join_outcome == "join_timeout" and row.join_age_seconds < 1.3
        assert row.recording_started_at is None
    assert not list(audit_db.data_dir.rglob("*.mkv"))


@pytest.mark.asyncio
async def test_a05_existing_queued_job_really_resumes(audit_db):
    from xsa.monitor.monitor import AccountMonitor

    monitor = AccountMonitor(None, audit_db)
    with get_session() as db:
        row = db.query(Space).filter_by(space_id="auditSpace").one()
        db.add(Job(space_id=row.id, job_type="post_process", state="queued"))
        db.commit()
    monitor._run_post_processing = AsyncMock()
    assert monitor.resume_queued_post_processing() == 1
    await asyncio.gather(*monitor._post_processing_tasks.values())
    assert monitor._run_post_processing.await_count == 1


@pytest.mark.asyncio
async def test_a06_cancel_ack_waits_for_thread_writer_to_exit(tmp_path):
    from xsa.blocking import run_blocking

    started = threading.Event()
    finished = threading.Event()
    target = tmp_path / "writer"

    def writer():
        started.set()
        time.sleep(0.05)
        target.write_text("finished")
        finished.set()

    task = asyncio.create_task(run_blocking(writer))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.005)
    assert not task.done()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and target.read_text() == "finished"


@pytest.mark.asyncio
async def test_a06_audio_child_is_reaped_before_cancellation(tmp_path, monkeypatch):
    from xsa.transcriber.audio import _extract

    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    child = SimpleNamespace(returncode=None)

    async def communicate():
        await asyncio.Event().wait()

    child.communicate = communicate
    child.terminate = lambda: setattr(child, "returncode", -15)
    child.wait = AsyncMock(return_value=-15)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    task = asyncio.create_task(_extract(source, tmp_path / "audio.wav", [], "test"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    child.wait.assert_awaited_once()
    assert child.returncode is not None and not (tmp_path / "audio.wav").exists()


@pytest.mark.asyncio
async def test_long_audio_uses_bounded_windows_and_preserves_global_times(tmp_path, monkeypatch):
    from xsa.transcriber import audio
    from xsa.transcriber.engine import SegmentItem, TranscriptionResult, WhisperTranscriber, WordItem

    source = tmp_path / "source.wav"
    source.write_bytes(b"immutable")
    monkeypatch.setattr(audio, "get_audio_duration_seconds", lambda p: 1901)
    windows = []

    async def extract(_source, output, start, duration):
        windows.append((start, duration))
        output.write_bytes(b"window")
        return True

    monkeypatch.setattr(audio, "extract_audio_window", extract)
    engine = WhisperTranscriber()
    engine.transcribe = lambda p, lang: TranscriptionResult(
        "en", 600, [SegmentItem(0, 1, 2, "word", [WordItem("word", 1, 2)])], "word"
    )
    result = await engine._transcribe_windows_async(source)
    assert windows == [(0, 600), (600, 600), (1200, 600), (1800, 101)]
    assert [s.id for s in result.segments] == [0, 1, 2, 3]
    assert [s.start for s in result.segments] == [1, 601, 1201, 1801]
    assert [s.words[0].start for s in result.segments] == [1, 601, 1201, 1801]
    assert result.duration == 1901 and source.read_bytes() == b"immutable"


@pytest.mark.asyncio
async def test_a07_transcription_obeys_live_and_maintenance_guards(audit_db, monkeypatch):
    from fastapi import HTTPException

    from xsa.maintenance_gate import arm_maintenance, release_maintenance

    api = importlib.import_module("xsa.api.app")
    monkeypatch.setattr(api, "_cfg", audit_db)
    queue = SimpleNamespace(schedule_post_processing=lambda *_a, **_k: True)
    monkeypatch.setattr(api, "_monitor", queue)
    arm_maintenance(audit_db.db_path.parent)
    with pytest.raises(HTTPException) as exc:
        await api.trigger_transcribe("auditSpace")
    assert exc.value.status_code == 423
    release_maintenance(audit_db.db_path.parent)
    with get_session() as db:
        db.query(Space).update({Space.state: "recording"})
        db.commit()
    with pytest.raises(HTTPException) as exc:
        await api.trigger_transcribe("auditSpace")
    assert exc.value.status_code == 409


def test_a09_excluded_segment_marks_partial(tmp_path, monkeypatch):
    from xsa.recorder import media

    paths = SpacePaths(root=tmp_path)
    broken, valid = tmp_path / "recording_000.mkv", tmp_path / "recording_001.mkv"
    broken.touch()
    valid.write_bytes(b"valid")
    monkeypatch.setattr(media, "has_audio_video", lambda p: p == valid)
    assert media.get_recording_source(paths) == valid
    manifest = json.loads((tmp_path / "recording_manifest.json").read_text())
    assert manifest["status"] == "PARTIAL"
    assert manifest["excluded_segments"] == [broken.name]


def test_a10_parallel_remux_produces_one_generation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from xsa.recorder import media

    source, target = tmp_path / "recording.mkv", tmp_path / "playback.mp4"
    source.write_bytes(b"source")
    commands = []

    def remux(command, **_kwargs):
        commands.append(command)
        time.sleep(0.02)
        Path(command[-1]).write_bytes(b"media")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(media.subprocess, "run", remux)
    monkeypatch.setattr(media, "has_audio_video", lambda p: p.exists() and p.stat().st_size > 0)
    monkeypatch.setattr(media, "_capture_active", lambda p: False)
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: media.create_playback_mp4(source, target), range(2)))
    assert all(results) and len(commands) == 1
    source.write_bytes(b"changed source")
    assert media.create_playback_mp4(source, target) and len(commands) == 2


def test_a11_expired_ownership_is_recoverable(audit_db):
    from xsa.job_ownership import recover_expired_jobs

    with get_session() as db:
        db.add(
            Job(
                job_type="diarize",
                state="running",
                owner_id="dead",
                lease_expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
            )
        )
        db.commit()
    assert recover_expired_jobs() == 1
    with get_session() as db:
        row = db.query(Job).one()
        assert row.state == "queued" and row.owner_id is None


def test_a11_worker_claim_recovers_expired_attempt_with_new_owner(audit_db, monkeypatch):
    from scripts import diarization_worker

    monkeypatch.setattr(diarization_worker, "DB_PATH", audit_db.db_path)
    with get_session() as db:
        row = db.query(Space).filter_by(space_id="auditSpace").one()
        db.add(
            Job(
                space_id=row.id,
                job_type="diarize",
                state="running",
                owner_id="dead",
                lease_expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
                priority=10,
            )
        )
        db.commit()
    claimed = diarization_worker._claim()
    assert claimed
    with get_session() as db:
        row = db.get(Job, claimed["id"])
        assert row.owner_id == diarization_worker.OWNER_ID
        assert row.state == "running" and row.attempt_id and row.heartbeat_at


@pytest.mark.asyncio
async def test_a12_stop_preserves_unverified_reason():
    from xsa.recorder.session import SpaceSession

    session = object.__new__(SpaceSession)
    session._stop_lock, session._stop_summary = asyncio.Lock(), None
    session._stop_reason = "space_state_unverified_after_silence"
    session._stop_impl = AsyncMock(return_value={})
    await session.stop()
    assert session._stop_reason == "space_state_unverified_after_silence"


def test_a13_ffmpeg_input_roles_distinguish_processing(monkeypatch):
    api = importlib.import_module("xsa.api.app")
    commands = [
        ["ffmpeg", "-f", "x11grab", "-i", ":100", "-f", "pulse", "-i", "sink.monitor", "recording_000.mkv"],
        ["ffmpeg", "-i", "recording_000.mkv", "audio.partial.wav"],
        ["ffmpeg", "-i", "recording_000.mkv", "playback.partial.mp4"],
    ]
    monkeypatch.setattr(
        api.psutil,
        "process_iter",
        lambda **_k: [SimpleNamespace(info={"name": "ffmpeg", "cmdline": a}) for a in commands],
    )
    assert api._ffmpeg_role_counts() == {"recording": 1, "processing": 2}


@pytest.mark.asyncio
async def test_readiness_separates_api_from_capture_and_requires_database(monkeypatch):
    api = importlib.import_module("xsa.api.app")
    monkeypatch.setattr(
        api,
        "health",
        AsyncMock(
            return_value={
                "status": "degraded",
                "capture_health": {"status": "degraded"},
                "database": {"healthy": True},
            }
        ),
    )
    assert (await api.readiness()).status_code == 200
    api.health.return_value["database"]["healthy"] = False
    assert (await api.readiness()).status_code == 503


@pytest.mark.asyncio
async def test_a15_creator_is_host_other_admin_is_not(monkeypatch):
    from xsa.browser import graphql

    monkeypatch.setattr(
        graphql,
        "fetch_audio_space_by_id",
        AsyncMock(
            return_value={
                "metadata": {"creator_results": {"result": {"legacy": {"screen_name": "creator"}}}},
                "participants": {
                    "admins": [{"twitter_screen_name": "creator"}, {"twitter_screen_name": "other"}],
                    "speakers": [],
                    "listeners": [],
                },
            }
        ),
    )
    rows = await graphql.fetch_space_participants_gql(None, "auditSpace")
    assert [(p.username, p.role) for p in rows] == [("creator", "host"), ("other", "admin")]


def test_a16_complete_generation_is_selected_by_db_atomically(audit_db):
    from xsa.transcriber.generation import stage_generation

    paths = SpacePaths.for_space(audit_db.data_dir, "auditSpace", datetime.now(UTC)).ensure()
    artifacts = {
        n: "{}" for n in ("raw.json", "normalized.json", "words.json", "segments.json", "transcript.json")
    }
    artifacts.update({"transcript.txt": "", "transcript.srt": "", "transcript.vtt": "WEBVTT"})
    generation, folder = stage_generation(paths.root, artifacts)
    assert paths.transcript_json == paths.root / "transcript.json"
    with get_session() as db:
        row = db.query(Space).filter_by(space_id="auditSpace").one()
        row.transcript_generation_id = generation
        db.commit()
    assert paths.transcript_json == folder / "transcript.json"
    assert json.loads(paths.transcript_json.read_text())["generation_id"] == generation
    assert json.loads((folder / "manifest.json").read_text())["files"] == sorted(artifacts)


@pytest.mark.asyncio
async def test_a16_failed_db_commit_keeps_previous_generation(audit_db, monkeypatch):
    from sqlalchemy.orm import Session

    from xsa.db import TranscriptSegment
    from xsa.transcriber import worker
    from xsa.transcriber.engine import TranscriptionResult

    paths = SpacePaths.for_space(audit_db.data_dir, "auditSpace", datetime.now(UTC)).ensure()
    paths.transcript_json.write_text('{"full_text":"previous", "segments":[]}')
    source = paths.segment(0)
    source.write_bytes(b"source")
    with get_session() as db:
        row = db.query(Space).filter_by(space_id="auditSpace").one()
        db.add(TranscriptSegment(space_id=row.id, start_ms=0, end_ms=1000, text="previous", segment_index=0))
        db.commit()
    monkeypatch.setattr(worker, "get_recording_source", lambda p: source)
    monkeypatch.setattr(worker, "extract_original_audio", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "extract_analysis_audio", AsyncMock(return_value=True))
    monkeypatch.setattr(
        worker.WhisperTranscriber,
        "transcribe_async",
        AsyncMock(return_value=TranscriptionResult("en", 4, [], "")),
    )

    def fail_commit(_self):
        raise RuntimeError("commit fault")

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit fault"):
        await worker.process_space_transcription("auditSpace", audit_db)
    with get_session() as db:
        assert db.query(TranscriptSegment).one().text == "previous"
        assert db.query(Space).filter_by(space_id="auditSpace").one().transcript_generation_id is None
    assert json.loads(paths.transcript_json.read_text())["full_text"] == "previous"


@pytest.mark.asyncio
async def test_a17_maintenance_boot_reconciles_recording_and_joining(audit_db, monkeypatch):
    from xsa.maintenance_gate import arm_maintenance

    api = importlib.import_module("xsa.api.app")
    with get_session() as db:
        db.query(Space).update({Space.state: "recording"})
        db.add(Space(space_id="orphanJoin", state="joining"))
        db.commit()
    arm_maintenance(audit_db.db_path.parent)
    monkeypatch.setattr(api, "_cfg", audit_db)
    monkeypatch.setattr(api, "setup_logging", lambda *_a, **_k: None)
    async with api.lifespan(api.app):
        with get_session() as db:
            assert not db.query(Space).filter(Space.state.in_(("recording", "joining"))).count()
            assert all(row.ended_at is None for row in db.query(Space))


@pytest.mark.asyncio
async def test_a08_missing_report_never_completes_processing(audit_db, monkeypatch):
    from xsa import analysis_platform
    from xsa.analyzer import summarizer
    from xsa.monitor.monitor import AccountMonitor
    from xsa.recorder import media
    from xsa.transcriber import worker

    paths = SpacePaths.for_space(audit_db.data_dir, "auditSpace", datetime.now(UTC)).ensure()
    source = paths.segment(0)
    source.write_bytes(b"source")
    paths.playback.write_bytes(b"playback")
    paths.thumbnail.write_bytes(b"thumbnail")
    paths.transcript_json.write_text('{"segments": [], "full_text": ""}')
    paths.summary_json.write_text("{}")
    monkeypatch.setattr(media, "get_recording_source", lambda _p: source)
    monkeypatch.setattr(media, "has_audio_video", lambda _p: True)
    monkeypatch.setattr(worker, "process_space_transcription", AsyncMock())
    monkeypatch.setattr(summarizer, "analyze_space", AsyncMock())
    monkeypatch.setattr(analysis_platform, "build_space_analysis", lambda *_a, **_k: {})
    monitor = AccountMonitor(None, audit_db)
    with get_session() as db:
        db_id = db.query(Space).filter_by(space_id="auditSpace").one().id
    assert monitor.schedule_post_processing("auditSpace", db_id, paths)
    await asyncio.gather(*monitor._post_processing_tasks.values())
    with get_session() as db:
        job = db.query(Job).filter_by(job_type="post_process").one()
        assert job.state == "failed"
        assert "full_analysis.json" in job.error


def test_a18_inventory_uses_valid_segment_and_recognizes_no_speech(audit_db, monkeypatch):
    from xsa import inventory

    paths = SpacePaths.for_space(audit_db.data_dir, "auditSpace", datetime.now(UTC)).ensure()
    paths.segment(0).touch()
    paths.segment(1).write_bytes(b"valid")
    paths.transcript_json.write_text('{"segments":[], "full_text":"", "recognition_state":"NO_SPEECH"}')

    def probe(path):
        valid = path.stat().st_size > 0
        return {
            "path": str(path),
            "valid": valid,
            "audio_valid": valid,
            "video_valid": valid,
            "duration_seconds": 189 if valid else 0,
            "size_bytes": path.stat().st_size,
        }

    monkeypatch.setattr(inventory, "probe_media", probe)
    value = inventory.audit_spaces(audit_db.data_dir)
    row = next(r for r in value["spaces"] if r["space_id"] == "auditSpace")
    assert row["media"]["duration_seconds"] == 189
    assert "NO_SPEECH" in row["flags"]
    assert "NEEDS_TRANSCRIPTION" not in row["flags"]
