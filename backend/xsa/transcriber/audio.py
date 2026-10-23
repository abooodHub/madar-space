"""Audio extraction helpers for completed Space recordings."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import wave
from pathlib import Path
from uuid import uuid4

from xsa.logging_setup import get_logger

log = get_logger("AUDIO")


def is_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def _extract(
    video_path: Path, output: Path, options: list[str], label: str, *, input_options: list[str] | None = None
) -> bool:
    if not video_path.exists():
        log.error("Input recording does not exist", path=str(video_path))
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + f".{uuid4().hex}.partial" + output.suffix)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *(input_options or []),
        "-i",
        str(video_path),
        "-vn",
        *options,
        str(partial),
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd[0], *cmd[1:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
            log.error(
                f"{label} audio extraction failed", error=stderr.decode("utf-8", errors="replace")[-500:]
            )
            return False
        partial.replace(output)
        log.info(f"{label} audio extracted", path=str(output), size_bytes=output.stat().st_size)
        return True
    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        raise
    except Exception as exc:
        log.error(f"Failed to extract {label} audio", error=str(exc))
        return False
    finally:
        partial.unlink(missing_ok=True)


async def extract_analysis_audio(
    video_path: Path, output_wav_path: Path, sample_rate: int = 16000, channels: int = 1
) -> bool:
    """Extract normalized PCM suitable for speech recognition."""
    return await _extract(
        video_path,
        output_wav_path,
        ["-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", str(channels)],
        "Analysis",
    )


async def extract_original_audio(video_path: Path, output_wav_path: Path) -> bool:
    """Decode audio to PCM without changing its native sample rate or channel count."""
    return await _extract(video_path, output_wav_path, ["-c:a", "pcm_s16le"], "Original")


async def extract_audio_window(source: Path, output: Path, start: float, duration: float) -> bool:
    return await _extract(
        source,
        output,
        ["-t", str(duration), "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1"],
        "Bounded analysis window",
        input_options=["-ss", str(start)],
    )


def get_audio_duration_seconds(audio_path: Path) -> float | None:
    if not audio_path.exists():
        return None
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return float(out.decode().strip())
    except Exception:
        try:
            with wave.open(str(audio_path), "rb") as wf:
                return wf.getnframes() / float(wf.getframerate())
        except Exception:
            return None
