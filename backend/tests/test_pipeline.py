"""Unit tests for Phase 3, 4, 5, 6 & 7 modules.

Tests:
  - GraphQL data parsing (audioSpace -> SpaceParticipantInfo)
  - Speaker duration & turn accumulation
  - Speaker alignment with transcript segments
  - SRT and WebVTT generation
  - Speaker participation analytics and topic extraction
  - Dashboard route and API integration
"""

from __future__ import annotations

import json
from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from xsa.analyzer.summarizer import (
    calculate_speaker_participation,
    extract_algorithmic_topics,
    extract_questions_and_quotes,
)
from xsa.api.app import app
from xsa.transcriber.aligner import (
    SpeakerInterval,
    align_segments_with_speakers,
    build_speaker_intervals,
    export_to_srt,
    export_to_vtt,
    format_srt_timestamp,
    format_vtt_timestamp,
)
from xsa.transcriber.engine import SegmentItem


def test_post_recording_artifact_layout(tmp_path):
    from datetime import datetime

    from xsa.paths import SpacePaths

    paths = SpacePaths.for_space(tmp_path, "space123", datetime.now(UTC))
    paths.ensure()
    assert paths.audio_original == paths.root / "audio" / "original.wav"
    assert paths.audio_analysis == paths.root / "audio" / "analysis.wav"
    assert paths.transcript_raw == paths.root / "transcript" / "raw.json"
    assert paths.transcript_normalized == paths.root / "transcript" / "normalized.json"
    assert not paths.audio_dir.exists()
    assert not paths.transcript_dir.exists()


def test_timebase_conversions_use_explicit_anchors():
    from datetime import datetime, timedelta

    from xsa.timebase import Timebase

    origin = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    space_start = origin - timedelta(minutes=5)
    mapping = Timebase(origin, space_start, media_timestamp_zero_s=0.25)
    at = mapping.media_to_wall(10.0)
    assert at == origin + timedelta(seconds=9.75)
    assert mapping.wall_to_media(at) == pytest.approx(10.0)
    assert mapping.media_to_space(10.0) == pytest.approx(309.75)
    assert Timebase(origin).media_to_space(10.0) is None


def test_timebase_rejects_naive_wall_clock():
    from datetime import datetime

    from xsa.timebase import Timebase

    with pytest.raises(ValueError):
        Timebase(datetime(2026, 10, 6))


def test_missing_transcription_engine_is_a_failure_not_empty_success(tmp_path):
    from xsa.transcriber.engine import WhisperTranscriber

    transcriber = WhisperTranscriber()
    transcriber._engine_type = "mock"
    transcriber._load_model = lambda: None
    with pytest.raises(RuntimeError, match="No transcription engine"):
        transcriber.transcribe(tmp_path / "audio.wav")


def test_faster_whisper_filters_silence_and_repetition(tmp_path):
    from types import SimpleNamespace

    from xsa.transcriber.engine import WhisperTranscriber

    class Model:
        def transcribe(self, _audio, **kwargs):
            self.kwargs = kwargs
            return iter(()), SimpleNamespace(language="ar", duration=0.0)

    transcriber = WhisperTranscriber()
    model = Model()
    transcriber._model = model
    transcriber._engine_type = "faster-whisper"
    result = transcriber.transcribe(tmp_path / "audio.wav", language="ar")
    assert model.kwargs["vad_filter"] is True
    assert model.kwargs["condition_on_previous_text"] is False
    assert result.language == "ar"


def test_transcription_failure_keeps_recording_and_previous_transcript(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime

    from xsa.config import get_settings
    from xsa.db import Space, TranscriptSegment, TranscriptWord, get_session, init_db
    from xsa.paths import SpacePaths
    from xsa.transcriber import worker

    data_dir = tmp_path / "data"
    cfg = get_settings().model_copy(update={"data_dir": data_dir, "db_path": tmp_path / "db.sqlite3"})
    init_db(cfg.db_path)
    started = datetime(2026, 10, 1, tzinfo=UTC)
    with get_session() as db:
        space = Space(space_id="failure_safe_space", state="done", started_at=started.replace(tzinfo=None))
        db.add(space)
        db.flush()
        previous = TranscriptSegment(
            space_id=space.id,
            segment_index=0,
            start_ms=0,
            end_ms=1000,
            text="previous",
            text_raw="raw previous",
        )
        db.add(previous)
        db.flush()
        db.add(
            TranscriptWord(space_id=space.id, segment_id=previous.id, start_ms=0, end_ms=500, word="previous")
        )
        db.commit()
        space_pk = space.id

    paths = SpacePaths.for_space(data_dir, "failure_safe_space", started).ensure()
    source = paths.segment(0)
    source.write_bytes(b"untouched-recording-fixture")
    source_before = source.read_bytes()
    paths.transcript_dir.mkdir(parents=True, exist_ok=True)
    paths.transcript_raw.write_text('{"full_text":"previous raw"}', encoding="utf-8")

    def fake_source(_paths):
        return source

    async def fake_extract(_source, output, *args):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"extracted audio")
        return True

    async def fail_transcription(self, audio_path, language=None, **_kwargs):
        raise RuntimeError("simulated transcription failure")

    monkeypatch.setattr(worker, "get_recording_source", fake_source)
    monkeypatch.setattr(worker, "extract_original_audio", fake_extract)
    monkeypatch.setattr(worker, "extract_analysis_audio", fake_extract)
    monkeypatch.setattr(worker.WhisperTranscriber, "transcribe_async", fail_transcription)

    with pytest.raises(RuntimeError, match="simulated transcription failure"):
        asyncio.run(worker.process_space_transcription("failure_safe_space", cfg))

    assert source.read_bytes() == source_before
    assert paths.audio_original.read_bytes() == b"extracted audio"
    assert paths.audio_analysis.read_bytes() == b"extracted audio"
    assert json.loads(paths.transcript_raw.read_text(encoding="utf-8"))["full_text"] == "previous raw"
    with get_session() as db:
        assert db.query(TranscriptSegment).filter_by(space_id=space_pk).count() == 1
        assert db.query(TranscriptWord).filter_by(space_id=space_pk).count() == 1


# ---------------------------------------------------------------------------
# 1. Phase 3: Speaker Timeline & GraphQL
# ---------------------------------------------------------------------------


def test_build_speaker_intervals():
    events = [
        {"timestamp": 10.0, "event": "speaker_started", "username": "alice", "role": "host"},
        {"timestamp": 25.0, "event": "speaker_stopped", "username": "alice", "role": "host"},
        {"timestamp": 26.0, "event": "speaker_started", "username": "bob", "role": "speaker"},
        {"timestamp": 40.0, "event": "speaker_stopped", "username": "bob", "role": "speaker"},
    ]
    intervals = build_speaker_intervals(events)
    assert len(intervals) == 2
    assert intervals[0].username == "alice"
    assert intervals[0].start_s == 10.0
    assert intervals[0].end_s == 25.0
    assert intervals[1].username == "bob"
    assert intervals[1].start_s == 26.0
    assert intervals[1].end_s == 40.0


# ---------------------------------------------------------------------------
# 2. Phase 4 & 5: Transcription & Alignment
# ---------------------------------------------------------------------------


def test_align_segments_with_speakers():
    intervals = [
        SpeakerInterval(username="alice", display_name="Alice Host", role="host", start_s=0.0, end_s=15.0),
        SpeakerInterval(username="bob", display_name="Bob Speaker", role="speaker", start_s=15.5, end_s=30.0),
    ]

    segments = [
        SegmentItem(id=0, start=1.0, end=5.0, text="مرحباً بالجميع في هذه المساحة"),
        SegmentItem(id=1, start=16.0, end=22.0, text="أهلاً أليس، شكراً على الاستضافة"),
    ]

    aligned = align_segments_with_speakers(segments, intervals)
    assert aligned[0].speaker == "alice"
    assert aligned[1].speaker == "bob"


def test_srt_and_vtt_formatting():
    assert format_srt_timestamp(65.25) == "00:01:05,250"
    assert format_vtt_timestamp(65.25) == "00:01:05.250"

    segments = [
        SegmentItem(id=0, start=0.0, end=2.5, text="أهلاً بكم", speaker="alice"),
    ]
    srt = export_to_srt(segments)
    assert "00:00:00,000 --> 00:00:02,500" in srt
    assert "[@alice]: أهلاً بكم" in srt

    vtt = export_to_vtt(segments)
    assert "WEBVTT" in vtt
    assert "<v @alice>أهلاً بكم</v>" in vtt


# ---------------------------------------------------------------------------
# 3. Phase 6: AI Analysis & Speaker Analytics
# ---------------------------------------------------------------------------


def test_speaker_participation_calculation():
    segments = [
        {"speaker": "alice", "start": 0.0, "end": 10.0, "text": "intro"},
        {"speaker": "bob", "start": 10.0, "end": 20.0, "text": "talk 1"},
        {"speaker": "alice", "start": 20.0, "end": 30.0, "text": "response"},
    ]
    stats = calculate_speaker_participation(segments)
    assert "alice" in stats
    assert "bob" in stats
    assert stats["alice"]["total_seconds"] == 20.0
    assert stats["alice"]["turns_count"] == 2
    assert stats["bob"]["total_seconds"] == 10.0
    assert stats["alice"]["percentage"] == pytest.approx(66.7, abs=0.1)
    assert stats["bob"]["percentage"] == pytest.approx(33.3, abs=0.1)


def test_extract_questions_and_quotes():
    segments = [
        {"speaker": "alice", "start": 5.0, "text": "ما هي توقعاتكم للسوق؟"},
        {"speaker": "bob", "start": 12.0, "text": "أعتقد أن النمو مستمر."},
    ]
    questions = extract_questions_and_quotes(segments)
    assert len(questions) == 1
    assert questions[0]["speaker"] == "alice"
    assert "ما هي توقعاتكم للسوق؟" in questions[0]["question"]


def test_extract_algorithmic_topics():
    text = (
        "الذكاء الاصطناعي يحدث ثورة في التقنية والبرمجة. "
        "الذكاء الاصطناعي يساعد المطورين في تسريع الإنتاجية التقنية البرمجة."
    )
    topics = extract_algorithmic_topics(text)
    assert len(topics) > 0
    # Common high-frequency terms
    assert any(term in ["الذكاء", "الاصطناعي", "التقنية", "البرمجة"] for term in topics)


# ---------------------------------------------------------------------------
# 4. Phase 7: Dashboard Route
# ---------------------------------------------------------------------------


def test_dashboard_endpoint():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "XSpace Analyzer" in response.text
    assert 'id="systemHealthSection"' in response.text
    assert 'href="/workspace/manual"' in response.text

    ui_response = client.get("/ui")
    assert ui_response.status_code == 200
    assert ui_response.text == response.text


# ---------------------------------------------------------------------------
# 5. Multi-Space Slot Pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slot_pool_acquire_and_release(monkeypatch):
    from xsa.recorder.slot_pool import DisplaySlotPool

    async def fake_acquire(slot):
        slot.is_busy = True

    async def fake_release(slot):
        slot.is_busy = False

    monkeypatch.setattr("xsa.recorder.slot_pool.DisplaySlot.acquire", fake_acquire)
    monkeypatch.setattr("xsa.recorder.slot_pool.DisplaySlot.release", fake_release)
    pool = DisplaySlotPool(max_slots=2, base_display_num=200)

    # Acquire slot 0
    slot0 = await pool.acquire_slot()
    assert slot0 is not None
    assert slot0.slot_id == 0
    assert slot0.display == ":200"

    # Pool should be able to track slots
    assert len(pool._slots) == 2

    # Release slot 0
    await pool.release_slot(slot0)
    assert slot0.is_busy is False
