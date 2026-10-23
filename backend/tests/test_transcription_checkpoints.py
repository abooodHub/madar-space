"""Interrupted recognition resumes only verified windows from the same source."""

from __future__ import annotations

import json

import pytest

from xsa.transcriber.checkpoints import WindowCheckpoints, read_partial_transcript, source_identity
from xsa.transcriber.engine import SegmentItem, TranscriptionResult, WhisperTranscriber, WordItem


@pytest.mark.asyncio
async def test_failed_window_resumes_without_repeating_saved_speech(tmp_path, monkeypatch):
    from xsa.transcriber import audio

    source = tmp_path / "recording.mkv"
    source.write_bytes(b"immutable source")
    analysis = tmp_path / "analysis.wav"
    analysis.write_bytes(b"audio")
    monkeypatch.setattr(audio, "get_audio_duration_seconds", lambda _path: 1901.0)
    current = {"offset": 0, "fail": True}
    extracted = []

    async def extract(_source, output, offset, _length):
        current["offset"] = offset
        extracted.append(offset)
        output.write_bytes(b"window")
        return True

    monkeypatch.setattr(audio, "extract_audio_window", extract)
    engine = WhisperTranscriber()

    def transcribe(_path, _language):
        if current["offset"] == 600 and current["fail"]:
            raise RuntimeError("window failed")
        return TranscriptionResult(
            "ar", 600, [SegmentItem(0, 1, 2, f"word-{current['offset']}", [WordItem("word", 1, 2)])], "word"
        )

    engine.transcribe = transcribe
    identity = source_identity(source, model="large-v3", language="ar", compute_type="int8")
    root = tmp_path / "checkpoints"
    with pytest.raises(RuntimeError, match="window failed"):
        await engine._transcribe_windows_async(analysis, "ar", root, identity)
    store = WindowCheckpoints(root, identity, 4)
    assert store.completed_count() == 1
    current["fail"] = False
    result = await engine._transcribe_windows_async(analysis, "ar", root, identity)
    assert extracted == [0, 600, 600, 1200, 1800]
    assert [item.text for item in result.segments] == ["word-0", "word-600", "word-1200", "word-1800"]
    assert [item.start for item in result.segments] == [1, 601, 1201, 1801]
    assert [item.id for item in result.segments] == [0, 1, 2, 3]
    assert store.completed_count() == 4
    assert source.read_bytes() == b"immutable source"


def test_checkpoint_generation_changes_with_source_and_model(tmp_path):
    source = tmp_path / "recording.mkv"
    source.write_bytes(b"first")
    first = source_identity(source, model="large-v3", language="ar", compute_type="int8")
    store = WindowCheckpoints(tmp_path, first, 1)
    store.save(0, 0, 600, TranscriptionResult("ar", 600, [], ""))
    assert store.load(0, 0, 600) is not None
    source.write_bytes(b"other")
    changed = source_identity(source, model="large-v3", language="ar", compute_type="int8")
    assert WindowCheckpoints(tmp_path, changed, 1).load(0, 0, 600) is None
    assert WindowCheckpoints(tmp_path, dict(first, model="small"), 1).load(0, 0, 600) is None
    store._path(0).write_text("{broken", encoding="utf-8")
    assert store.load(0, 0, 600) is None
    assert store.completed_count() == 0


def test_progress_excludes_checkpoint_with_invalid_segments(tmp_path):
    source = tmp_path / "recording.mkv"
    source.write_bytes(b"source")
    identity = source_identity(source, model="large-v3", language="ar", compute_type="int8")
    store = WindowCheckpoints(tmp_path / "checkpoints", identity, 1)
    store.save(0, 0, 600, TranscriptionResult("ar", 600, [], ""))
    assert store.completed_count() == 1
    payload = json.loads(store._path(0).read_text(encoding="utf-8"))
    payload["segments"] = [{"start": 0, "end": 900, "text": "invalid", "id": 1}]
    store._path(0).write_text(json.dumps(payload), encoding="utf-8")
    assert store.completed_count() == 0
    assert read_partial_transcript(tmp_path)["recognition_state"] == "NOT_STARTED"


def test_partial_text_is_source_verified_and_never_marked_final(tmp_path):
    source = tmp_path / "recording.mkv"
    source.write_bytes(b"original recording")
    identity = source_identity(source, model="large-v3", language="ar", compute_type="int8")
    store = WindowCheckpoints(tmp_path / "transcript" / "checkpoints", identity, 2)
    result = TranscriptionResult("ar", 600, [SegmentItem(0, 4, 6, "مرحبا")], "مرحبا")
    store.save(0, 0, 600, result)
    partial = read_partial_transcript(tmp_path)
    assert partial["recognition_state"] == "PARTIAL"
    assert partial["completed_windows"] == 1
    assert partial["total_windows"] == 2
    assert partial["segments"][0]["start"] == 4
    source.write_bytes(b"replacement recording")
    assert read_partial_transcript(tmp_path)["recognition_state"] == "NOT_STARTED"


@pytest.mark.asyncio
async def test_changed_source_cannot_publish_recognition_result(tmp_path, monkeypatch):
    from xsa.transcriber import engine as engine_module

    source = tmp_path / "recording.mkv"
    source.write_bytes(b"first version")

    async def replaced_while_working(*_args):
        source.write_bytes(b"second version")
        return TranscriptionResult("ar", 1, [], "")

    monkeypatch.setattr(engine_module, "run_owned_process", replaced_while_working)
    transcriber = WhisperTranscriber()
    with pytest.raises(RuntimeError, match="source changed"):
        await transcriber.transcribe_async(
            source, checkpoint_root=tmp_path / "checkpoints", source_path=source
        )


def test_partial_transcript_route_reads_only_matching_space(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from importlib import import_module

    from fastapi import HTTPException

    from xsa.config import get_settings
    from xsa.db import Space, get_session, init_db
    from xsa.db import database as db_module
    from xsa.paths import SpacePaths

    cfg = get_settings().model_copy(
        update={"data_dir": tmp_path / "data", "db_path": tmp_path / "db.sqlite3"}
    )
    init_db(cfg.db_path)
    with get_session() as db:
        db.add(Space(space_id="checkpointSpace", state="processing"))
        db.commit()
    paths = SpacePaths.for_space(cfg.data_dir, "checkpointSpace", datetime.now(UTC))
    paths.root.mkdir(parents=True)
    source = paths.segment(0)
    source.write_bytes(b"recording")
    identity = source_identity(source, model="large-v3", language="ar", compute_type="int8")
    store = WindowCheckpoints(paths.root / "transcript" / "checkpoints", identity, 1)
    store.save(0, 0, 600, TranscriptionResult("ar", 600, [SegmentItem(0, 2, 3, "نص")], "نص"))
    api = import_module("xsa.api.app")
    monkeypatch.setattr(api, "_cfg", cfg)
    try:
        payload = api.get_partial_space_transcript("checkpointSpace")
        assert payload["recognition_state"] == "PARTIAL"
        assert payload["segments"][0]["text"] == "نص"
        with pytest.raises(HTTPException) as error:
            api.get_partial_space_transcript("nonexistentSpace")
        assert error.value.status_code == 404
    finally:
        if db_module._engine:
            db_module._engine.dispose()
        db_module._engine = None
        db_module._SessionLocal = None
