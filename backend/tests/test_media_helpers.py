from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from xsa.recorder import media


def test_playback_readiness_detects_changed_source_and_new_segments(tmp_path: Path) -> None:
    source = tmp_path / "recording_000.mkv"
    playback = tmp_path / "playback.mp4"
    source.write_bytes(b"original")
    playback.write_bytes(b"prepared")
    original_mtime = source.stat().st_mtime_ns
    (tmp_path / "playback.mp4.source.json").write_text(
        json.dumps({"source": source.name, "size_bytes": source.stat().st_size, "mtime_ns": original_mtime}),
        encoding="utf-8",
    )
    paths = SimpleNamespace(root=tmp_path)
    assert media.playback_readiness_for_paths(paths) == (True, "ready")

    source.write_bytes(b"restored with different bytes")
    os.utime(source, ns=(original_mtime, original_mtime))
    assert media.playback_readiness_for_paths(paths) == (False, "source_changed")

    source.write_bytes(b"original")
    os.utime(source, ns=(original_mtime, original_mtime))
    newer = tmp_path / "recording_001.mkv"
    newer.write_bytes(b"new segment")
    os.utime(newer, ns=(original_mtime + 1_000_000_000, original_mtime + 1_000_000_000))
    assert media.playback_readiness_for_paths(paths) == (False, "new_segments")


def test_legacy_playback_uses_timestamps_and_surviving_mp4(tmp_path: Path) -> None:
    source = tmp_path / "recording.mkv"
    playback = tmp_path / "playback.mp4"
    source.write_bytes(b"original")
    playback.write_bytes(b"prepared")
    source_time = source.stat().st_mtime_ns
    os.utime(playback, ns=(source_time + 1_000_000_000, source_time + 1_000_000_000))
    paths = SimpleNamespace(root=tmp_path)
    assert media.playback_readiness_for_paths(paths) == (True, "ready_legacy")
    os.utime(source, ns=(source_time + 2_000_000_000, source_time + 2_000_000_000))
    assert media.playback_readiness_for_paths(paths) == (False, "source_changed")
    source.unlink()
    assert media.playback_readiness_for_paths(paths) == (True, "source_unavailable")


def test_create_playback_mp4_allows_large_faststart_remux(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "recording.mkv"
    target = tmp_path / "playback.mp4"
    source.touch()
    timeouts: list[int] = []

    def fake_run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        Path(command[-1]).write_bytes(b"valid mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    monkeypatch.setattr(media, "has_audio_video", lambda _path: True)

    assert media.create_playback_mp4(source, target)
    assert timeouts == [1800]
    assert target.read_bytes() == b"valid mp4"
    assert not (tmp_path / "playback.partial.mp4").exists()


def test_create_thumbnail_publishes_atomically(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "recording.mkv"
    target = tmp_path / "thumbnail.jpg"
    source.touch()
    target.write_bytes(b"old thumbnail")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"new thumbnail")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    assert media.create_thumbnail(source, target, seek_seconds=15)
    assert target.read_bytes() == b"new thumbnail"
    assert commands[0][commands[0].index("-ss") + 1] == "15"
    assert not (tmp_path / "thumbnail.partial.jpg").exists()


def test_create_thumbnail_failure_keeps_existing_thumbnail(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "recording.mkv"
    target = tmp_path / "thumbnail.jpg"
    source.touch()
    target.write_bytes(b"existing thumbnail")

    def fail_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 120)

    monkeypatch.setattr(media.subprocess, "run", fail_run)

    assert not media.create_thumbnail(source, target)
    assert target.read_bytes() == b"existing thumbnail"
    assert not (tmp_path / "thumbnail.partial.jpg").exists()


def test_session_media_finalization_does_not_block_event_loop(monkeypatch, tmp_path: Path) -> None:
    from xsa.recorder.session import SpaceSession

    source = tmp_path / "recording_000.mkv"
    source.write_bytes(b"recording")
    playback = tmp_path / "playback.mp4"
    thumbnail = tmp_path / "thumbnail.jpg"
    paths = SimpleNamespace(root=tmp_path, playback=playback, thumbnail=thumbnail)

    def slow_valid_media(path: Path) -> bool:
        time.sleep(0.08)
        return path == source or path == playback and playback.exists()

    def slow_playback(_source: Path, target: Path) -> bool:
        time.sleep(0.08)
        target.write_bytes(b"playback")
        return True

    def slow_thumbnail(_source: Path, target: Path) -> bool:
        time.sleep(0.08)
        target.write_bytes(b"thumbnail")
        return True

    monkeypatch.setattr(media, "has_audio_video", slow_valid_media)
    monkeypatch.setattr(media, "create_playback_mp4", slow_playback)
    monkeypatch.setattr(media, "create_thumbnail", slow_thumbnail)

    session = object.__new__(SpaceSession)
    session.paths = paths

    async def run() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(12):
                await asyncio.sleep(0.01)
                ticks += 1

        finalized, _ = await asyncio.gather(session._finalize_recording_media(source), ticker())
        assert finalized == source
        return ticks

    assert asyncio.run(run()) >= 8
    assert playback.read_bytes() == b"playback"
    assert thumbnail.read_bytes() == b"thumbnail"


def test_video_route_does_not_prepare_media_on_get(monkeypatch, tmp_path: Path) -> None:
    api = importlib.import_module("xsa.api.app")

    source = tmp_path / "recording_000.mkv"
    source.write_bytes(b"recording")
    playback = tmp_path / "playback.mp4"
    paths = SimpleNamespace(root=tmp_path, playback=playback, recording=tmp_path / "recording.mkv")

    monkeypatch.setattr(api.SpacePaths, "resolve_existing", lambda *_args: paths)
    monkeypatch.setattr(media, "get_recording_source", lambda *_args: pytest.fail("GET merged media"))
    monkeypatch.setattr(media, "create_playback_mp4", lambda *_args: pytest.fail("GET ran FFmpeg"))

    response = api.stream_space_video("space123")
    assert response.status_code == 202
    assert b"prepare-media" in response.body
    assert not playback.exists()

    playback.write_bytes(b"prepared mp4")
    response = api.stream_space_video("space123")
    assert response.path == playback

    source.write_bytes(b"recording changed")
    changed_time = playback.stat().st_mtime_ns + 1_000_000_000
    os.utime(source, ns=(changed_time, changed_time))
    response = api.stream_space_video("space123")
    assert response.status_code == 202


def test_thumbnail_route_never_runs_ffmpeg(monkeypatch, tmp_path: Path) -> None:
    api = importlib.import_module("xsa.api.app")
    source = tmp_path / "recording_000.mkv"
    source.write_bytes(b"recording")
    paths = SimpleNamespace(
        root=tmp_path, thumbnail=tmp_path / "thumbnail.jpg", screenshots=tmp_path / "screenshots"
    )
    monkeypatch.setattr(api.SpacePaths, "resolve_existing", lambda *_args: paths)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("GET ran FFmpeg"))

    with pytest.raises(api.HTTPException) as error:
        api.get_space_thumbnail("space123")
    assert error.value.status_code == 404
    assert not paths.thumbnail.exists()
