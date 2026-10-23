from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from xsa.browser.adapter import XBrowserAdapter
from xsa.space_end import SpaceEndDetector, SpaceEndState, should_stop_unverified_silence


@pytest.mark.parametrize("state", ["DISCONNECTED", "UNKNOWN"])
def test_connection_or_unknown_state_never_confirms_end(state: str) -> None:
    detector = SpaceEndDetector(confirm_seconds=2)
    assert detector.observe({"state": state}, now=0) == SpaceEndState(state)
    assert detector.observe({"state": state}, now=30) == SpaceEndState(state)


def test_long_silence_and_no_speaker_do_not_stop_a_live_space() -> None:
    detector = SpaceEndDetector(confirm_seconds=2)
    live = {"state": "LIVE", "source": "metadata", "evidence": "state=Running", "confidence": 0.99}
    for second in range(0, 600, 5):
        assert detector.observe(live, now=second) == SpaceEndState.LIVE
    assert detector.detected_at is None


def test_unverified_silence_guard_requires_both_long_silence_and_stale_space_state():
    assert not should_stop_unverified_silence(
        silence_seconds=3600, space_state=SpaceEndState.LIVE, unknown_seconds=3600
    )
    assert not should_stop_unverified_silence(
        silence_seconds=1200, space_state=SpaceEndState.UNKNOWN, unknown_seconds=1200
    )
    assert not should_stop_unverified_silence(
        silence_seconds=3600, space_state=SpaceEndState.UNKNOWN, unknown_seconds=120
    )
    assert should_stop_unverified_silence(
        silence_seconds=1800, space_state=SpaceEndState.UNKNOWN, unknown_seconds=300
    )
    assert should_stop_unverified_silence(
        silence_seconds=1800, space_state=SpaceEndState.DISCONNECTED, unknown_seconds=300
    )


def test_end_requires_repeat_confirmation_and_live_signal_cancels_suspect() -> None:
    detector = SpaceEndDetector(confirm_seconds=2)
    ended = {"state": "ENDED", "source": "metadata", "evidence": "state=Ended"}
    assert detector.observe(ended, now=1) == SpaceEndState.SUSPECTED_ENDED
    assert detector.observe({"state": "UNKNOWN"}, now=2) == SpaceEndState.SUSPECTED_ENDED
    assert (
        detector.observe({"state": "LIVE", "source": "metadata", "confidence": 0.99}, now=2.5)
        == SpaceEndState.LIVE
    )
    assert detector.observe(ended, now=3) == SpaceEndState.SUSPECTED_ENDED
    assert detector.observe(ended, now=4.9) == SpaceEndState.SUSPECTED_ENDED
    assert detector.observe(ended, now=5) == SpaceEndState.ENDED
    detected_at = detector.detected_at
    assert detector.observe(ended, now=20) == SpaceEndState.ENDED
    assert detector.detected_at == detected_at


class FakeContext:
    def is_closed(self) -> bool:
        return False


class FakePage:
    def __init__(self, url: str, dom: dict | None = None, raises: bool = False) -> None:
        self.url = url
        self.dom = dom or {}
        self.raises = raises

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, _script: str):
        if self.raises:
            raise ConnectionError("temporary page evaluation failure")
        return self.dom


async def _adapter_evidence(
    monkeypatch,
    url: str,
    gql: str | None,
    dom: dict | None = None,
    raises: bool = False,
    check_metadata: bool = True,
) -> dict:
    adapter = XBrowserAdapter(Path("unused"))
    adapter._active_space_id = "space123"
    adapter._context = FakeContext()
    adapter._page = FakePage(url, dom, raises)
    adapter.getSpaceStateGQL = AsyncMock(return_value=gql)
    return await adapter.get_space_end_evidence("space123", check_metadata=check_metadata)


@pytest.mark.asyncio
async def test_explicit_metadata_end_is_bound_to_requested_space(monkeypatch) -> None:
    result = await _adapter_evidence(monkeypatch, "https://x.com/i/spaces/space123/peek", "ended")
    assert result["state"] == "ENDED"
    assert result["source"] == "metadata"


@pytest.mark.asyncio
async def test_explicit_dom_ended_message_on_locked_space_is_end(monkeypatch) -> None:
    result = await _adapter_evidence(
        monkeypatch,
        "https://x.com/i/spaces/space123/peek",
        None,
        {"explicit_ended": True},
    )
    assert result["state"] == "ENDED"
    assert result["source"] == "x_dom"


@pytest.mark.asyncio
async def test_conflicting_live_metadata_prevents_dom_false_positive(monkeypatch) -> None:
    result = await _adapter_evidence(
        monkeypatch,
        "https://x.com/i/spaces/space123/peek",
        "live",
        {"explicit_ended": True},
    )
    assert result["state"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_temporary_dom_disappearance_is_unknown_not_ended(monkeypatch) -> None:
    result = await _adapter_evidence(
        monkeypatch,
        "https://x.com/i/spaces/space123/peek",
        None,
        {"explicit_ended": False, "replay_ended": False, "space_dock_visible": False},
    )
    assert result["state"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_temporary_network_or_page_failure_is_not_ended(monkeypatch) -> None:
    result = await _adapter_evidence(
        monkeypatch,
        "https://x.com/i/spaces/space123/peek",
        None,
        raises=True,
    )
    assert result["state"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_end_evidence_can_skip_metadata_poll_while_still_checking_dom(monkeypatch) -> None:
    adapter = XBrowserAdapter(Path("unused"))
    adapter._active_space_id = "space123"
    adapter._context = FakeContext()
    adapter._page = FakePage("https://x.com/i/spaces/space123", {"space_dock_visible": True})
    adapter.getSpaceStateGQL = AsyncMock(return_value="ended")

    result = await adapter.get_space_end_evidence("space123", check_metadata=False)

    adapter.getSpaceStateGQL.assert_not_awaited()
    assert result["state"] == "LIVE"


@pytest.mark.asyncio
async def test_replay_state_on_locked_space_is_explicit_end(monkeypatch) -> None:
    result = await _adapter_evidence(
        monkeypatch,
        "https://x.com/i/spaces/space123",
        None,
        {"replay_ended": True},
    )
    assert result["state"] == "ENDED"
    assert result["source"] == "x_dom"


@pytest.mark.asyncio
async def test_unrelated_space_route_is_unknown_and_never_reused(monkeypatch) -> None:
    result = await _adapter_evidence(
        monkeypatch,
        "https://x.com/i/spaces/otherSpace",
        "live",
        {"space_dock_visible": True},
    )
    assert result["state"] == "UNKNOWN"
    assert result["evidence"].startswith("space_id_mismatch")


@pytest.mark.asyncio
async def test_audio_helper_does_not_start_unrelated_video_or_media() -> None:
    adapter = XBrowserAdapter(Path("unused"))
    fake_page = FakePage("https://x.com/i/spaces/space123")
    adapter._page = fake_page
    captured: dict[str, str] = {}

    async def evaluate(script: str):
        captured["script"] = script
        return {"total": 0, "fixed": 0, "player_found": True}

    fake_page.evaluate = evaluate
    await adapter.ensureAudioPlaying()
    assert "querySelectorAll('audio')" in captured["script"]
    assert "querySelectorAll('audio, video')" not in captured["script"]
    assert ".play()" not in captured["script"]
    assert "AudioContext" not in captured["script"]


@pytest.mark.asyncio
async def test_post_stop_quarantine_pauses_and_mutes_all_page_media() -> None:
    adapter = XBrowserAdapter(Path("unused"))
    fake_page = FakePage("https://x.com/i/spaces/space123")
    adapter._page = fake_page
    captured: dict[str, str] = {}

    async def evaluate(script: str):
        captured["script"] = script

    fake_page.evaluate = evaluate
    await adapter.quarantine_space_media()
    assert "querySelectorAll('audio, video')" in captured["script"]
    assert "el.pause()" in captured["script"]
    assert "el.muted = true" in captured["script"]


@pytest.mark.asyncio
async def test_hls_observation_stays_space_bound_and_redacts_query_tokens() -> None:
    adapter = XBrowserAdapter(Path("unused"))
    adapter._active_space_id = "space123"
    adapter._last_space_end_observation = {"state": "LIVE"}

    class Request:
        url = "https://video.example/master.m3u8?token=secret"

        class Frame:
            url = "https://x.com/i/spaces/space123"

        frame = Frame()

    class Route:
        request = Request()

        async def continue_(self):
            pass

    await adapter._capture_hls(Route())
    assert adapter._hls_observations[0]["active_space_id"] == "space123"
    assert adapter._hls_observations[0]["media_url"] == "https://video.example/master.m3u8"
    assert "secret" not in repr(adapter._hls_observations)

    adapter._last_space_end_observation = {"state": "ENDED"}
    await adapter._capture_hls(Route())
    assert len(adapter._hls_observations) == 1


@pytest.mark.asyncio
async def test_ffmpeg_stop_precedes_browser_cleanup() -> None:
    from xsa.recorder.session import SpaceSession

    session = object.__new__(SpaceSession)
    session._running = True
    session._recorder = type("Recorder", (), {})()
    calls: list[str] = []

    async def stop_recorder():
        calls.append("ffmpeg_stop")
        return Path("recording.mkv")

    session._recorder.stop = stop_recorder
    session._rec_wall_start = None
    session._current_offset_ms = lambda: 5000
    session.space_end_recording_time = 3.0
    session.recording_stopped_at = None
    session.recording_stop_recording_time = None
    session.end_tail_duration = None
    session.space_end_detected_at = None
    session.space_end_source = None
    session.space_end_evidence = None
    session.space_end_confidence = None
    session._persist_space_end_events = lambda: calls.append("persist")
    session.adapter = type("Adapter", (), {})()

    async def quarantine():
        calls.append("browser_cleanup")

    session.adapter.quarantine_space_media = quarantine
    result = await session._stop_capture_before_browser_cleanup()
    assert result == Path("recording.mkv")
    assert calls == ["ffmpeg_stop", "browser_cleanup", "persist"]


@pytest.mark.asyncio
async def test_session_stop_is_idempotent() -> None:
    from xsa.recorder.session import SpaceSession

    session = object.__new__(SpaceSession)
    session._stop_lock = asyncio.Lock()
    session._stop_summary = None
    calls: list[str] = []

    async def stop_impl():
        calls.append("stop")
        return {"space_id": "space123"}

    session._stop_impl = stop_impl
    first = await session.stop()
    second = await session.stop()
    assert first is second
    assert calls == ["stop"]
