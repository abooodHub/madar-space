"""Transcription & alignment pipeline worker (Phase 4 & 5).

Orchestrates audio extraction, Whisper transcription, speaker alignment,
and database persistence for completed Space recordings.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime

from xsa.blocking import run_blocking
from xsa.config import Settings, get_settings
from xsa.db import Space, TranscriptSegment, TranscriptWord, get_session
from xsa.logging_setup import get_logger
from xsa.paths import SpacePaths
from xsa.recorder.media import get_recording_source
from xsa.transcriber.aligner import (
    align_segments_with_speakers,
    build_speaker_intervals,
    export_to_json,
    export_to_srt,
    export_to_vtt,
)
from xsa.transcriber.audio import extract_analysis_audio, extract_original_audio
from xsa.transcriber.engine import WhisperTranscriber

log = get_logger("WORKER")


async def process_space_transcription(
    space_id: str,
    cfg: Settings | None = None,
    language: str | None = None,
    progress_callback=None,
) -> dict:
    """
    Run full transcription and speaker alignment pipeline for a given space_id.
    """
    cfg = cfg or get_settings()

    # Ensure DB is ready
    from xsa.db import init_db

    try:
        from xsa.db.database import _SessionLocal

        if _SessionLocal is None:
            init_db(cfg.db_path)
    except Exception:
        init_db(cfg.db_path)

    # Locate space paths
    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        started_at = (
            space.started_at if space and space.started_at else datetime.now(UTC).replace(tzinfo=None)
        )

    paths = SpacePaths.for_space(cfg.data_dir, space_id, started_at)

    # 1. Locate recording file
    rec_file = await run_blocking(get_recording_source, paths)
    if rec_file is None:
        raise FileNotFoundError(f"No complete audio/video recording found in {paths.root}")

    log.info("Processing transcription pipeline", space_id=space_id, file=str(rec_file))

    # 2. Preserve decoded source audio and create a separate recognition copy.
    original_is_current = (
        paths.audio_original.exists()
        and paths.audio_original.stat().st_size > 0
        and paths.audio_original.stat().st_mtime >= rec_file.stat().st_mtime
    )
    if not original_is_current and not await extract_original_audio(rec_file, paths.audio_original):
        log.warning("Could not create preserved source-audio WAV")

    wav_path = paths.audio_analysis
    wav_is_current = (
        wav_path.exists()
        and wav_path.stat().st_size > 0
        and wav_path.stat().st_mtime >= rec_file.stat().st_mtime
    )
    if not wav_is_current:
        extracted = await extract_analysis_audio(rec_file, wav_path)
        if not extracted:
            log.warning("Audio extraction failed, checking if rec_file can be directly transcribed")
            wav_path = rec_file

    # 3. Transcribe with Whisper
    transcriber = WhisperTranscriber(cfg)
    result = await transcriber.transcribe_async(
        wav_path,
        language=language,
        checkpoint_root=paths.root / "transcript" / "checkpoints",
        source_path=rec_file,
        progress_callback=progress_callback,
    )
    log.info("Transcription completed", segments_count=len(result.segments), lang=result.language)

    # 4. Load speaker events and align speakers (Phase 5)
    events: list[dict] = []
    if paths.speaker_events_live.exists():
        try:
            with paths.speaker_events_live.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except Exception as e:
            log.warning("Failed to parse speaker_events_live.jsonl", error=str(e))

    intervals = build_speaker_intervals(events)
    aligned_segments = align_segments_with_speakers(result.segments, intervals)
    result.segments = aligned_segments

    # 5. Keep raw recognition text intact; normalization is a separate artifact.
    raw_data = export_to_json(result)
    normalized_result = type(result)(
        language=result.language,
        duration=result.duration,
        segments=[
            type(seg)(
                id=seg.id,
                start=seg.start,
                end=seg.end,
                text=re.sub(r"\s+", " ", unicodedata.normalize("NFC", seg.text)).strip(),
                words=seg.words,
                speaker=seg.speaker,
                language=seg.language,
                confidence=seg.confidence,
                no_speech_prob=seg.no_speech_prob,
            )
            for seg in aligned_segments
        ],
        text=" ".join(
            re.sub(r"\s+", " ", unicodedata.normalize("NFC", seg.text)).strip() for seg in aligned_segments
        ).strip(),
    )
    json_data = export_to_json(normalized_result)
    from xsa.transcriber.generation import publish_compatibility, stage_generation

    generation, complete = stage_generation(
        paths.root,
        {
            "raw.json": json.dumps(raw_data, indent=2, ensure_ascii=False),
            "normalized.json": json.dumps(json_data, indent=2, ensure_ascii=False),
            "words.json": json.dumps(
                [word for segment in json_data["segments"] for word in segment["words"]], ensure_ascii=False
            ),
            "segments.json": json.dumps(json_data["segments"], ensure_ascii=False),
            "transcript.txt": json_data["full_text"] + "\n",
            "transcript.json": json.dumps(dict(json_data, generation_id=None), ensure_ascii=False),
            "transcript.srt": export_to_srt(aligned_segments),
            "transcript.vtt": export_to_vtt(aligned_segments),
        },
    )

    # 6. Persist to Database
    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if space:
            # Clear old segments if any exist for retry
            # Delete dependent words first so reprocessing is safe with FK checks enabled.
            db.query(TranscriptWord).filter_by(space_id=space.id).delete(synchronize_session=False)
            db.query(TranscriptSegment).filter_by(space_id=space.id).delete(synchronize_session=False)

            for seg in aligned_segments:
                normalized_seg = next((item for item in normalized_result.segments if item.id == seg.id), seg)
                db_seg = TranscriptSegment(
                    space_id=space.id,
                    segment_index=seg.id,
                    start_ms=int(seg.start * 1000),
                    end_ms=int(seg.end * 1000),
                    text=normalized_seg.text,
                    text_raw=next((raw.text for raw in result.segments if raw.id == seg.id), seg.text),
                    language=seg.language or result.language,
                    confidence=seg.confidence,
                    no_speech_prob=seg.no_speech_prob,
                    speaker_label=seg.speaker,
                )
                db.add(db_seg)
                db.flush()

                # Add word-level timings
                for w in seg.words:
                    db_word = TranscriptWord(
                        segment_id=db_seg.id,
                        space_id=space.id,
                        start_ms=int(w.start * 1000),
                        end_ms=int(w.end * 1000),
                        word=w.word,
                        probability=w.probability,
                    )
                    db.add(db_word)

            space.transcript_generation_id = generation
            space.state = "transcribed"
            db.commit()

    publish_compatibility(paths.root, complete)

    return {
        "generation_id": generation,
        "space_id": space_id,
        "segments": len(aligned_segments),
        "duration": result.duration,
        "language": result.language,
        "srt_path": str(paths.transcript_srt),
        "vtt_path": str(paths.transcript_vtt),
        "json_path": str(paths.transcript_json),
    }
