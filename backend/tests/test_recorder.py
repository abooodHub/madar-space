from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from xsa.recorder.recorder import DockerRecorder, RecorderStatus


class FakeStdin:
    closed = False

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, value: bytes) -> int:
        if not isinstance(value, bytes):
            raise TypeError("a bytes-like object is required")
        self.data.extend(value)
        return len(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_stop_sends_binary_quit_and_reaps_ffmpeg(tmp_path: Path) -> None:
    recorder = DockerRecorder()
    process = FakeProcess()
    output = tmp_path / "recording_000.mkv"
    recorder._proc = process  # type: ignore[assignment]
    recorder._output = output
    recorder._status = RecorderStatus(state="recording", current_file=output)

    result = await recorder.stop()

    assert result == output
    assert bytes(process.stdin.data) == b"q\n"
    assert process.stdin.closed
    assert process.returncode == 0
    assert recorder._proc is None
    assert recorder._status.health is False


@pytest.mark.asyncio
async def test_stop_uses_termination_fallback_when_quit_pipe_is_broken(tmp_path: Path) -> None:
    recorder = DockerRecorder()
    process = FakeProcess()

    def broken_write(_: bytes) -> int:
        raise BrokenPipeError("pipe closed")

    process.stdin.write = broken_write  # type: ignore[method-assign]
    recorder._proc = process  # type: ignore[assignment]
    recorder._output = tmp_path / "recording_000.mkv"
    recorder._status = RecorderStatus(state="recording")

    await recorder.stop()

    # FFmpeg had already exited by the time wait returned; escalation is only
    # required if its process remains alive after the graceful wait timeout.
    assert process.returncode == 0
    assert recorder._proc is None


@pytest.mark.asyncio
async def test_start_refuses_recording_below_configured_disk_floor(tmp_path: Path) -> None:
    recorder = DockerRecorder(cfg=SimpleNamespace(min_free_disk_gb=20))  # type: ignore[arg-type]
    with (
        patch("xsa.recorder.recorder.shutil.disk_usage", return_value=SimpleNamespace(free=10)),
        pytest.raises(RuntimeError, match="Insufficient free disk space"),
    ):
        await recorder.start(tmp_path / "recording.mkv")


def test_health_reports_stalled_output(tmp_path: Path) -> None:
    recorder = DockerRecorder()
    process = FakeProcess()
    process.returncode = None
    output = tmp_path / "recording.mkv"
    output.write_bytes(b"some media")
    recorder._proc = process  # type: ignore[assignment]
    recorder._output = output
    recorder._start_wall = 0
    recorder._last_progress_size = output.stat().st_size
    recorder._last_progress_wall = 0
    with patch("xsa.recorder.recorder.time.monotonic", return_value=61):
        assert recorder.health() is False
    assert "not grown" in (recorder.get_status().error or "")
