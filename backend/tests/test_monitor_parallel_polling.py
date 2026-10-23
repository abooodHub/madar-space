from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xsa.config import Settings
from xsa.monitor import monitor as monitor_package


@pytest.mark.asyncio
async def test_account_polling_uses_one_page_to_avoid_viewer_bursts(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("xsa.monitor.monitor")
    accounts = [SimpleNamespace(username=f"user_{index}") for index in range(8)]

    class Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return accounts

        def filter_by(self, username):
            self.username = username
            return self

        def first(self):
            return SimpleNamespace(username=self.username, last_checked_at=None)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def query(self, _model):
            return Query()

        def commit(self):
            pass

    monkeypatch.setattr(module, "get_session", Session)
    adapter = SimpleNamespace(ensureLoggedIn=AsyncMock(return_value=True))
    monitor = monitor_package.AccountMonitor(
        adapter=adapter,
        cfg=Settings(
            data_dir=tmp_path / "data",
            db_path=tmp_path / "state" / "xsa.sqlite3",
            account_poll_spacing_seconds=0,
        ),
    )
    pages = []

    class Page:
        async def close(self):
            self.closed = True

    first_page = Page()
    monitor._poll_page = first_page
    monitor._poll_context = SimpleNamespace(new_page=AsyncMock())

    async def new_page():
        page = Page()
        pages.append(page)
        return page

    monitor._poll_context.new_page.side_effect = new_page
    monitor._get_polling_page = AsyncMock(return_value=first_page)
    active = 0
    max_active = 0

    async def poll_account(_username, page=None):
        nonlocal active, max_active
        assert page is not None
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return None

    monitor._poll_account = poll_account

    await monitor._poll_all_accounts()

    assert max_active == 1
    assert not pages
