"""Manual capture only consumes a verified live Space and preserves accounts."""

from __future__ import annotations

import importlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from xsa.config import Settings
from xsa.db import MonitoredAccount, Space, database, get_session, init_db
from xsa.manual_recording import space_id_from_url
from xsa.monitor.monitor import AccountMonitor
from xsa.paths import SpacePaths
from xsa.recorder.session import SpaceSession


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/i/spaces/abc",
        "http://x.com/i/spaces/abc",
        "https://x.com.evil.example/i/spaces/abc",
        "https://x.com/i/spaces/abc/other",
        "https://x.com/i/spaces/abc#fragment",
        "https://x.com@evil.example/i/spaces/abc",
    ],
)
def test_invalid_manual_links_are_rejected(url):
    with pytest.raises(ValueError):
        space_id_from_url(url)


@pytest.mark.parametrize("domain", ["x.com", "twitter.com", "www.x.com", "www.twitter.com"])
def test_valid_manual_links_extract_exact_id(domain):
    assert space_id_from_url(f"https://{domain}/i/spaces/Ab123?s=20") == "Ab123"


@pytest.mark.asyncio
async def test_browser_writes_reject_foreign_origin_before_reaching_capture():
    api = importlib.import_module("xsa.api.app")
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://localhost:8765") as client:
        result = await client.post(
            "/spaces/record",
            json={"url": "https://x.com/i/spaces/abc"},
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_queued_request_survives_restart_and_can_cancel_without_account_mutation(tmp_path, monkeypatch):
    api = importlib.import_module("xsa.api.app")
    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "db.sqlite3")
    cfg.data_dir.mkdir()
    init_db(cfg.db_path)
    monitor = AccountMonitor(SimpleNamespace(), cfg)
    monkeypatch.setattr(api, "_cfg", cfg)
    monkeypatch.setattr(api, "_adapter", SimpleNamespace())
    monkeypatch.setattr(api, "_monitor", monitor)
    try:
        request = api.RecordSpaceIn(url="https://twitter.com/i/spaces/abc123")
        first = await api.queue_record_space(request)
        second = await api.queue_record_space(request)
        assert first["status"] == second["status"] == "queued"
        assert monitor.live_discovery.entries["abc123"]["capture_request_state"] == "queued"
        from xsa.monitor.live_discovery import LiveDiscovery

        assert LiveDiscovery(cfg.db_path.parent).entries["abc123"]["capture_request_state"] == "queued"
        monitor.live_discovery.entries["abc123"]["capture_request_state"] = "starting"
        monitor.live_discovery.save()
        restarted = LiveDiscovery(cfg.db_path.parent)
        assert restarted.entries["abc123"]["capture_request_state"] == "queued"
        assert restarted.entries["abc123"]["next_check_epoch"] == 0
        monitor.live_discovery = restarted
        with get_session() as db:
            assert db.query(MonitoredAccount).count() == 0
        assert (await api.cancel_record_request("abc123"))["status"] == "cancelled"
        assert LiveDiscovery(cfg.db_path.parent).entries["abc123"]["capture_request_state"] == "cancelled"
    finally:
        database._engine.dispose()
        database._engine = None
        database._SessionLocal = None


@pytest.mark.asyncio
async def test_only_exact_fresh_verified_live_creator_can_start(tmp_path, monkeypatch):
    from xsa.browser import adapter as browser_adapter

    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "db.sqlite3")
    monitor = AccountMonitor(SimpleNamespace(), cfg)
    monitor._start_detected_space = AsyncMock(return_value="live123")
    monkeypatch.setattr(browser_adapter, "viewer_retry_remaining", lambda: 0)
    row = monitor.live_discovery.request_capture("live123")
    row.update(state="live", role="host", host_username="UnmonitoredHost", checked_at_epoch=time.time())
    assert await monitor._start_manual_capture("live123", {**row, "space_id": "wrong"}) is None
    assert await monitor._start_manual_capture("live123", {**row, "role": "guest"}) is None
    assert (
        await monitor._start_manual_capture("live123", {**row, "checked_at_epoch": time.time() - 181}) is None
    )
    assert await monitor._start_manual_capture("live123", row) == "live123"
    assert monitor._start_detected_space.await_count == 1
    assert monitor._start_detected_space.call_args.args[0] == "UnmonitoredHost"
    assert monitor.live_discovery.entries["live123"]["capture_request_state"] == "started"
    assert await monitor._start_manual_capture("live123", row) is None


@pytest.mark.asyncio
async def test_scheduled_space_refuses_capture(tmp_path, monkeypatch):
    from xsa.browser import adapter as browser_adapter

    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "db.sqlite3")
    monitor = AccountMonitor(SimpleNamespace(), cfg)
    monitor._start_detected_space = AsyncMock(return_value="future")
    monkeypatch.setattr(browser_adapter, "viewer_retry_remaining", lambda: 0)
    monitor.live_discovery.request_capture("future")
    row = monitor.live_discovery.apply_metadata(
        "future",
        {
            "rest_id": "future",
            "metadata": {
                "state": "NotStarted",
                "creator_results": {"result": {"legacy": {"screen_name": "host"}}},
            },
        },
    )
    assert row["capture_request_state"] == "not_live"
    assert await monitor._start_manual_capture("future", row) is None
    monitor._start_detected_space.assert_not_awaited()


def test_manual_host_adoption_never_adds_or_reactivates_watched_account(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "db.sqlite3")
    init_db(cfg.db_path)
    with get_session() as db:
        db.add(Space(space_id="newhost", state="recording"))
        db.add(MonitoredAccount(username="savedhost", active=False, priority="P2"))
        db.commit()
    try:
        session = SpaceSession(
            "newhost",
            "https://x.com/i/spaces/newhost",
            "unmonitored",
            SpacePaths(cfg.data_dir, "newhost"),
            SimpleNamespace(),
            cfg,
            manual_capture=True,
        )
        session._adopt_discovered_host("unmonitored")
        with get_session() as db:
            assert db.query(MonitoredAccount).count() == 1
            space = db.query(Space).filter_by(space_id="newhost").one()
            assert space.host_account_id is None
            assert space.verified_host_username == "unmonitored"
        session._adopt_discovered_host("savedhost")
        with get_session() as db:
            saved = db.query(MonitoredAccount).one()
            assert saved.active is False and saved.priority == "P2"
            space = db.query(Space).filter_by(space_id="newhost").one()
            assert space.host_account_id == saved.id
            assert space.verified_host_username == "savedhost"
    finally:
        database._engine.dispose()
        database._engine = None
        database._SessionLocal = None
