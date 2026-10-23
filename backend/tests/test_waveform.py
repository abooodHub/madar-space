from __future__ import annotations

import struct
import wave

from xsa.waveform import read_waveform


def test_waveform_uses_pcm_samples_and_caches_without_changing_source(tmp_path):
    audio = tmp_path / "analysis.wav"
    cache = tmp_path / "analysis" / "waveform.json"
    with wave.open(str(audio), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(struct.pack("<" + "h" * 8000, *([0] * 4000 + [16384] * 4000)))
    original = audio.read_bytes()
    first = read_waveform(audio, cache, bins=64)
    assert first["source_kind"] == "mixed_recording_audio"
    assert first["duration_seconds"] == 1
    assert len(first["peaks"]) == 64
    assert max(first["peaks"][:32]) == 0
    assert min(first["peaks"][32:]) > 0.49
    assert cache.is_file() and audio.read_bytes() == original
    assert read_waveform(audio, cache, bins=64) == first
