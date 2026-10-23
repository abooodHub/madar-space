"""Bounded, source-backed waveform peaks for the recording studio."""

from __future__ import annotations

import array
import json
import math
import os
import sys
import threading
import wave
from pathlib import Path

_LOCK = threading.Lock()


def read_waveform(audio: Path, cache: Path, *, bins: int = 1024) -> dict:
    """Return RMS buckets from a finalized PCM WAV, never inferred speaker audio."""
    if not 64 <= bins <= 2048:
        raise ValueError("Invalid waveform resolution")
    with _LOCK:
        source = audio.stat()
        identity = {"size": source.st_size, "mtime_ns": source.st_mtime_ns}
        if cache.is_file():
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                if saved.get("source") == identity and len(saved.get("peaks", [])) == bins:
                    return saved
            except (OSError, ValueError, TypeError):
                pass
        with wave.open(str(audio), "rb") as recording:
            channels = recording.getnchannels()
            sample_width = recording.getsampwidth()
            rate = recording.getframerate()
            frames = recording.getnframes()
            if channels not in (1, 2) or sample_width not in (1, 2, 4) or rate <= 0:
                raise ValueError("Unsupported PCM recording")
            peaks = []
            stride = max(1, rate // 1000) * channels
            for index in range(bins):
                end = (index + 1) * frames // bins
                start = index * frames // bins
                samples = recording.readframes(end - start)
                values = array.array({1: "B", 2: "h", 4: "i"}[sample_width])
                values.frombytes(samples)
                if sample_width > 1 and sys.byteorder != "little":
                    values.byteswap()
                left = values[::stride]
                right = values[1::stride] if channels == 2 else None
                offset = 128 if sample_width == 1 else 0
                if right is None:
                    power = sum((value - offset) ** 2 for value in left) / len(left) if left else 0
                else:
                    power = (
                        sum(((a + b) / 2 - offset) ** 2 for a, b in zip(left, right, strict=True)) / len(left)
                        if left
                        else 0
                    )
                rms = math.sqrt(power)
                peaks.append(round(min(1.0, rms / float(1 << (8 * sample_width - 1))), 5))
            result = {
                "source": identity,
                "source_kind": "mixed_recording_audio",
                "duration_seconds": round(frames / rate, 3),
                "peaks": peaks,
            }
        if audio.stat().st_size == source.st_size and audio.stat().st_mtime_ns == source.st_mtime_ns:
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
            temporary.replace(cache)
        return result
