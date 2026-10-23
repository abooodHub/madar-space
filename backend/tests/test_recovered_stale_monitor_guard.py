from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xsa.browser.adapter import SpaceState
from xsa.config import Settings
from xsa.monitor import monitor as monitor_package


@pytest.mark.parametrize("state", ["recovered_stale", "development_interrupted", "blocked"])
@pytest.mark.asyncio
async def test_terminal_space_is_not_joined_again(tmp_path, monkeypatch, state):
    import importlib

    module = importlib.import_module("xsa.monitor.monitor")
    monitor = monitor_package.AccountMonitor(
        adapter=None,
        cfg=Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "xsa.sqlite3"),
    )
    monitor._get_polling_page = AsyncMock(return_value=object())
    monitor._detect_space_on_page = AsyncMock(
        return_value=SpaceState(
            space_id="staleSpace123",
            host_username="host",
            state="live",
            url="https://x.com/i/spaces/staleSpace123/peek",
        )
    )

    class Query:
        def filter_by(self, **_kwargs):
            return self

        def first(self):
            return SimpleNamespace(state=state)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def query(self, _model):
            return Query()

    monkeypatch.setattr(module, "maintenance_active", lambda _state_dir: False)
    monkeypatch.setattr(module, "get_session", Session)

    assert await monitor._poll_account("host") is None
    monitor._detect_space_on_page.assert_awaited_once()
