from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from xsa.diarization import (
    ActiveRecordingError,
    GuardedPyannoteDiarizer,
    PyannoteDiarizer,
    assert_safe_to_diarize,
    build_offline_timeline,
    normalize_intervals,
    recording_processes,
    run_offline_diarization,
)


def test_interval_normalization_clips_merges_and_orders_speaker_ids_by_first_occurrence():
    intervals = normalize_intervals(
        [
            {"start": -1, "end": 2, "speaker": "late_label"},
            {"start": 1.5, "end": 3, "speaker": "late_label"},
            {"start": 4, "end": 6, "speaker": "early_label"},
            {"start": 12, "end": 13, "speaker": "outside"},
            {"start": float("nan"), "end": 9, "speaker": "invalid_nan"},
        ],
        duration=10,
    )
    assert [(x["start"], x["end"], x["speaker_id"]) for x in intervals] == [
        (0.0, 3.0, "SPEAKER_00"),
        (4.0, 6.0, "SPEAKER_01"),
    ]
    assert all(x["username"] is None for x in intervals)


def test_adjacent_fragments_merge_only_below_threshold_and_keep_gap_as_silence():
    intervals = normalize_intervals(
        [
            {"start": 0, "end": 1, "speaker": "a"},
            {"start": 1.05, "end": 2, "speaker": "a"},
            {"start": 2.3, "end": 3, "speaker": "a"},
        ],
        duration=4,
        merge_gap_seconds=0.1,
    )
    assert len(intervals) == 2
    assert intervals[0]["bridged_gaps"] == [[1, 1.05]]
    assert intervals[0]["speech_seconds"] == 1.95
    assert intervals[1]["start"] == 2.3


def test_offline_timeline_preserves_overlap_and_leaves_silence_unassigned():
    timeline = build_offline_timeline(
        space_id="oldSpace",
        duration=10,
        raw_intervals=[
            {"start": 0, "end": 5, "speaker": "a"},
            {"start": 4, "end": 8, "speaker": "b"},
        ],
        transcript_segments=[
            {"id": 1, "start": 0.5, "end": 2, "text": "speaker A"},
            {"id": 2, "start": 4.2, "end": 4.8, "text": "overlap"},
        ],
    )
    assert len(timeline["speakers"]) == 2
    assert timeline["coverage"]["speech_seconds_unique"] == 8
    assert timeline["coverage"]["speech_seconds_summed_by_speaker"] == 9
    assert timeline["coverage"]["overlap_seconds"] == 1
    assert timeline["coverage"]["silence_or_unassigned_seconds"] == 2
    assert timeline["transcript_associations"][0]["speaker_id"] == "SPEAKER_00"
    assert timeline["transcript_associations"][1]["speaker_id"] is None
    assert timeline["transcript_associations"][1]["reason"] == "overlapping_speakers"
    assert all(interval["username"] is None for interval in timeline["intervals"])


def test_unknown_identity_and_voice_similarity_are_not_promoted_to_username():
    timeline = build_offline_timeline(
        space_id="space",
        duration=4,
        raw_intervals=[{"start": 0, "end": 2, "speaker": "s0"}],
        transcript_segments=[{"id": 0, "start": 0, "end": 1, "text": "hello"}],
        identity_mappings={
            "SPEAKER_00": {
                "candidate_username": "somebody",
                "method": "inferred",
                "evidence": "voice_similarity_only",
                "confidence": 0.99,
            }
        },
    )
    assert timeline["intervals"][0]["username"] is None
    assert timeline["intervals"][0]["identity_mapping"]["verified"] is False
    assert timeline["transcript_associations"][0]["username"] is None


def test_observed_mapping_reuses_diarization_and_rebuilds_only_identity_timeline(tmp_path):
    class FakeDiarizer:
        model_id = "test-model"

        def __init__(self):
            self.calls = 0

        def diarize(self, audio_path):
            self.calls += 1
            return [{"start": 0, "end": 2, "speaker": "voice"}]

    audio = tmp_path / "analysis.wav"
    transcript = tmp_path / "segments.json"
    audio.write_bytes(b"audio-fixture")
    transcript.write_text(json.dumps([{"id": 4, "start": 0, "end": 1.5, "text": "hello"}]), encoding="utf-8")
    engine = FakeDiarizer()
    kwargs = dict(
        space_id="space",
        audio_path=audio,
        transcript_path=transcript,
        analysis_dir=tmp_path / "analysis",
        diarizer=engine,
        duration=3,
        active_space_ids=lambda: (),
        process_rows=lambda: (),
    )
    first = run_offline_diarization(**kwargs)
    assert (tmp_path / "analysis" / "diarization_raw.json").is_file()
    assert first["timeline"]["transcript_associations"][0]["speaker_id"] == "SPEAKER_00"
    assert first["timeline"]["speakers"][0]["username"] is None
    mapping = {
        "username": "verified_user",
        "method": "observed",
        "evidence": "validated X event",
        "diarization_fingerprint": first["timeline"]["source"]["diarization_fingerprint"],
    }
    second = run_offline_diarization(
        **kwargs,
        identity_mappings={
            "SPEAKER_00": {
                **mapping,
            }
        },
    )
    assert engine.calls == 1
    assert second["timeline"]["speakers"][0]["username"] == "verified_user"
    assert second["timeline"]["transcript_associations"][0]["username"] == "verified_user"
    third = run_offline_diarization(
        **kwargs,
        identity_mappings={
            "SPEAKER_00": {
                **mapping,
            }
        },
    )
    assert third["status"] == "skipped"
    assert engine.calls == 1
    assert transcript.read_text(encoding="utf-8").find("speaker") == -1


def test_recording_guard_detects_active_space_and_ffmpeg_capture():
    with pytest.raises(ActiveRecordingError):
        assert_safe_to_diarize(active_space_ids=["space"], process_rows=[])
    rows = [("44", "ffmpeg -i screen -f matroska recording_000.mkv")]
    assert recording_processes(rows) == ["44"]
    with pytest.raises(ActiveRecordingError):
        assert_safe_to_diarize(process_rows=rows)
    assert_safe_to_diarize(process_rows=[("10", "ffmpeg -i source.wav output.wav")])


def test_pyannote_adapter_is_lazy_and_selects_cpu_when_cuda_is_unavailable(tmp_path, monkeypatch):
    class Annotation:
        def itertracks(self, *, yield_label):
            assert yield_label
            return iter([((types.SimpleNamespace(start=1.0, end=2.5), None, "speaker_0"))])

    class Pipeline:
        received_token = None

        @classmethod
        def from_pretrained(cls, model_id, token=None):
            cls.received_token = token
            return cls()

        def to(self, device):
            self.device = str(device)

        def __call__(self, audio_path):
            return types.SimpleNamespace(speaker_diarization=Annotation())

    pyannote = types.ModuleType("pyannote")
    pyannote.__path__ = []
    audio_mod = types.ModuleType("pyannote.audio")
    audio_mod.Pipeline = Pipeline
    pyannote.audio = audio_mod
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.device = lambda device: device
    monkeypatch.setitem(sys.modules, "pyannote", pyannote)
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio_mod)
    monkeypatch.setitem(sys.modules, "torch", torch)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"unused")
    adapter = PyannoteDiarizer(token="secret-test-token", device="auto")
    assert adapter._pipeline is None
    assert adapter.diarize(audio) == [{"start": 1.0, "end": 2.5, "speaker": "speaker_0"}]
    assert adapter.actual_device == "cpu"
    assert Pipeline.received_token == "secret-test-token"


def test_guarded_worker_is_terminated_if_recording_starts_mid_run(monkeypatch):
    class Receiver:
        def poll(self, timeout):
            return False

        def close(self):
            pass

    class Sender:
        def close(self):
            pass

    class Worker:
        def __init__(self, **kwargs):
            self.alive = False
            self.terminated = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            pass

        def terminate(self):
            self.terminated = True
            self.alive = False

        def kill(self):
            self.alive = False

    class Context:
        def Pipe(self, duplex=False):
            return Receiver(), Sender()

        def Process(self, **kwargs):
            self.worker = Worker(**kwargs)
            return self.worker

    context = Context()
    monkeypatch.setattr("xsa.diarization.multiprocessing.get_context", lambda _: context)
    checks = iter([[], ["new-live-space"]])
    guard = GuardedPyannoteDiarizer(
        active_space_ids=lambda: next(checks), process_rows=lambda: (), poll_seconds=0
    )
    with pytest.raises(ActiveRecordingError):
        guard.diarize(Path("unused.wav"))
    assert context.worker.terminated is True


def test_identity_mapping_voice_similarity_is_explicitly_unverified():
    timeline = build_offline_timeline(
        space_id="space",
        duration=2,
        raw_intervals=[{"start": 0, "end": 1, "speaker": "s0"}],
        transcript_segments=[],
        identity_mappings={
            "SPEAKER_00": {"username": "candidate", "method": "inferred", "evidence": "voice_similarity_only"}
        },
    )
    mapping = timeline["intervals"][0]["identity_mapping"]
    assert mapping["verified"] is False
    assert timeline["intervals"][0]["username"] is None


def test_transcript_overlap_reports_duration_quality_and_ambiguity():
    timeline = build_offline_timeline(
        space_id="space",
        duration=5,
        raw_intervals=[
            {"start": 0, "end": 2, "speaker": "a"},
            {"start": 1, "end": 3, "speaker": "b"},
        ],
        transcript_segments=[{"id": 1, "start": 0.5, "end": 2.5, "text": "two voices"}],
    )
    association = timeline["transcript_associations"][0]
    assert association["speaker_id"] is None
    assert association["ambiguous"] is True
    assert association["quality"] == "ambiguous"
    assert association["overlap_by_speaker"][0]["overlap_seconds"] == 1.5
    assert timeline["quality_metrics"]["ground_truth_der_measured"] is False


def test_stale_manual_identity_mapping_is_explicitly_blocked():
    timeline = build_offline_timeline(
        space_id="space",
        duration=2,
        raw_intervals=[{"start": 0, "end": 1, "speaker": "s0"}],
        transcript_segments=[],
        identity_mappings={
            "SPEAKER_00": {
                "username": "user",
                "method": "manual",
                "evidence": "operator mapping",
                "diarization_fingerprint": "old-fingerprint",
            }
        },
        metadata={"diarization_fingerprint": "new-fingerprint"},
    )
    mapping = timeline["intervals"][0]["identity_mapping"]
    assert timeline["intervals"][0]["username"] is None
    assert mapping["blocked_reason"] == "mapping_belongs_to_different_diarization_fingerprint"


def test_word_association_is_derived_and_force_rerun_archives_prior_raw(tmp_path):
    class FakeDiarizer:
        model_id = "test-model"

        def __init__(self):
            self.calls = 0

        def diarize(self, _audio):
            self.calls += 1
            return [{"start": 0, "end": 2, "speaker": "speaker"}]

    audio = tmp_path / "audio.wav"
    transcript = tmp_path / "segments.json"
    analysis = tmp_path / "analysis"
    audio.write_bytes(b"audio-data")
    transcript.write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "start": 0,
                    "end": 2,
                    "text": "hello world",
                    "words": [{"start": 0.1, "end": 0.4, "word": "hello"}],
                }
            ]
        ),
        encoding="utf-8",
    )
    engine = FakeDiarizer()
    kwargs = dict(
        space_id="space",
        audio_path=audio,
        transcript_path=transcript,
        analysis_dir=analysis,
        diarizer=engine,
        duration=2,
    )
    first = run_offline_diarization(**kwargs)
    assert first["timeline"]["word_associations"][0]["speaker_id"] == "SPEAKER_00"
    second = run_offline_diarization(**kwargs, force_model=True)
    assert second["status"] == "completed"
    assert engine.calls == 2
    assert list((analysis / "diarization_runs").glob("*.json"))
