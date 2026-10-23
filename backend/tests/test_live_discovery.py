from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xsa.browser import SpaceState
from xsa.config import Settings
from xsa.db import MonitoredAccount, get_session, init_db
from xsa.monitor.live_discovery import LiveDiscovery
from xsa.monitor.monitor import AccountMonitor


def metadata(sid="live123", host="actual_host", state="Running"):
    return {
        "rest_id": sid,
        "metadata": {
            "state": state,
            "title": "Live discussion",
            "creator_results": {"result": {"legacy": {"screen_name": host, "name": "Actual Host"}}},
        },
    }


def test_discovery_is_persistent_and_does_not_confuse_guest_with_host(tmp_path):
    cache = LiveDiscovery(tmp_path)
    cache.remember("live123", "guest")
    assert cache.feed()[0]["host_username"] is None
    assert cache.feed()[0]["verification_state"] == "pending"
    cache.apply_metadata("live123", metadata())
    restored = LiveDiscovery(tmp_path).feed()[0]
    assert restored["host_username"] == "actual_host"
    assert restored["discovered_via"] == ["guest"]
    assert restored["verification_state"] == "confirmed_live"


def test_ended_and_wrong_id_cannot_be_presented_as_confirmed_live(tmp_path):
    cache = LiveDiscovery(tmp_path)
    cache.apply_metadata("live123", metadata(sid="wrong"))
    assert cache.feed()[0]["verification_state"] == "pending"
    cache.apply_metadata("live123", metadata(state="Ended"))
    assert cache.feed() == []


def test_cached_live_result_becomes_stale_instead_of_claiming_live_forever(tmp_path, monkeypatch):
    cache = LiveDiscovery(tmp_path)
    cache.apply_metadata("live123", metadata())
    checked = cache.entries["live123"]["checked_at_epoch"]
    monkeypatch.setattr("xsa.monitor.live_discovery.time.time", lambda: checked + 181)
    assert cache.feed()[0]["verification_state"] == "stale"


@pytest.mark.asyncio
async def test_api_keeps_discoveries_visible_during_cooldown(tmp_path, monkeypatch):
    api = importlib.import_module("xsa.api.app")
    cache = LiveDiscovery(tmp_path)
    cache.remember("live123", "operator_link")
    monkeypatch.setattr(
        api, "_monitor", SimpleNamespace(active_space_ids=[], _live_spaces_feed={}, live_discovery=cache)
    )
    monkeypatch.setattr("xsa.browser.adapter.viewer_retry_remaining", lambda: 600)
    value = await api.get_live_feed()
    assert value["count"] == 1 and value["confirmed_count"] == 0
    assert value["discovery"]["status"] == "rate_limited"
    assert value["feed"][0]["verification_state"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("monitored_host", [True, False])
async def test_profile_candidate_is_verified_before_allocating_capture(tmp_path, monkeypatch, monitored_host):
    from xsa.db import database

    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "db.sqlite3")
    init_db(cfg.db_path)
    with get_session() as db:
        db.add(MonitoredAccount(username="guest"))
        if monitored_host:
            db.add(MonitoredAccount(username="actual_host"))
        db.commit()
    monitor = AccountMonitor(None, cfg)
    monitor._detect_space_on_page = AsyncMock(
        return_value=SpaceState(space_id="live123", host_username="guest")
    )
    monitor._start_detected_space = AsyncMock(return_value="live123")
    fetch = AsyncMock(return_value=metadata())
    monkeypatch.setattr("xsa.browser.graphql.fetch_audio_space_by_id", fetch)
    monkeypatch.setattr("xsa.browser.adapter.viewer_retry_remaining", lambda: 0)
    try:
        await monitor._poll_account("guest", page=object())
        assert monitor.live_discovery.feed()[0]["host_username"] == "actual_host"
        if monitored_host:
            assert monitor._start_detected_space.call_args.args[0] == "actual_host"
        else:
            monitor._start_detected_space.assert_not_called()
        await monitor._poll_account("guest", page=object())
        assert fetch.await_count == 1  # one metadata request for a repeated candidate
    finally:
        database._engine.dispose()
        database._engine = database._SessionLocal = None


def test_duplicate_js_error_does_not_escalate_or_extend_cooldown(tmp_path, monkeypatch):
    from xsa.browser import adapter

    clock = [100.0]
    monkeypatch.setattr(
        adapter, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0])
    )
    monkeypatch.setattr(adapter, "_viewer_backoff_path", lambda: tmp_path / "backoff.json")
    monkeypatch.setattr(adapter, "_viewer_backoff_loaded", True)
    monkeypatch.setattr(adapter, "_viewer_retry_until", 0)
    monkeypatch.setattr(adapter, "_viewer_429_streak", 0)
    response = SimpleNamespace(
        url="https://x.com/i/api/graphql/id/Viewer", status=429, headers={"retry-after": "120"}
    )
    adapter.observe_viewer_response(response)
    original = adapter._viewer_retry_until
    adapter.observe_viewer_page_error("ApiError: https://x.com/i/api/graphql/id/Viewer HTTP-429")
    assert adapter._viewer_retry_until == original
    assert adapter._viewer_429_streak == 1
    clock[0] += 10
    adapter.observe_viewer_page_error("ApiError: https://x.com/i/api/graphql/id/Viewer HTTP-429")
    assert adapter._viewer_retry_until == original


@pytest.mark.asyncio
async def test_space_metadata_queries_respect_shared_cooldown(monkeypatch):
    from xsa.browser.graphql import fetch_audio_space_by_id

    monkeypatch.setattr("xsa.browser.adapter.viewer_retry_remaining", lambda: 120)
    page = SimpleNamespace(evaluate=AsyncMock())
    assert await fetch_audio_space_by_id(page, "live123") is None
    page.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_parks_only_discovery_page(tmp_path, monkeypatch):
    monitor = AccountMonitor(None, Settings(db_path=tmp_path / "state" / "db.sqlite3"))
    page = SimpleNamespace(url="https://x.com/guest", is_closed=lambda: False, goto=AsyncMock())
    capture = SimpleNamespace(stop=AsyncMock(), adapter=SimpleNamespace(close=AsyncMock()))
    monitor._poll_page = page
    monitor._active_sessions["recording123"] = capture
    monkeypatch.setattr("xsa.browser.adapter.viewer_retry_remaining", lambda: 120)
    await monitor._poll_all_accounts()
    assert page.goto.call_args.args[0] == "about:blank"
    capture.stop.assert_not_called()
    capture.adapter.close.assert_not_called()


def test_legacy_cooldown_deadline_is_preserved_without_duplicate_event_counter(tmp_path, monkeypatch):
    from xsa.browser import adapter

    path = tmp_path / "backoff.json"
    path.write_text(json.dumps({"retry_until_epoch": 2000, "rate_limit_streak": 4}))
    monkeypatch.setattr(adapter, "time", SimpleNamespace(monotonic=lambda: 100, time=lambda: 1000))
    monkeypatch.setattr(adapter, "_viewer_backoff_path", lambda: path)
    monkeypatch.setattr(adapter, "_viewer_retry_until", 0)
    monkeypatch.setattr(adapter, "_viewer_429_streak", 0)
    monkeypatch.setattr(adapter, "_viewer_backoff_loaded", False)
    adapter._load_viewer_backoff_state(force=True)
    assert adapter.viewer_retry_remaining() == 1000
    assert adapter._viewer_429_streak == 0


@pytest.mark.asyncio
async def test_rate_limit_does_not_hide_maintenance_boot_state(tmp_path, monkeypatch):
    from xsa.db import database
    from xsa.maintenance_gate import arm_maintenance

    api = importlib.import_module("xsa.api.app")
    cfg = Settings(db_path=tmp_path / "state" / "db.sqlite3", data_dir=tmp_path / "data")
    init_db(cfg.db_path)
    arm_maintenance(cfg.db_path.parent)
    monkeypatch.setattr(api, "_cfg", cfg)
    monkeypatch.setattr(api, "_monitor", None)
    monkeypatch.setattr(api, "_monitor_task", None)
    monkeypatch.setattr(api, "_adapter", None)
    monkeypatch.setattr("xsa.browser.adapter.viewer_retry_remaining", lambda: 300)
    try:
        value = await api.health()
        assert value["status"] == "maintenance"
        assert value["api_health"]["status"] == "healthy"
        assert value["x_access"]["status"] == "rate_limited"
    finally:
        database._engine.dispose()
        database._engine = database._SessionLocal = None
