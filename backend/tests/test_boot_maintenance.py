from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi import HTTPException

from xsa.config import Settings
from xsa.maintenance_gate import arm_maintenance


@pytest.mark.asyncio
async def test_boot_lock_keeps_api_available_without_starting_browser_or_monitor(tmp_path, monkeypatch):
    api_module = importlib.import_module("xsa.api.app")

    state_dir = tmp_path / "state"
    cfg = Settings(data_dir=tmp_path / "data", db_path=state_dir / "xsa.sqlite3")
    arm_maintenance(state_dir)
    monkeypatch.setattr(api_module, "_cfg", cfg)
    monkeypatch.setattr(api_module, "_adapter", None)
    monkeypatch.setattr(api_module, "_monitor", None)
    monkeypatch.setattr(api_module, "_monitor_task", None)
    monkeypatch.setattr(api_module, "_maintenance_watch_task", None)
    monkeypatch.setattr(api_module, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        api_module, "XBrowserAdapter", lambda *_args, **_kwargs: pytest.fail("browser started")
    )
    monkeypatch.setattr(
        api_module, "AccountMonitor", lambda *_args, **_kwargs: pytest.fail("monitor started")
    )

    async with api_module.lifespan(api_module.app):
        health = await api_module.health()
        assert health["status"] == "maintenance"
        assert health["maintenance_mode"] is True
        assert health["monitor_running"] is False
        assert health["browser_connected"] is False
        assert health["active_recordings"] == []
        assert api_module._monitor is None
        assert api_module._monitor_task is None
        with pytest.raises(HTTPException) as exc_info:
            await api_module.trigger_record_space(api_module.RecordSpaceIn(url="https://x.com/i/spaces/test"))
        assert exc_info.value.status_code == 423


@pytest.mark.asyncio
async def test_without_boot_lock_monitor_starts_and_is_stopped_on_shutdown(tmp_path, monkeypatch):
    api_module = importlib.import_module("xsa.api.app")

    started = asyncio.Event()
    stopped = asyncio.Event()

    class FakeAdapter:
        def __init__(self, **_kwargs):
            self._context = object()

        async def start(self):
            return None

        async def close(self):
            return None

    class FakeMonitor:
        def __init__(self, _adapter, _cfg):
            self.active_space_ids = []
            self._active_sessions = {}
            self._post_processing_tasks = {}

        async def start(self):
            started.set()
            await asyncio.Event().wait()

        async def stop(self):
            stopped.set()

    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "xsa.sqlite3")
    monkeypatch.setattr(api_module, "_cfg", cfg)
    monkeypatch.setattr(api_module, "_adapter", None)
    monkeypatch.setattr(api_module, "_monitor", None)
    monkeypatch.setattr(api_module, "_monitor_task", None)
    monkeypatch.setattr(api_module, "_maintenance_watch_task", None)
    monkeypatch.setattr(api_module, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api_module, "XBrowserAdapter", FakeAdapter)
    monkeypatch.setattr(api_module, "AccountMonitor", FakeMonitor)

    async with api_module.lifespan(api_module.app):
        await asyncio.wait_for(started.wait(), timeout=1)
        assert api_module._monitor is not None
        assert api_module._monitor_task is not None

    assert stopped.is_set()
    assert api_module._monitor_task is not None
    assert api_module._monitor_task.done()
    assert api_module._monitor_task.cancelled()


@pytest.mark.asyncio
async def test_removing_boot_lock_resumes_monitor(tmp_path, monkeypatch):
    from xsa.api import app as fastapi_app

    api_module = importlib.import_module("xsa.api.app")
    started = asyncio.Event()
    stopped = asyncio.Event()
    state_dir = tmp_path / "state"
    cfg = Settings(data_dir=tmp_path / "data", db_path=state_dir / "xsa.sqlite3")
    arm_maintenance(state_dir)

    class FakeAdapter:
        def __init__(self, **_kwargs):
            self._context = object()

        async def start(self):
            return None

        async def close(self):
            return None

    class FakeMonitor:
        def __init__(self, _adapter, _cfg):
            self.active_space_ids = []
            self._active_sessions = {}
            self._post_processing_tasks = {}

        async def start(self):
            started.set()
            await asyncio.Event().wait()

        async def stop(self):
            stopped.set()

    monkeypatch.setattr(api_module, "_cfg", cfg)
    monkeypatch.setattr(api_module, "_adapter", None)
    monkeypatch.setattr(api_module, "_monitor", None)
    monkeypatch.setattr(api_module, "_monitor_task", None)
    monkeypatch.setattr(api_module, "_maintenance_watch_task", None)
    monkeypatch.setattr(api_module, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api_module, "XBrowserAdapter", FakeAdapter)
    monkeypatch.setattr(api_module, "AccountMonitor", FakeMonitor)

    async with api_module.lifespan(fastapi_app):
        await asyncio.sleep(0.6)
        assert not started.is_set()
        assert api_module._monitor is None
        from xsa.maintenance_gate import release_maintenance

        assert release_maintenance(state_dir)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert api_module._monitor is not None
        assert api_module._monitor_task is not None

    assert stopped.is_set()
