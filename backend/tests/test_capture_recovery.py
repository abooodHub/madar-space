from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from xsa.config import Settings
from xsa.paths import SpacePaths
from xsa.recorder.recorder import DockerRecorder
from xsa.recorder.session import SpaceSession
from xsa.recorder.slot_pool import DisplaySlotPool


@pytest.fixture
def capture(tmp_path, monkeypatch):
    paths = SpacePaths.for_space(tmp_path / "data", "recover123", datetime.now(UTC))
    paths.root.mkdir(parents=True)
    old = paths.segment(0)
    old.write_bytes(b"preserved old media")
    calls = []
    slot = SimpleNamespace(
        display=":100",
        sink_name="private_sink",
        slot_id=0,
        check_resources=Mock(side_effect=RuntimeError("display died")),
    )
    adapter = SimpleNamespace(
        _active_space_id="recover123",
        get_space_end_evidence=AsyncMock(return_value={"state": "LIVE"}),
        openSpace=AsyncMock(return_value=True),
    )

    async def close(**kwargs):
        assert kwargs == {"remove_profile": False}
        calls.append("browser_close")

    async def start_browser():
        calls.append("browser_start")

    adapter.close = AsyncMock(side_effect=close)
    adapter.start = AsyncMock(side_effect=start_browser)
    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "test.sqlite3")
    session = SpaceSession(
        "recover123", "https://x.com/i/spaces/recover123/peek", "host", paths, adapter, cfg, slot
    )
    session._running = True

    async def stop():
        calls.append("ffmpeg_stop")
        return old

    async def start(path, _index):
        calls.append("ffmpeg_start")
        path.write_bytes(b"replacement media")

    session._recorder = SimpleNamespace(
        get_status=lambda: SimpleNamespace(error="ffmpeg exited"),
        get_current_file=lambda: old,
        stop=AsyncMock(side_effect=stop),
        start=AsyncMock(side_effect=start),
        health=lambda: True,
    )
    session._persist_segment_stop = Mock()
    session._persist_segment_start = Mock()

    async def rebuild(_slot):
        calls.append("rebuild_slot")

    pool = SimpleNamespace(recover_slot=AsyncMock(side_effect=rebuild))
    monkeypatch.setattr("xsa.recorder.slot_pool.get_slot_pool", lambda: pool)
    monkeypatch.setattr("xsa.recorder.recorder.browser_audio_route_ready", lambda _sink: True)
    return session, pool, calls, old


@pytest.mark.asyncio
async def test_crashed_display_recovery_preserves_files_and_enforces_order(capture):
    session, pool, calls, old = capture
    assert await session._restart_segment()
    assert calls == ["ffmpeg_stop", "browser_close", "rebuild_slot", "browser_start", "ffmpeg_start"]
    assert old.read_bytes() == b"preserved old media"
    assert session.paths.segment(1).read_bytes() == b"replacement media"
    assert session.adapter.openSpace.call_args.kwargs["prefer_anonymous"] is True
    rows = [
        json.loads(line)
        for line in (session.paths.root / "capture_recoveries.jsonl").read_text().splitlines()
    ]
    assert [row["status"] for row in rows] == ["recovering", "recovered"]
    assert rows[-1]["confirmed_gap_seconds"] >= 0
    assert rows[-1]["loss_started_at"] is None
    pool.recover_slot.assert_awaited_once_with(session.slot)


@pytest.mark.asyncio
async def test_uncertain_ffmpeg_exit_never_tears_down_resources(capture):
    session, pool, calls, old = capture
    session._recorder.stop.side_effect = RuntimeError("still running")
    assert not await session._restart_segment()
    session.adapter.close.assert_not_awaited()
    pool.recover_slot.assert_not_awaited()
    session._recorder.start.assert_not_awaited()
    assert old.read_bytes() == b"preserved old media"
    assert session._capture_recoveries[-1]["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["privacy", "wrong_space"])
async def test_recovery_requires_identity_and_exact_original_space(capture, failure):
    session, _pool, _calls, _old = capture
    if failure == "privacy":
        session.adapter.openSpace.return_value = False
    else:
        session.adapter._active_space_id = "unrelated_space"
    assert not await session._restart_segment()
    session._recorder.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_ffmpeg_only_failure_keeps_healthy_browser_and_display(capture):
    session, pool, calls, _old = capture
    session.slot.check_resources.side_effect = None
    assert await session._restart_segment()
    assert calls == ["ffmpeg_stop", "ffmpeg_start"]
    session.adapter.close.assert_not_awaited()
    pool.recover_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_during_resource_recovery_prevents_new_capture(capture):
    session, _pool, calls, _old = capture
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def close(**_kwargs):
        entered.set()
        await proceed.wait()

    session.adapter.close.side_effect = close
    session._stop_impl = AsyncMock(return_value={"stopped": True})
    recovery = asyncio.create_task(session._restart_segment())
    await entered.wait()
    stop = asyncio.create_task(session.stop(reason="shutdown"))
    await asyncio.sleep(0)
    proceed.set()
    assert not await recovery
    assert await stop == {"stopped": True}
    session._recorder.start.assert_not_awaited()
    assert "browser_start" not in calls


@pytest.mark.asyncio
async def test_failed_slot_rebuild_keeps_reservation_quarantined(monkeypatch):
    pool = DisplaySlotPool(max_slots=1)
    slot = pool._slots[0]
    slot.is_busy = True

    async def release():
        slot.is_busy = False

    slot.release = AsyncMock(side_effect=release)
    slot.acquire = AsyncMock(side_effect=RuntimeError("Xvfb unavailable"))
    with pytest.raises(RuntimeError, match="Xvfb unavailable"):
        await pool.recover_slot(slot)
    assert slot.is_busy
    assert await pool.acquire_slot() is None


def test_pool_growth_retains_existing_session_reservations(monkeypatch):
    from xsa.recorder import slot_pool

    monkeypatch.setattr(slot_pool, "_pool", None)
    pool = slot_pool.get_slot_pool(max_slots=1)
    slot = pool._slots[0]
    slot.is_busy = True
    assert slot_pool.get_slot_pool(max_slots=3) is pool
    assert pool._slots[0] is slot and slot.is_busy
    assert len(pool._slots) == 3
    assert slot_pool.get_slot_pool().max_slots == 3


@pytest.mark.asyncio
async def test_recorder_refuses_overwrite_and_implicit_stop(tmp_path):
    recorder = DockerRecorder()
    path = tmp_path / "recording_000.mkv"
    path.write_bytes(b"historical media")
    with pytest.raises(FileExistsError):
        await recorder.start(path)
    recorder._proc = SimpleNamespace(poll=lambda: None)
    with pytest.raises(RuntimeError, match="already active"):
        await recorder.start(tmp_path / "recording_001.mkv")
    assert path.read_bytes() == b"historical media"
