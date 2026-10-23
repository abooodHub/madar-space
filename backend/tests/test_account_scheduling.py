from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xsa.config import Settings
from xsa.db import MonitoredAccount, get_session, init_db
from xsa.monitor.monitor import AccountMonitor
from xsa.monitor.scheduling import AccountPollError, GraphqlWindowBudget, PollMeasurements, next_poll_at


def test_scheduler_schema_upgrade_preserves_existing_accounts(tmp_path):
    from xsa.db import database

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE monitored_accounts (
            id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,display_name TEXT,x_user_id TEXT,
            avatar_url TEXT,avatar_cached_at DATETIME,active BOOLEAN NOT NULL,added_at DATETIME NOT NULL,
            last_checked_at DATETIME,tags TEXT,notes TEXT);
            INSERT INTO monitored_accounts(id,username,active,added_at,notes)
            VALUES(17,'preserved',1,'2026-10-01','keep this note');""")
    for _attempt in range(2):
        init_db(path)
        with get_session() as db:
            account = db.get(MonitoredAccount, 17)
            assert account.username == "preserved" and account.notes == "keep this note"
            assert account.next_check_at is None and account.poll_failure_streak == 0
        database._engine.dispose()
    database._engine = database._SessionLocal = None


def test_schedule_never_shortens_retry_after_and_bounds_error_backoff(monkeypatch):
    monkeypatch.setattr("xsa.monitor.scheduling.random.uniform", lambda _a, b: b)
    now = datetime(2026, 10, 22)
    assert next_poll_at(
        now=now, interval=60, outcome="rate_limited", failures=0, retry_after=900, jitter_seconds=2
    ) == now + timedelta(seconds=902)
    assert next_poll_at(
        now=now, interval=60, outcome="error", failures=100, jitter_seconds=2
    ) == now + timedelta(seconds=3602)


def test_measurements_count_transport_without_retaining_sensitive_urls():
    metrics = PollMeasurements()
    for url in (
        "https://x.com/i/api/graphql/hash/Viewer?secret=value",
        "https://example.com/i/api/graphql/hash/Viewer",
        "https://x.com/photo",
    ):
        metrics.on_request(SimpleNamespace(url=url))
    metrics.on_response(SimpleNamespace(url="https://x.com/i/api/graphql/hash/Viewer", status=401))
    assert metrics.requests == 1
    assert metrics.outcome(False, 0) == "authentication_required"
    assert metrics.outcome(False, 90) == "rate_limited"
    assert "secret" not in repr(metrics)


def test_graphql_window_budget_counts_only_x_and_expires(monkeypatch):
    now = 1000.0
    monkeypatch.setattr("xsa.monitor.scheduling.time.monotonic", lambda: now)
    budget = GraphqlWindowBudget(window_seconds=300)
    budget.on_request(SimpleNamespace(url="https://x.com/i/api/graphql/hash/Viewer?secret=x"))
    budget.on_request(SimpleNamespace(url="https://example.com/i/api/graphql/hash/Viewer"))
    assert budget.used() == 1
    assert not budget.available(1)
    assert "secret" not in repr(budget)
    now = 1301.0
    assert budget.available(1)
    assert budget.used() == 0
    budget.on_response(SimpleNamespace(url="https://x.com/i/api/graphql/hash/Viewer", status=401))
    assert budget.authentication_required()
    budget.on_response(SimpleNamespace(url="https://x.com/i/api/graphql/hash/Viewer", status=200))
    assert not budget.authentication_required()


@pytest.fixture
def scheduler_db(tmp_path, monkeypatch):
    from xsa.db import database

    init_db(tmp_path / "state" / "test.sqlite3")
    monkeypatch.setattr("xsa.browser.adapter.viewer_retry_remaining", lambda: 0)
    with get_session() as db:
        db.add_all(
            [
                MonitoredAccount(
                    username="future",
                    next_check_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
                ),
                MonitoredAccount(username="old", last_checked_at=datetime(2026, 1, 1)),
                MonitoredAccount(username="new"),
                MonitoredAccount(username="paused", active=False),
            ]
        )
        db.commit()
    monitor = AccountMonitor(
        SimpleNamespace(ensureLoggedIn=AsyncMock(return_value=True)),
        Settings(
            data_dir=tmp_path / "data",
            db_path=tmp_path / "state" / "test.sqlite3",
            account_poll_spacing_seconds=0,
            account_poll_jitter_seconds=0,
        ),
    )
    monitor._poll_page = SimpleNamespace()
    monitor._get_polling_page = AsyncMock(return_value=monitor._poll_page)
    yield monitor
    database._engine.dispose()
    database._engine = None
    database._SessionLocal = None


@pytest.mark.asyncio
async def test_due_accounts_are_fair_persisted_and_not_polled_twice(scheduler_db):
    monitor = scheduler_db
    monitor._poll_account = AsyncMock(return_value=None)
    await monitor._poll_all_accounts()
    assert [call.args[0] for call in monitor._poll_account.call_args_list] == ["new", "old"]
    await monitor._poll_all_accounts()
    assert monitor._poll_account.await_count == 2
    with get_session() as db:
        account = db.query(MonitoredAccount).filter_by(username="new").one()
        assert account.last_poll_outcome == "checked"
        assert account.next_check_at > account.last_checked_at
        assert account.last_poll_request_count is None
        assert account.last_poll_duration_seconds >= 0


@pytest.mark.asyncio
async def test_rate_limit_preserves_unattempted_accounts(scheduler_db, monkeypatch):
    monitor = scheduler_db
    remaining = 0

    async def limited(_username, page=None):
        nonlocal remaining
        remaining = 900

    monitor._poll_account = limited
    monkeypatch.setattr("xsa.browser.adapter.viewer_retry_remaining", lambda: remaining)
    await monitor._poll_all_accounts()
    with get_session() as db:
        first = db.query(MonitoredAccount).filter_by(username="new").one()
        later = db.query(MonitoredAccount).filter_by(username="old").one()
        assert first.last_poll_outcome == "rate_limited"
        assert (first.next_check_at - first.last_checked_at).total_seconds() >= 900
        assert later.last_checked_at == datetime(2026, 1, 1)
        assert later.next_check_at is None


@pytest.mark.asyncio
async def test_authentication_failure_gets_its_own_backoff(scheduler_db):
    scheduler_db._poll_account = AsyncMock(side_effect=AccountPollError("authentication_required"))
    await scheduler_db._poll_all_accounts()
    with get_session() as db:
        account = db.query(MonitoredAccount).filter_by(username="new").one()
        assert account.last_poll_outcome == "authentication_required"
        assert account.poll_failure_streak == 1
        assert (
            account.next_check_at - account.last_checked_at
        ).total_seconds() == 2 * scheduler_db._cfg.poll_interval_seconds


@pytest.mark.asyncio
async def test_discovery_graphql_budget_admits_exact_limit(scheduler_db):
    monitor = scheduler_db
    monitor._cfg.max_graphql_requests_per_cycle = 2
    for expected in (1, 2, 2):
        route = SimpleNamespace(
            request=SimpleNamespace(url="https://x.com/i/api/graphql/hash/Viewer"),
            continue_=AsyncMock(),
            abort=AsyncMock(),
        )
        await monitor._admit_graphql_route(route)
        assert monitor._graphql_budget.used() == expected
        if expected < 2 or route.continue_.await_count:
            route.continue_.assert_awaited_once()
            route.abort.assert_not_awaited()
        else:
            route.abort.assert_awaited_once()
