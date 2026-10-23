from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from xsa.browser import SpaceParticipantInfo
from xsa.db import Space, SpaceParticipant, SpeakerEvent, get_session, init_db
from xsa.db.models import Participant
from xsa.paths import SpacePaths
from xsa.recorder.session import SpaceSession
from xsa.speaker_observation import SpeakerObservationTracker, append_jsonl, participant_payload


def person(role="speaker", active=None):
    return {
        "username": "sample_user",
        "display_name": "Sample",
        "avatar_url": None,
        "role": role,
        "active_speaking": active,
        "active_speaking_evidence": "verified_dom" if active is not None else None,
        "source": "x_dom",
        "x_user_id": None,
        "is_muted": False,
    }


def sample(tracker, at, recording_time, people, *, complete=False, active=False):
    return tracker.observe(
        people,
        observed_at=datetime(2026, 10, 6, tzinfo=UTC),
        recording_time=recording_time,
        source="x_dom",
        complete_snapshot=complete,
        active_indicator_validated=active,
    )


def test_observation_and_first_seen_are_separate_and_deduplicated():
    tracker = SpeakerObservationTracker("space", start_debounce_ms=0)
    observation, events = sample(tracker, 0, 2.5, [person()])
    assert observation["recording_time"] == 2.5
    assert observation["space_time"] is None
    assert observation["participants"][0]["active_speaking"] is None
    assert [e["event"] for e in events] == ["participant_seen"]
    _, events = sample(tracker, 1, 7.5, [person()])
    assert events == []


def test_role_changes_are_debounced_and_speaking_requires_validated_signal():
    tracker = SpeakerObservationTracker("space", start_debounce_ms=1000, stop_debounce_ms=1000)
    sample(tracker, 0, 0, [person("listener")])
    _, events = sample(tracker, 1, 0.5, [person("speaker")])
    assert events == []
    _, events = sample(tracker, 2, 1.5, [person("speaker")])
    assert [e["event"] for e in events] == ["became_speaker"]
    # Legacy is_speaking=True from GQL is not promoted to a speech assertion.
    participant = SpaceParticipantInfo(role="speaker", is_speaking=True, source="gql")
    assert participant_payload(participant)["active_speaking"] is None


def test_participant_leave_and_rejoin_require_complete_snapshots():
    tracker = SpeakerObservationTracker("space", start_debounce_ms=0, stop_debounce_ms=0)
    sample(tracker, 0, 0, [person()], complete=True)
    _, partial = sample(tracker, 1, 1, [], complete=False)
    assert partial == []
    _, left = sample(tracker, 2, 2, [], complete=True)
    assert [e["event"] for e in left] == ["participant_left"]
    _, joined = sample(tracker, 3, 3, [person()], complete=True)
    assert [e["event"] for e in joined] == ["participant_joined"]


def test_speech_start_stop_ignore_flicker_and_need_dom_evidence():
    tracker = SpeakerObservationTracker("space", start_debounce_ms=1000, stop_debounce_ms=1000)
    sample(tracker, 0, 0, [person(active=False)], active=True)
    _, events = sample(tracker, 1, 0.2, [person(active=True)], active=True)
    assert events == []
    sample(tracker, 2, 0.6, [person(active=False)], active=True)
    _, events = sample(tracker, 3, 1.4, [person(active=True)], active=True)
    assert events == []
    _, events = sample(tracker, 4, 2.5, [person(active=True)], active=True)
    assert [e["event"] for e in events] == ["speaking_started"]
    _, events = sample(tracker, 5, 2.7, [person(active=False)], active=True)
    assert events == []
    _, events = sample(tracker, 6, 3.8, [person(active=False)], active=True)
    assert [e["event"] for e in events] == ["speaking_stopped"]


def test_jsonl_is_append_only_and_readable(tmp_path):
    target = tmp_path / "speaker_observations.jsonl"
    append_jsonl(target, {"n": 1})
    append_jsonl(target, {"n": 2})
    assert [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()] == [
        {"n": 1},
        {"n": 2},
    ]


def test_database_event_persists_even_when_jsonl_path_fails(tmp_path):
    init_db(tmp_path / "speaker.sqlite3")
    with get_session() as db:
        row = Space(space_id="testSpace", state="recording", url="https://x.com/i/spaces/testSpace")
        db.add(row)
        db.commit()
        space_row_id = row.id
    folder = tmp_path / "space"
    folder.mkdir()
    adapter = object()
    session = SpaceSession(
        "testSpace", "https://x.com/i/spaces/testSpace", "host", SpacePaths(folder), adapter
    )
    session._space_db_id = space_row_id
    session._speaker_event_file = folder  # opening a directory as JSONL must fail
    event = {
        "recording_time": 1.25,
        "wall_time": "2026-10-06T00:00:01Z",
        "event": "participant_seen",
        "username": "listener",
        "source": "x_gql",
        "confidence": 1.0,
        "display_name": "Listener",
        "role": "listener",
    }
    asyncio.run(session._persist_observed_event(event))
    with get_session() as db:
        stored = db.query(SpeakerEvent).one()
        assert stored.event_type == "participant_seen"
        assert stored.recording_offset_ms == 1250
        assert stored.username == "listener"
    assert session.speaker_collector_health["consecutive_failures"] == 1


def test_real_observation_persists_identity_relationship_and_linked_event(tmp_path):
    init_db(tmp_path / "speaker-participants.sqlite3")
    with get_session() as db:
        space = Space(space_id="participant-space", state="recording")
        db.add(space)
        db.commit()
        space_id = space.id

    class Adapter:
        _last_participant_source = "x_gql"

        async def getParticipants(self):
            return [
                SpaceParticipantInfo(
                    username="speaker_one",
                    display_name="Speaker One",
                    avatar_url="https://example.test/avatar.png",
                    role="speaker",
                    source="x_gql",
                )
            ]

    folder = tmp_path / "space"
    folder.mkdir()
    session = SpaceSession(
        "participant-space",
        "https://x.com/i/spaces/participant-space",
        "host",
        SpacePaths(folder),
        Adapter(),
    )
    session._space_db_id = space_id
    session._speaker_observation_file = folder / "speaker_observations.jsonl"
    session._speaker_event_file = folder / "speaker_events.jsonl"

    asyncio.run(session._collect_speaker_observation(use_gql=False))

    with get_session() as db:
        participant = db.query(Participant).one()
        relation = db.query(SpaceParticipant).one()
        event = db.query(SpeakerEvent).one()
        assert participant.username == "speaker_one"
        assert participant.display_name == "Speaker One"
        assert participant.avatar_url == "https://example.test/avatar.png"
        assert participant.first_seen_at is not None
        assert participant.last_seen_at is not None
        assert relation.role == "speaker"
        assert relation.last_seen_at is not None
        assert event.participant_id == participant.id


def test_collector_failure_recovers_with_gap_and_does_not_change_recorder_state(tmp_path):
    class Adapter:
        fail = True

        async def getParticipants(self):
            if self.fail:
                raise RuntimeError("temporary DOM disconnect")
            return [SpaceParticipantInfo(username="sample_user", role="speaker")]

    folder = tmp_path / "space"
    folder.mkdir()
    session = SpaceSession(
        "testSpace", "https://x.com/i/spaces/testSpace", "host", SpacePaths(folder), Adapter()
    )
    session._running = True
    session._speaker_observation_file = folder / "speaker_observations.jsonl"
    session._speaker_event_file = folder / "speaker_events.jsonl"
    try:
        asyncio.run(session._collect_speaker_observation(use_gql=False))
    except RuntimeError as exc:
        session._speaker_failure("participant observation failed", exc)
    assert session._running is True
    assert session.speaker_collector_health["consecutive_failures"] == 1
    session.adapter.fail = False
    asyncio.run(session._collect_speaker_observation(use_gql=False))
    events = [
        json.loads(line) for line in session._speaker_event_file.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["observation_gap", "participant_seen"]
    assert session.speaker_collector_health["consecutive_failures"] == 0
    assert session._running is True


def test_empty_collector_is_not_reported_as_healthy_coverage(tmp_path):
    class Adapter:
        async def getParticipants(self):
            return []

    folder = tmp_path / "space"
    folder.mkdir()
    session = SpaceSession(
        "testSpace", "https://x.com/i/spaces/testSpace", "host", SpacePaths(folder), Adapter()
    )
    session._speaker_observation_file = folder / "speaker_observations.jsonl"
    session._speaker_event_file = folder / "speaker_events.jsonl"
    asyncio.run(session._collect_speaker_observation(use_gql=False))
    assert session.speaker_collector_health["coverage_state"] == "unknown"
    asyncio.run(session._collect_speaker_observation(use_gql=False))
    asyncio.run(session._collect_speaker_observation(use_gql=False))
    assert session.speaker_collector_health["speaker_collector_running"] is True
    assert session.speaker_collector_health["coverage_state"] == "unavailable"
    assert session.speaker_collector_health["quality_state"] == "unavailable"
