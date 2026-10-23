"""Recorder interface and DockerRecorder backend.

Recorder
    start()         → begin recording the virtual display + isolated audio sink
    stop()          → stop FFmpeg gracefully; finalize the segment
    health()        → True if FFmpeg is alive and writing
    get_status()    → RecorderStatus dataclass
    get_current_file() → Path to the current segment file

DockerRecorder uses x11grab + PulseAudio monitor via FFmpeg inside the
same container (Xvfb display :99, PulseAudio sink 'xspace').
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xsa.config import Settings, get_settings
from xsa.logging_setup import get_logger

log = get_logger("RECORDER")


def browser_audio_route_ready(sink: str) -> bool:
    """Require a Chrome output stream on the reserved sink, not just a silent monitor."""
    try:
        sinks = json.loads(subprocess.check_output(["pactl", "-f", "json", "list", "sinks"], timeout=3))
        inputs = json.loads(
            subprocess.check_output(["pactl", "-f", "json", "list", "sink-inputs"], timeout=3)
        )
        ids = {str(item["index"]) for item in sinks if item.get("name") == sink}
        return any(
            str(item.get("sink")) in ids
            and not item.get("corked")
            and any(
                name in str(item.get("properties", {}).get("application.process.binary", "")).lower()
                for name in ("chrome", "chromium")
            )
            for item in inputs
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Shared data types
# ---------------------------------------------------------------------------


@dataclass
class RecorderStatus:
    state: str = "idle"  # idle | starting | recording | stopping | error
    segment_index: int = 0
    current_file: Path | None = None
    started_at: datetime | None = None
    duration_seconds: float = 0.0
    file_bytes: int = 0
    health: bool = False
    error: str | None = None
    dropped_frames: int = 0


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class Recorder(ABC):
    @abstractmethod
    async def start(self, output_path: Path, segment_index: int = 0) -> None: ...

    @abstractmethod
    async def stop(self) -> Path | None: ...

    @abstractmethod
    def health(self) -> bool: ...

    @abstractmethod
    def get_status(self) -> RecorderStatus: ...

    @abstractmethod
    def get_current_file(self) -> Path | None: ...


# ---------------------------------------------------------------------------
# DockerRecorder — x11grab + PulseAudio, runs INSIDE the capture container
# ---------------------------------------------------------------------------


class DockerRecorder(Recorder):
    """
    Runs FFmpeg as a subprocess inside the container (or native Windows host
    for the Windows fallback). Designed to be used from within the capture
    container where DISPLAY and PULSE_SERVER are set.
    """

    def __init__(
        self, cfg: Settings | None = None, display: str | None = None, sink: str | None = None
    ) -> None:
        self._cfg = cfg or get_settings()
        self._display = display
        self._sink = sink
        self._proc: subprocess.Popen | None = None
        self._output: Path | None = None
        self._start_wall: float | None = None
        self._segment_index: int = 0
        self._status = RecorderStatus()
        self._drop_count: int = 0
        self._last_progress_size: int = 0
        self._last_progress_wall: float | None = None
        self._silence_log_offset: int = 0
        self._silence_log_buffer: bytes = b""
        self._silence_started_monotonic: float | None = None
        self._silence_started_at: datetime | None = None
        self._silence_last_duration: float | None = None
        self._audio_measurement = "unavailable"
        self._audio_input_ready = False
        self._silence_raw_start: float | None = None
        self._silence_media_start: float | None = None
        self._audio_rms_db: float | None = None
        self._audio_level_silent: bool | None = None
        self._audio_last_measurement: float | None = None

    def _build_cmd(self, output: Path) -> list[str]:
        cfg = self._cfg
        display = self._display or cfg.master_display
        size = cfg.record_size
        fps = cfg.record_fps
        sink = self._sink or "xspace"  # Isolated PulseAudio null sink

        if cfg.video_encoder == "nvenc":
            venc = ["-c:v", "h264_nvenc", "-preset", "p2", "-cq", "24", "-bf", "2"]
        else:
            venc = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-crf", "23"]

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-stats",
            "-stats_period",
            "10",
            "-y",
            # ---- video input (virtual display) ----
            "-thread_queue_size",
            "2048",
            "-probesize",
            "32M",
            "-f",
            "x11grab",
            "-framerate",
            str(fps),
            "-video_size",
            size,
            "-draw_mouse",
            "0",
            "-i",
            f"{display}.0",
            # ---- audio input (isolated PulseAudio sink monitor) ----
            "-thread_queue_size",
            "2048",
            "-f",
            "pulse",
            "-sample_rate",
            "48000",
            "-channels",
            "2",
            "-i",
            f"{sink}.monitor",
            # ---- streams ----
            "-map",
            "0:v",
            "-map",
            "[record_audio]",
            "-r",
            str(fps),
            "-fps_mode",
            "cfr",
            *venc,
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(fps * 2),
            # Professional audio sync filter to lock audio to video clock
            "-filter_complex",
            "[1:a]asplit=2[record_input][measure_input];"
            f"[record_input]aresample=async=1000:first_pts=0,silencedetect=noise={self._cfg.silence_noise_db:g}dB:d=1[record_audio];"
            "[measure_input]asetnsamples=n=48000:p=0,astats=metadata=1:reset=1,"
            "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level,anullsink",
            "-c:a",
            "aac",
            "-b:a",
            cfg.audio_bitrate,
            "-ar",
            "48000",
            # MKV is crash-resistant (keyframe-indexed)
            "-f",
            "matroska",
            str(output),
        ]

    async def start(self, output_path: Path, segment_index: int = 0) -> None:
        if output_path.exists():
            raise FileExistsError(f"Refusing to overwrite an existing recording segment: {output_path}")
        if self._proc and self._proc.poll() is None:
            raise RuntimeError("Recorder is already active; stop it before starting a replacement segment")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(output_path.parent).free
        required_bytes = int(self._cfg.min_free_disk_gb * 1024**3)
        if free_bytes < required_bytes:
            raise RuntimeError(
                f"Insufficient free disk space: {free_bytes / 1024**3:.1f} GiB available, "
                f"{self._cfg.min_free_disk_gb:.1f} GiB required"
            )
        self._output = output_path
        self._segment_index = segment_index
        cmd = self._build_cmd(output_path)
        log.info("Starting FFmpeg", cmd=shlex.join(cmd))

        log_file = output_path.parent / f"ffmpeg_{segment_index}.log"
        # Keep the descriptor open while FFmpeg owns it as its stderr target.
        self._log_fh = open(log_file, "a", encoding="utf-8")  # noqa: SIM115
        self._silence_log_offset = log_file.stat().st_size
        self._silence_log_buffer = b""
        self._silence_started_monotonic = None
        self._silence_started_at = None
        self._silence_last_duration = None
        self._audio_measurement = "unavailable"
        self._audio_input_ready = False
        self._silence_raw_start = None
        self._silence_media_start = None
        self._audio_rms_db = None
        self._audio_level_silent = None
        self._audio_last_measurement = None
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self._log_fh,
        )
        self._start_wall = time.monotonic()
        self._last_progress_size = 0
        self._last_progress_wall = self._start_wall
        self._status = RecorderStatus(
            state="recording",
            segment_index=segment_index,
            current_file=output_path,
            started_at=datetime.now(UTC),
        )
        # Give FFmpeg 2 s to start producing output; check for early exit
        await asyncio.sleep(2)
        if self._proc.poll() is not None:
            err = ""
            try:
                if log_file.exists():
                    err = log_file.read_text(encoding="utf-8")[-500:]
            except Exception:
                pass
            self._status.state = "error"
            self._status.error = f"FFmpeg exited immediately: {err}"
            log.error("FFmpeg exited immediately", error=err)
            raise RuntimeError(f"FFmpeg failed to start: {err}")
        # Input readiness is independent of audible speech. Silent PulseAudio
        # input is valid, but missing audio input/output must fail startup.
        for _ in range(16):
            self.get_audio_silence_status()
            if self._audio_input_ready and output_path.exists() and output_path.stat().st_size > 0:
                break
            if self._proc.poll() is not None:
                break
            await asyncio.sleep(0.5)
        else:
            await self.stop()
            raise RuntimeError("FFmpeg audio input/output readiness could not be verified")
        if not self._audio_input_ready or self._proc.poll() is not None:
            await self.stop()
            raise RuntimeError("FFmpeg audio input failed during startup")
        log.info("Recording started", file=str(output_path), segment=segment_index)

    async def stop(self) -> Path | None:
        proc = self._proc
        if not proc:
            return self._output
        self._status.state = "stopping"
        log.info("Stopping recorder")
        if proc.poll() is None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    # Popen uses a binary pipe. Send FFmpeg its interactive quit
                    # command and close stdin so shutdown cannot hang on the pipe.
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
                    proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError) as exc:
                log.warning("Could not send FFmpeg graceful quit; escalating", error=type(exc).__name__)

        try:
            if proc.poll() is None:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=20)
        except TimeoutError:
            log.warning("FFmpeg did not stop gracefully; sending SIGTERM")
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=5)
            except TimeoutError:
                log.error("FFmpeg ignored SIGTERM; sending SIGKILL")
                with contextlib.suppress(OSError):
                    proc.kill()
                await asyncio.to_thread(proc.wait)

        if proc.poll() is None:
            # Keep the process handle and log open. Callers must retain its
            # display/audio slot until a later stop attempt confirms exit.
            self._status.state = "error"
            self._status.health = True
            self._status.error = "FFmpeg is still running after stop escalation"
            raise RuntimeError(self._status.error)

        rc = proc.returncode
        self._status.state = "idle" if rc in (0, 255) else "error"
        self._status.health = False
        self._proc = None
        if getattr(self, "_log_fh", None) and not self._log_fh.closed:
            try:
                self._log_fh.close()
            except OSError as exc:
                log.warning("Could not close FFmpeg log", error=type(exc).__name__)
        if self._output and self._output.exists():
            self._status.file_bytes = self._output.stat().st_size
        log.info("Recorder stopped", rc=rc, file=str(self._output), bytes=self._status.file_bytes)
        return self._output

    def health(self) -> bool:
        if not self._proc:
            return False
        alive = self._proc.poll() is None
        # A live process is not healthy if the capture file has stopped advancing.
        if alive and self._output and self._output.exists():
            try:
                size = self._output.stat().st_size
            except OSError:
                size = 0
            self._status.file_bytes = size
            now = time.monotonic()
            if size > self._last_progress_size:
                self._last_progress_size = size
                self._last_progress_wall = now
            elif (
                self._start_wall is not None
                and now - self._start_wall >= 60
                and self._last_progress_wall is not None
                and now - self._last_progress_wall >= 60
            ):
                self._status.health = False
                self._status.error = "FFmpeg is alive but the recording file has not grown for 60 seconds"
                return False
        elif alive and self._start_wall is not None and time.monotonic() - self._start_wall >= 60:
            self._status.health = False
            self._status.error = "FFmpeg is alive but the recording file has not been created"
            return False
        if self._start_wall:
            self._status.duration_seconds = time.monotonic() - self._start_wall
        self._status.health = alive
        return alive

    def get_status(self) -> RecorderStatus:
        self.health()  # refresh
        return self._status

    def get_current_file(self) -> Path | None:
        return self._output

    def get_audio_silence_status(self) -> dict:
        """Read new FFmpeg silencedetect events without rescanning the growing log."""
        log_file = self._output.parent / f"ffmpeg_{self._segment_index}.log" if self._output else None
        if self._proc and self._proc.poll() is None and log_file and log_file.is_file():
            try:
                with log_file.open("rb") as stream:
                    stream.seek(self._silence_log_offset)
                    chunk = stream.read()
                self._silence_log_offset += len(chunk)
                lines = (self._silence_log_buffer + chunk).splitlines(keepends=True)
                self._silence_log_buffer = b""
                if lines and not lines[-1].endswith((b"\n", b"\r")):
                    self._silence_log_buffer = lines.pop()
                for raw_line in lines:
                    line = raw_line.decode("utf-8", errors="ignore")
                    if re.search(r"Stream #1:0.*Audio:", line):
                        self._audio_input_ready = True
                    numeric = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
                    rms_match = re.search(rf"lavfi.astats.Overall.RMS_level=({numeric}|-inf)", line)
                    if rms_match:
                        rms = float(rms_match.group(1))
                        self._audio_rms_db = rms if rms != float("-inf") else None
                        self._audio_level_silent = rms <= self._cfg.silence_noise_db
                        self._audio_last_measurement = time.monotonic()
                        self._audio_measurement = "healthy"
                    start_match = re.search(rf"silence_start:\s*({numeric})", line)
                    if start_match:
                        original = float(start_match.group(1))
                        normalized = max(0.0, original)
                        self._silence_raw_start = original
                        self._silence_media_start = normalized
                        self._silence_started_monotonic = (
                            self._start_wall + normalized
                            if self._start_wall is not None
                            else time.monotonic()
                        )
                        self._silence_started_at = (
                            self._status.started_at + timedelta(seconds=normalized)
                            if self._status.started_at
                            else datetime.now(UTC)
                        )
                        self._audio_measurement = "healthy"
                        self._audio_last_measurement = time.monotonic()
                    end_match = re.search(
                        rf"silence_end:\s*({numeric}).*?silence_duration:\s*({numeric})", line
                    )
                    if end_match:
                        self._silence_last_duration = max(0.0, float(end_match.group(2)))
                        self._silence_started_monotonic = None
                        self._silence_started_at = None
                        self._audio_measurement = "healthy"
                        self._audio_last_measurement = time.monotonic()
                    if "silence_start:" in line and not start_match:
                        self._audio_measurement = "invalid"
            except (OSError, ValueError, OverflowError):
                self._audio_measurement = "invalid"
        duration = (
            max(0.0, time.monotonic() - self._silence_started_monotonic)
            if self._silence_started_monotonic is not None
            else 0.0
        )
        threshold = float(self._cfg.silence_suspect_seconds)
        now_monotonic = time.monotonic()
        measurement_age = (
            now_monotonic - self._audio_last_measurement if self._audio_last_measurement is not None else None
        )
        if measurement_age is not None and measurement_age > 15:
            self._audio_measurement = "unavailable"
        monitoring_unavailable_seconds = (
            max(
                0.0,
                now_monotonic
                - (self._audio_last_measurement or self._start_wall or now_monotonic)
                - (15.0 if self._audio_last_measurement is not None else 0.0),
            )
            if self._audio_measurement != "healthy"
            else 0.0
        )
        return {
            "monitoring_available": self._audio_measurement == "healthy",
            "audio_measurement": self._audio_measurement,
            "audio_input_ready": self._audio_input_ready,
            "audio_rms_db": self._audio_rms_db,
            "audio_measurement_age_seconds": measurement_age,
            "audio_monitoring_unavailable_seconds": round(monitoring_unavailable_seconds, 1),
            "audio_silent": (self._silence_started_monotonic is not None) or bool(self._audio_level_silent)
            if self._audio_measurement == "healthy"
            else None,
            "silence_start_original_seconds": self._silence_raw_start,
            "silence_start_media_seconds": self._silence_media_start,
            "silence_duration_seconds": round(duration, 1),
            "silence_started_at": self._silence_started_at.isoformat() if self._silence_started_at else None,
            "silence_suspected": duration >= threshold,
            "silence_threshold_seconds": threshold,
            "capture_stop_seconds": float(self._cfg.silence_capture_stop_seconds),
            "silence_noise_db": self._cfg.silence_noise_db,
            "last_completed_silence_seconds": self._silence_last_duration,
            "end_action": "recheck_space_state_only",
        }
