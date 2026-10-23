from __future__ import annotations

import json

from xsa.analysis_platform import (
    _mentions,
    _questions,
    _speech_timing_metrics,
    _word_regions,
    media_inventory_record,
    search_space_analysis,
)
from xsa.paths import SpacePaths


def test_finalized_media_evidence_populates_report_health(tmp_path, monkeypatch):
    paths = SpacePaths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.segment(0).write_bytes(b"source kept intact")
    monkeypatch.setattr(
        "xsa.inventory.probe_media",
        lambda path: {
            "valid": True,
            "audio_valid": True,
            "video_valid": True,
            "duration_seconds": 42.5,
            "path": str(path),
        },
    )
    result = media_inventory_record(paths)
    assert result["recording_health"]["overall_status"] == "GOOD"
    assert result["recording_health"]["audio_valid"] is True
    assert result["recording_health"]["video_valid"] is True
    assert result["media"]["duration_seconds"] == 42.5
    assert paths.segment(0).read_bytes() == b"source kept intact"


def test_word_regions_use_word_times_and_keep_speaker_unknown():
    regions = _word_regions(
        [
            {
                "id": 4,
                "speaker": None,
                "words": [
                    {"word": "مرحبا", "start": 3.0, "end": 3.4},
                    {"word": "بكم", "start": 3.8, "end": 4.1},
                    {"word": "بعيد", "start": 7.0, "end": 7.3},
                ],
            },
        ]
    )
    assert len(regions) == 2
    assert regions[0]["start"] == 3.0
    assert regions[0]["end"] == 4.1
    assert regions[0]["text"] == "مرحبا بكم"
    assert regions[0]["speaker_id"] is None
    assert regions[0]["transcript_segment_ids"] == [4]


def test_question_and_handle_extractors_keep_source_segment_ids():
    segments = [{"id": 9, "start": 12.0, "end": 13.0, "speaker": None, "text": "@SomeUser هل وصلت؟"}]
    questions = _questions(segments)
    mentions = _mentions(segments)
    assert questions[0]["transcript_segment_ids"] == [9]
    assert mentions[0]["entity"] == "@SomeUser"
    assert mentions[0]["timestamps"] == [12.0]


def test_space_search_uses_generated_timeline(tmp_path):
    paths = SpacePaths(tmp_path)
    paths.analysis_dir.mkdir(parents=True)
    (paths.analysis_dir / "timeline.json").write_text(
        json.dumps(
            {
                "items": [
                    {"start": 8.0, "text": "نقاش حول التسجيل", "speaker_id": None},
                    {"start": 20.0, "text": "موضوع آخر", "speaker_id": None},
                ]
            }
        ),
        encoding="utf-8",
    )
    found = search_space_analysis(paths, "التسجيل")
    assert len(found) == 1
    assert found[0]["start"] == 8.0


def test_speech_time_ratio_is_not_reported_as_transcript_coverage():
    metrics = _speech_timing_metrics([{"start": 10.0, "end": 20.0}], 100.0)
    assert metrics["speech_time_seconds"] == 10.0
    assert metrics["speech_time_ratio_percent"] == 10.0
    assert metrics["transcript_coverage_percent"] is None
    assert metrics["transcript_coverage_status"] == "UNKNOWN_WITHOUT_REFERENCE_TRANSCRIPT"
