from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xsa.config import Settings
from xsa.monitor import AccountMonitor


class FakeSession:
    def __init__(self, db_id: int) -> None:
        self._space_db_id = db_id
        self.recording_stopped_at = datetime.now(UTC)
        self.reason = None

    async def stop(self, *, reason: str):
        self.reason = reason
        return {"final_file": f"space-{self._space_db_id}.mkv"}


@pytest.mark.asyncio
async def test_operator_stop_marks_interrupted_after_graceful_session_stop(tmp_path):
    monitor = AccountMonitor(
        adapter=None,
        cfg=Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "xsa.sqlite3"),
    )
    session = FakeSession(12)
    monitor._active_sessions["space123"] = session
    state_updates = []
    events = []
    monitor._set_space_state = lambda db_id, state: state_updates.append((db_id, state))
    monitor._record_space_event = lambda *args: events.append(args)

    result = await monitor.stop_space("space123", "maintenance test")

    assert result == {"final_file": "space-12.mkv"}
    assert session.reason == "maintenance test"
    assert state_updates == [(12, "development_interrupted")]
    assert events[0][3] == "Recording stopped by operator"
    assert events[0][4]["classification"] == "development_interrupted"
    assert monitor.active_space_ids == []


@pytest.mark.asyncio
async def test_stop_all_uses_each_session_graceful_stop_path(tmp_path):
    monitor = AccountMonitor(
        adapter=None,
        cfg=Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "xsa.sqlite3"),
    )
    first = FakeSession(1)
    second = FakeSession(2)
    monitor._active_sessions.update({"one": first, "two": second})
    monitor._set_space_state = lambda *_args: None
    monitor._record_space_event = lambda *_args: None

    result = await monitor.stop_all_recordings("operator test")

    assert set(result) == {"one", "two"}
    assert result["one"]["final_file"] == "space-1.mkv"
    assert result["two"]["final_file"] == "space-2.mkv"
    assert first.reason == second.reason == "operator test"
    assert monitor.active_space_ids == []


@pytest.mark.asyncio
async def test_admin_maintenance_route_arms_gate_and_stops_when_requested(tmp_path, monkeypatch):
    import importlib

    from xsa.maintenance_gate import maintenance_active

    api = importlib.import_module("xsa.api.app")
    state_dir = tmp_path / "state"
    monkeypatch.setattr(
        api,
        "_cfg",
        Settings(data_dir=tmp_path / "data", db_path=state_dir / "xsa.sqlite3"),
    )
    monitor = SimpleNamespace(
        active_space_ids=[],
        stop_all_recordings=AsyncMock(return_value={"space123": {"final_file": "recording.mkv"}}),
    )
    monkeypatch.setattr(api, "_monitor", monitor)

    result = await api.admin_enable_maintenance(
        api.AdminMaintenanceIn(stop_recordings=True, reason="test deployment")
    )

    assert maintenance_active(state_dir)
    assert result["maintenance_mode"] is True
    monitor.stop_all_recordings.assert_awaited_once_with("test deployment")
    assert await api.admin_release_maintenance() == {"maintenance_mode": False, "monitor_resuming": True}
    assert not maintenance_active(state_dir)


@pytest.mark.asyncio
async def test_admin_stop_route_calls_the_graceful_monitor_path(monkeypatch):
    import importlib

    api = importlib.import_module("xsa.api.app")
    monitor = SimpleNamespace(stop_space=AsyncMock(return_value={"final_file": "recording.mkv"}))
    monkeypatch.setattr(api, "_monitor", monitor)

    result = await api.admin_stop_recording("space123", api.AdminStopIn(reason="operator test"))

    assert result["state"] == "development_interrupted"
    monitor.stop_space.assert_awaited_once_with("space123", "operator test")
