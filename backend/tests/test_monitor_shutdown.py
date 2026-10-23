from __future__ import annotations

import asyncio

import pytest

from xsa.monitor.monitor import AccountMonitor


@pytest.mark.asyncio
async def test_shutdown_stops_capture_before_waiting_for_post_processing() -> None:
    monitor = object.__new__(AccountMonitor)
    monitor._running = True
    calls: list[str] = []

    class Session:
        async def stop(self, *, reason=None) -> None:
            assert reason == "shutdown"
            calls.append("capture_stopped")

    async def post_process() -> None:
        calls.append("post_processing_started")

    monitor._active_sessions = {"space123": Session()}
    monitor._active_tasks = {}
    monitor._post_processing_tasks = {"space123": asyncio.create_task(post_process())}
    monitor._poll_context = None
    monitor._poll_browser = None

    await monitor.stop()

    assert calls == ["capture_stopped", "post_processing_started"]
