from __future__ import annotations

import json

import pytest

from xsa.analysis_platform import build_space_analysis, count_transcript_phrase
from xsa.diarization import run_offline_diarization
from xsa.paths import SpacePaths
from xsa.speech_analytics import load_verified_timeline


@pytest.fixture
def analyzed(tmp_path, monkeypatch):
    monkeypatch.setattr("xsa.db.database._SessionLocal", None)
    monkeypatch.setattr("xsa.analysis_platform._database_evidence", lambda _sid: ([], []))
    paths = SpacePaths(root=tmp_path)
    (tmp_path / "audio").mkdir()
    (tmp_path / "transcript").mkdir()
    audio = tmp_path / "audio" / "analysis.wav"
    audio.write_bytes(b"test acoustic source")
    segments = [
        {"id": i, "start": i * 2, "end": i * 2 + 1, "text": f"hello Hello phrase {i}"} for i in range(4)
    ]
    paths.transcript_segments.write_text(json.dumps(segments), encoding="utf-8")
    paths.transcript_json.write_text(json.dumps({"segments": segments}), encoding="utf-8")

    class Diarizer:
        model_id = "test-acoustic-model"

        def diarize(self, _audio):
            return [
                {"start": 0, "end": 5.5, "speaker": "voice_a"},
                {"start": 6, "end": 7, "speaker": "voice_b"},
            ]

    run_offline_diarization(
        space_id="test123",
        audio_path=audio,
        transcript_path=paths.transcript_segments,
        analysis_dir=paths.analysis_dir,
        diarizer=Diarizer(),
        duration=8,
    )
    return paths


def test_analysis_uses_verified_timeline_and_keeps_raw_transcript(analyzed):
    before = analyzed.transcript_json.read_bytes()
    report = build_space_analysis(
        analyzed, space={"space_id": "test123"}, inventory_record={"media": {"duration_seconds": 8}}
    )
    acoustic = report["speakers"]["acoustic_diarization"]
    assert acoustic["status"] == "DONE" and acoustic["speaker_count"] == 2
    assert acoustic["speakers"][0]["total_speaking_seconds"] == 5.5
    assert acoustic["speakers"][0]["text_summary"]["status"] == "available"
    assert acoustic["speakers"][0]["username"] is None
    assert report["transcript_segments"][0]["speaker"] == "SPEAKER_00"
    assert analyzed.transcript_json.read_bytes() == before
    assert report["processing"]["stages"]["SEMANTIC_ANALYSIS"] == "NOT_STARTED"


def test_changed_transcript_rejects_stale_speaker_identities(analyzed):
    analyzed.transcript_segments.write_text("[]", encoding="utf-8")
    timeline, status = load_verified_timeline(
        analyzed.analysis_dir,
        analyzed.root / "audio" / "analysis.wav",
        analyzed.transcript_segments,
        "test123",
    )
    assert timeline is None and status == "STALE_INPUTS"
    report = build_space_analysis(analyzed, space={"space_id": "test123"})
    assert report["speakers"]["acoustic_diarization"]["speaker_count"] == 0


def test_phrase_count_includes_repetitions_and_reports_truncation(analyzed):
    result = count_transcript_phrase(analyzed, "hello", limit=2)
    assert result["occurrence_count"] == 8
    assert result["matching_segment_count"] == 4
    assert result["truncated"] is True
    assert [row["start"] for row in result["results"]] == [0, 2]
    assert result["results"][0]["occurrences"] == 2
