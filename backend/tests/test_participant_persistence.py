from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from xsa.db import Space, TranscriptSegment, TranscriptWord, get_session, init_db
from xsa.db.models import Participant, SpaceParticipant, SpeakerEvent
from xsa.db.participant_persistence import persist_space_participant
from xsa.paths import SpacePaths


def test_participant_and_space_relationship_upsert_is_idempotent(tmp_path: Path):
    init_db(tmp_path / "participants.sqlite3")
    observed_at = datetime(2026, 10, 8, 12, 0, 0)
    with get_session() as db:
        space = Space(space_id="persist-test", state="recording")
        db.add(space)
        db.commit()
        space_id = space.id

    with get_session() as db:
        first, participant_created, relationship_created, conflict = persist_space_participant(
            db,
            space_db_id=space_id,
            evidence={
                "username": "Example_User",
                "display_name": "Example",
                "avatar_url": "https://example.test/avatar.png",
                "role": "listener",
            },
            observed_at=observed_at,
        )
        assert first is not None
        assert (participant_created, relationship_created, conflict) == (True, True, False)
        first_id = first.id
        db.commit()

    with get_session() as db:
        second, participant_created, relationship_created, conflict = persist_space_participant(
            db,
            space_db_id=space_id,
            evidence={
                "username": "example_user",
                "display_name": "Example Updated",
                "role": "speaker",
                "is_muted": False,
            },
            observed_at=observed_at + timedelta(seconds=10),
        )
        assert second is not None
        assert second.id == first_id
        assert (participant_created, relationship_created, conflict) == (False, False, False)
        db.commit()

    with get_session() as db:
        assert db.query(Participant).count() == 1
        participant = db.query(Participant).one()
        relation = db.query(SpaceParticipant).one()
        assert participant.first_seen_at == observed_at
        assert participant.last_seen_at == observed_at + timedelta(seconds=10)
        assert participant.display_name == "Example Updated"
        assert relation.role == "speaker"
        assert relation.first_seen_at == observed_at
        assert relation.last_seen_at == observed_at + timedelta(seconds=10)
        assert relation.is_muted is False


def test_space_resolver_uses_sidecar_identity_and_does_not_rename(tmp_path: Path):
    folder = tmp_path / "spaces" / "legacy" / "unrelated-descriptive-title"
    folder.mkdir(parents=True)
    sidecar = folder / "space.json"
    sidecar.write_text(json.dumps({"metadata": {"space_id": "stableSpace42"}}), encoding="utf-8")

    resolved = SpacePaths.resolve_existing(tmp_path, "stableSpace42")

    assert resolved is not None
    assert resolved.root == folder
    assert sidecar.is_file()
    assert not (folder.parent / "stableSpace42").exists()


def test_backfill_dry_run_then_apply_is_idempotent(tmp_path: Path, monkeypatch):
    from scripts import reconcile_speaker_participants as backfill

    data_dir = tmp_path / "data"
    folder = data_dir / "spaces" / "old" / "renamed-folder"
    folder.mkdir(parents=True)
    (folder / "space.json").write_text(json.dumps({"space_id": "backfillSpace42"}), encoding="utf-8")
    observation = {
        "space_id": "backfillSpace42",
        "observed_at": "2026-10-08T12:00:00Z",
        "recording_time": 1.25,
        "participants": [
            {
                "username": "speaker_one",
                "display_name": "Speaker One",
                "avatar_url": "https://example.test/speaker.png",
                "role": "speaker",
                "active_speaking": None,
            }
        ],
    }
    event = {
        "space_id": "backfillSpace42",
        "recording_time": 1.25,
        "wall_time": "2026-10-08T12:00:00Z",
        "event": "became_speaker",
        "username": "speaker_one",
        "display_name": "Speaker One",
        "avatar_url": "https://example.test/speaker.png",
        "role": "speaker",
        "source": "x_gql",
        "confidence": 1.0,
    }
    observation_path = folder / "speaker_observations.jsonl"
    event_path = folder / "speaker_events.jsonl"
    observation_path.write_text(json.dumps(observation) + "\n", encoding="utf-8")
    event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    raw_hashes = (observation_path.read_bytes(), event_path.read_bytes())

    db_path = tmp_path / "backfill.sqlite3"
    monkeypatch.setattr(backfill, "get_settings", lambda: SimpleNamespace(db_path=db_path))
    init_db(db_path)
    with get_session() as db:
        space = Space(space_id="backfillSpace42", state="transcribed")
        db.add(space)
        db.flush()
        db.add(
            SpeakerEvent(
                space_id=space.id,
                recording_offset_ms=1250,
                wall_time=datetime(2026, 10, 8, 12, 0, 0),
                event_type="became_speaker",
                username="speaker_one",
                source="x_gql",
                raw_data={"role": "speaker"},
            )
        )
        db.commit()

    preview = backfill.reconcile("backfillSpace42", data_dir=data_dir, dry_run=True)
    assert preview["identities_found"] == 1
    assert preview["participants_to_create"] == 1
    assert preview["space_participant_relationships_to_create"] == 1
    assert preview["speaker_events_to_create"] == 0
    assert preview["speaker_events_to_link"] == 1
    with get_session() as db:
        assert db.query(Participant).count() == 0
        assert db.query(SpaceParticipant).count() == 0

    applied = backfill.reconcile("backfillSpace42", data_dir=data_dir, dry_run=False)
    assert applied["participants_to_create"] == 1
    assert applied["space_participant_relationships_to_create"] == 1
    assert applied["speaker_events_to_create"] == 0
    assert applied["speaker_events_to_link"] == 1

    repeated = backfill.reconcile("backfillSpace42", data_dir=data_dir, dry_run=False)
    assert repeated["participants_to_create"] == 0
    assert repeated["space_participant_relationships_to_create"] == 0
    assert repeated["speaker_events_to_create"] == 0
    assert repeated["speaker_events_to_link"] == 0
    with get_session() as db:
        assert db.query(Participant).count() == 1
        assert db.query(SpaceParticipant).count() == 1
        assert db.query(SpeakerEvent).count() == 1
    assert (observation_path.read_bytes(), event_path.read_bytes()) == raw_hashes


def test_backfill_counts_malformed_lines_without_mutating_raw_evidence(tmp_path: Path, monkeypatch):
    from scripts import reconcile_speaker_participants as backfill

    data_dir = tmp_path / "data"
    folder = data_dir / "spaces" / "renamed"
    folder.mkdir(parents=True)
    (folder / "space.json").write_text(json.dumps({"space_id": "malformedSpace"}), encoding="utf-8")
    obs_path = folder / "speaker_observations.jsonl"
    evt_path = folder / "speaker_events.jsonl"
    obs_path.write_text(
        "not json\n" + json.dumps({"space_id": "malformedSpace", "participants": []}), encoding="utf-8"
    )
    evt_path.write_text("[]\n", encoding="utf-8")
    before = obs_path.read_bytes(), evt_path.read_bytes()

    db_path = tmp_path / "malformed.sqlite3"
    monkeypatch.setattr(backfill, "get_settings", lambda: SimpleNamespace(db_path=db_path))
    init_db(db_path)
    with get_session() as db:
        db.add(Space(space_id="malformedSpace", state="ended"))
        db.commit()

    result = backfill.reconcile("malformedSpace", data_dir=data_dir, dry_run=True)

    assert result["malformed_records"] == 2
    assert result["identities_found"] == 0
    assert (obs_path.read_bytes(), evt_path.read_bytes()) == before


def test_transcript_artifact_recovery_uses_db_without_media_or_whisper(tmp_path: Path, monkeypatch):
    from scripts import reconcile_space_artifacts as artifacts

    db_path = tmp_path / "artifacts.sqlite3"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        artifacts, "get_settings", lambda: SimpleNamespace(db_path=db_path, data_dir=data_dir)
    )
    init_db(db_path)
    with get_session() as db:
        space = Space(
            space_id="artifactSpace42",
            state="transcribed",
            detected_at=datetime(2026, 10, 3, 23, 36, 27),
        )
        db.add(space)
        db.flush()
        segment = TranscriptSegment(
            space_id=space.id,
            start_ms=1200,
            end_ms=2400,
            segment_index=0,
            text="existing words",
            text_raw="existing words",
            language="en",
        )
        db.add(segment)
        db.flush()
        db.add(
            TranscriptWord(
                segment_id=segment.id,
                space_id=space.id,
                start_ms=1200,
                end_ms=1800,
                word="existing",
                probability=0.9,
            )
        )
        db.commit()

    preview = artifacts.reconcile("artifactSpace42", data_dir=data_dir, dry_run=True)
    assert preview["transcript_db_segments"] == 1
    assert preview["transcript_db_words"] == 1
    assert preview["source_mkv_found"] is False
    assert preview["analysis_audio_found"] is False
    assert preview["whisper_rerun_required"] is False
    assert not Path(preview["resolved_path"]).exists()

    result = artifacts.reconcile("artifactSpace42", data_dir=data_dir, dry_run=False)
    resolved = Path(result["resolved_path"])
    restored = json.loads((resolved / "transcript" / "segments.json").read_text(encoding="utf-8"))
    transcript = json.loads((resolved / "transcript.json").read_text(encoding="utf-8"))
    assert len(restored) == 1
    assert restored[0]["words"][0]["word"] == "existing"
    assert transcript["duration_seconds"] is None
    assert not (resolved / "audio" / "analysis.wav").exists()
