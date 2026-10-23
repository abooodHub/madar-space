from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from xsa.config import Settings
from xsa.db import Space, SystemEvent, get_session, init_db
from xsa.monitor.monitor import AccountMonitor
from xsa.paths import SpacePaths
from xsa.recorder.session import SpaceSession


@pytest.fixture
def join_db(tmp_path):
    from xsa.db import database as db_module

    db_path = tmp_path / "state" / "join-test.sqlite3"
    init_db(db_path)
    with get_session() as db:
        space = Space(space_id="joinTest123", state="detected", url="https://x.com/i/spaces/joinTest123")
        db.add(space)
        db.commit()
        db.refresh(space)
        space_id = space.id
    yield db_path, space_id
    if db_module._engine:
        db_module._engine.dispose()
        db_module._engine = None
        db_module._SessionLocal = None


def make_monitor(timeout: float = 0.03) -> AccountMonitor:
    monitor = object.__new__(AccountMonitor)
    monitor._running = True
    monitor._cfg = SimpleNamespace(
        db_path=Path("/state/xsa.sqlite3"),
        session_start_timeout_seconds=timeout,
        max_concurrent_recordings=2,
    )
    monitor._adapter = object()
    monitor._active_sessions = {}
    monitor._active_tasks = {}
    monitor._post_processing_tasks = {}
    monitor.schedule_post_processing = lambda *_args: None
    return monitor


class FakeRecorder:
    def __init__(self):
        self._proc = None

    def health(self):
        return bool(self._proc and self._proc.poll() is None)


class FakeAdapter:
    def __init__(self):
        self.join_checkpoint = "waiting_for_player"
        self.join_outcome = None
        self.join_host_verification_state = "verified"
        self.join_anonymous_listen_state = "joined_anonymously"
        self.join_space_state = "live"
        self._page = None
        self.closed = False

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, wait_forever=False, outcome=None):
        self._space_db_id = None
        self.adapter = FakeAdapter()
        self.slot = None
        self._recorder = FakeRecorder()
        self.join_outcome = None
        self.join_last_error = None
        self.join_started_monotonic = None
        self.join_progress_callback = None
        self.wait_forever = wait_forever
        self.outcome = outcome
        self.aborted = False

    async def start(self):
        if self.outcome:
            self.adapter.join_outcome = self.outcome
            raise RuntimeError(self.outcome)
        if self.wait_forever:
            await asyncio.Event().wait()

    async def abort_join(self):
        self.aborted = True

    async def stop(self):
        return {}

    async def run_until_ended(self):
        return None


@pytest.mark.asyncio
async def test_join_progress_accepts_sync_persistence_callback():
    session = object.__new__(SpaceSession)
    session.adapter = FakeAdapter()
    seen = []
    session.join_progress_callback = lambda checkpoint, details, at: seen.append((checkpoint, details, at))

    await session.report_join_progress("opening_space", {"source": "test"})

    assert seen and seen[0][0:2] == ("opening_space", {"source": "test"})
    assert session.adapter.join_checkpoint == "opening_space"


@pytest.mark.asyncio
async def test_join_deadline_covers_entire_start_and_persists_terminal_timeout(join_db, monkeypatch):
    import xsa.monitor.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "maintenance_active", lambda _path: False)
    _, db_id = join_db
    monitor = make_monitor()
    session = FakeSession(wait_forever=True)
    session._space_db_id = db_id
    monitor._active_sessions["joinTest123"] = session
    monitor._active_tasks["joinTest123"] = asyncio.current_task()

    await monitor._run_session(session, "joinTest123", db_id)

    with get_session() as db:
        row = db.get(Space, db_id)
        event = db.query(SystemEvent).filter_by(space_id=db_id).one()
        assert row.state == "error"
        assert row.join_outcome == "join_timeout"
        assert row.join_checkpoint == "waiting_for_player"
        assert row.join_deadline_seconds == pytest.approx(0.03)
        assert row.join_age_seconds < 0.2
        assert row.recording_started_at is None
        assert row.recording_ended_at is None
        assert event.details["reason"] == "join_timeout"
    assert session.aborted
    assert session.adapter.closed
    assert monitor.active_space_ids == []
    assert monitor._active_tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        ("host_mismatch", "rejected"),
        ("anonymous_listening_unavailable", "rejected"),
        ("space_ended_before_join", "rejected"),
        ("space_not_live", "rejected"),
        ("identity_exposure_guard", "rejected"),
        ("graphql_failure", "error"),
        ("browser_failure", "error"),
        ("player_not_ready", "error"),
    ],
)
async def test_join_outcomes_are_terminal_and_do_not_create_recording_files(
    join_db, monkeypatch, outcome, expected_state
):
    import xsa.monitor.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "maintenance_active", lambda _path: False)
    _, db_id = join_db
    monitor = make_monitor()
    session = FakeSession(outcome=outcome)
    session._space_db_id = db_id
    monitor._active_sessions["joinTest123"] = session
    monitor._active_tasks["joinTest123"] = asyncio.current_task()

    await monitor._run_session(session, "joinTest123", db_id)

    with get_session() as db:
        row = db.get(Space, db_id)
        assert row.state == expected_state
        assert row.join_outcome == outcome
        assert row.recording_started_at is None
        assert row.recording_ended_at is None
        assert db.query(SystemEvent).filter_by(space_id=db_id).count() == 1
    assert session.aborted
    assert session.adapter.closed


@pytest.mark.asyncio
async def test_failed_open_does_not_create_a_space_folder(tmp_path):
    class RejectingAdapter:
        join_outcome = "anonymous_listening_unavailable"
        join_checkpoint = None

        async def openSpace(self, *_args, **_kwargs):
            return False

    paths = SpacePaths.planned_path(
        tmp_path,
        "joinTest123",
        datetime(2026, 10, 9, 10, 11, 12, tzinfo=UTC),
        title="عنوان غير مسجل",
        host="@host",
    )
    session = SpaceSession(
        "joinTest123",
        "https://x.com/i/spaces/joinTest123",
        "host",
        paths,
        RejectingAdapter(),
        Settings(data_dir=tmp_path),
    )

    with pytest.raises(RuntimeError, match="Failed to join Space"):
        await session.start()

    assert not paths.root.exists()


@pytest.mark.asyncio
async def test_admin_join_cancel_waits_for_task_cleanup_and_checks_ffmpeg(join_db, monkeypatch):
    import xsa.monitor.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "maintenance_active", lambda _path: False)
    _, db_id = join_db
    monitor = make_monitor(timeout=120)
    session = FakeSession(wait_forever=True)
    session._space_db_id = db_id
    monitor._active_sessions["joinTest123"] = session
    task = asyncio.create_task(monitor._run_session(session, "joinTest123", db_id))
    monitor._active_tasks["joinTest123"] = task
    for _ in range(20):
        await asyncio.sleep(0)
        with get_session() as db:
            row = db.get(Space, db_id)
            if row.state == "joining":
                break

    result = await monitor.cancel_join("joinTest123", "unit test cancellation")
    assert result == {
        "space_id": "joinTest123",
        "cancelled": True,
        "ffmpeg_alive": False,
        "reason": "unit test cancellation",
    }
    with get_session() as db:
        row = db.get(Space, db_id)
        assert row.state == "error"
        assert row.join_outcome == "cancelled"
    assert task.done()
    assert session.aborted
    assert session.adapter.closed


def test_startup_recovery_marks_orphan_join_terminal(join_db):
    from xsa.api.app import recover_interrupted_join_rows

    _, db_id = join_db
    with get_session() as db:
        row = db.get(Space, db_id)
        row.state = "joining"
        row.joining_started_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=130)
        row.join_deadline_seconds = 120
        row.join_checkpoint = "verifying_host"
        row.last_progress_at = row.joining_started_at
        db.commit()

    assert recover_interrupted_join_rows() == 1
    with get_session() as db:
        row = db.get(Space, db_id)
        assert row.state == "error"
        assert row.join_outcome == "startup_recovery_interrupted_join"
        assert row.join_checkpoint == "verifying_host"
        assert row.join_age_seconds >= 130


@pytest.mark.asyncio
async def test_maintenance_reconciles_orphan_join_but_fresh_task_blocks(join_db, monkeypatch):
    import importlib

    from xsa.maintenance_gate import arm_maintenance

    api = importlib.import_module("xsa.api.app")
    state_dir = join_db[0].parent
    cfg = Settings(db_path=join_db[0], data_dir=state_dir / "data", session_start_timeout_seconds=120)
    monkeypatch.setattr(api, "_cfg", cfg)
    runtime_monitor = SimpleNamespace(
        _active_tasks={}, active_space_ids=[], _post_processing_tasks={}, _active_sessions={}
    )
    monkeypatch.setattr(api, "_monitor", runtime_monitor)
    arm_maintenance(state_dir)
    with get_session() as db:
        row = db.get(Space, join_db[1])
        row.state = "joining"
        row.joining_started_at = datetime.now(UTC).replace(tzinfo=None)
        row.join_deadline_seconds = 120
        db.commit()

    status = await api.maintenance_status()
    assert status["safe_to_deploy"] is True
    with get_session() as db:
        row = db.get(Space, join_db[1])
        assert row.state == "error"
        assert row.join_outcome == "startup_recovery_interrupted_join"

    async def wait_forever():
        await asyncio.Event().wait()

    live_task = asyncio.create_task(wait_forever())
    runtime_monitor._active_tasks["joinTest123"] = live_task
    with get_session() as db:
        row = db.get(Space, join_db[1])
        row.state = "joining"
        row.join_outcome = None
        row.joining_started_at = datetime.now(UTC).replace(tzinfo=None)
        row.join_deadline_seconds = 120
        db.commit()
    status = await api.maintenance_status()
    assert status["safe_to_deploy"] is False
    assert any(
        item["space_id"] == "joinTest123" and item["state"] == "joining" for item in status["unsafe_spaces"]
    )
    live_task.cancel()
    await asyncio.gather(live_task, return_exceptions=True)
