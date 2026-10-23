"""Tests for the new features:
- Tags system and account tags update
- Live feed endpoint
- DisplaySlotPool scaling to 10 slots
- Thumbnail and Video streaming endpoints
- Account filtering and stats
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from xsa.api.app import app
from xsa.config import get_settings
from xsa.db import Job, MonitoredAccount, Space, get_session, init_db
from xsa.paths import SpacePaths
from xsa.recorder.slot_pool import DisplaySlotPool


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    db_file = tmp_path / "test_features.sqlite3"
    init_db(db_file)
    with get_session() as db:
        acc1 = MonitoredAccount(username="user1", display_name="User One", active=True, tags="سياسة,رياضة")
        acc2 = MonitoredAccount(username="user2", display_name="User Two", active=False, tags="رياضة,نخبوي")
        db.add_all([acc1, acc2])
        db.commit()


def test_accounts_includes_tags_and_metadata():
    client = TestClient(app)
    res = client.get("/accounts")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 2
    u1 = next(x for x in data if x["username"] == "user1")
    assert u1["tags"] == ["سياسة", "رياضة"]
    assert "spaces_count" in u1
    assert "is_live" in u1
    assert "is_recording" in u1


def test_update_account_tags():
    client = TestClient(app)
    res = client.put("/accounts/user1/tags", json={"tags": ["تقنية", "ذكاء_اصطناعي"]})
    assert res.status_code == 200
    assert res.json()["tags"] == ["تقنية", "ذكاء_اصطناعي"]

    # Verify via /tags endpoint
    tags_res = client.get("/tags")
    assert tags_res.status_code == 200
    tags_data = tags_res.json()
    tag_names = [t["name"] for t in tags_data]
    assert "تقنية" in tag_names
    assert "ذكاء_اصطناعي" in tag_names


def test_live_feed_endpoint():
    client = TestClient(app)
    res = client.get("/live/feed")
    assert res.status_code == 200
    data = res.json()
    assert "feed" in data
    assert "count" in data


def test_slot_pool_supports_10_slots():
    pool = DisplaySlotPool(max_slots=10)
    assert pool.max_slots == 10
    assert len(pool._slots) == 10
    # Slot pool uses private per-space displays and sinks.
    assert pool._slots[0].display == ":100"
    assert pool._slots[0].sink_name == "xspace_slot0"
    # Slot 9
    assert pool._slots[9].display == ":109"
    assert pool._slots[9].sink_name == "xspace_slot9"


def test_video_and_thumbnail_404_when_missing():
    client = TestClient(app)
    res_thumb = client.get("/spaces/non_existent_123/thumbnail")
    assert res_thumb.status_code == 404

    res_vid = client.get("/spaces/non_existent_123/video")
    assert res_vid.status_code == 404


def test_recordings_library_hides_failed_attempts_without_artifacts(tmp_path, monkeypatch):
    import importlib

    api_module = importlib.import_module("xsa.api.app")

    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        api_module,
        "_cfg",
        get_settings().model_copy(update={"data_dir": data_dir}),
    )
    with get_session() as db:
        db.add_all(
            [
                Space(space_id="rejectedWithoutMedia", state="rejected", join_outcome="host_mismatch"),
                Space(space_id="activeCapture", state="recording"),
                Space(space_id="savedCapture", state="ended"),
            ]
        )
        db.commit()
        db.add_all(
            Space(space_id=f"failedAttempt{index:03}", state="error", join_outcome="join_timeout")
            for index in range(105)
        )
        db.commit()
        saved_space = db.query(Space).filter_by(space_id="savedCapture").one()
        db.add(
            Job(
                space_id=saved_space.id,
                job_type="post_process",
                state="running",
                progress=0.35,
                result={"stage": "transcription"},
                attempt=1,
            )
        )
        db.commit()

    paths = SpacePaths.planned_path(
        data_dir,
        "savedCapture",
        datetime(2026, 10, 12),
        title="Saved Space",
        host="@host",
    ).ensure()
    paths.segment(0).write_bytes(b"fixture media")

    client = TestClient(app)
    saved = client.get("/spaces").json()
    assert {row["space_id"] for row in saved} == {"activeCapture", "savedCapture"}
    saved_row = next(row for row in saved if row["space_id"] == "savedCapture")
    assert saved_row["has_video"] is True
    assert saved_row["has_saved_artifact"] is True
    assert saved_row["processing_state"] == "running"
    assert saved_row["processing_progress"] == 0.35
    assert saved_row["processing_stage"] == "transcription"
    assert saved_row["status"]["capture"] == "saved"
    assert saved_row["status"]["transcription"] == "processing"
    assert saved_row["status"]["analysis"] == "processing"

    attempts = client.get("/spaces", params={"include_failed": "true"}).json()
    assert len(attempts) == 108
    rejected_row = next(row for row in attempts if row["space_id"] == "rejectedWithoutMedia")
    assert rejected_row["has_video"] is False
    assert rejected_row["has_saved_artifact"] is False
    assert rejected_row["join_outcome"] == "host_mismatch"
    assert rejected_row["status"]["capture"] == "failed"


def test_live_details_exposes_silence_and_space_end_state(monkeypatch):
    import importlib
    from types import SimpleNamespace

    api_module = importlib.import_module("xsa.api.app")
    rec_status = SimpleNamespace(
        health=True,
        state="recording",
        duration_seconds=1200,
        file_bytes=1_000_000,
    )
    session = SimpleNamespace(
        host_username="host",
        _recorder=SimpleNamespace(
            get_status=lambda: rec_status,
            get_audio_silence_status=lambda: {
                "audio_silent": True,
                "silence_suspected": True,
                "silence_duration_seconds": 900,
            },
        ),
        adapter=None,
        slot=SimpleNamespace(slot_id=0, display=":100"),
        _speaker_durations={},
        _speaker_turn_counts={},
        speaker_collector_health={"last_success_at": "2026-10-12T10:00:00Z"},
        end_detector=SimpleNamespace(snapshot=lambda: {"space_state": "UNKNOWN"}),
    )
    monitor = SimpleNamespace(
        active_space_ids=["spaceFake123"],
        _active_sessions={"spaceFake123": session},
    )
    monkeypatch.setattr(api_module, "_monitor", monitor)

    result = TestClient(app).get("/live/details")
    assert result.status_code == 200
    active = result.json()["active"][0]
    assert active["audio_silence"]["silence_duration_seconds"] == 900
    assert active["end_detector"]["space_state"] == "UNKNOWN"
    assert active["ffmpeg_healthy"] is True
