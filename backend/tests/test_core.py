"""Tests for core backend logic (run with: cd backend && pytest -v)"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 1. Logging / secret redaction
# ---------------------------------------------------------------------------


def test_logging_redacts_bearer_token():
    from xsa.logging_setup import redact

    dirty = "Authorization: Bearer AAAAAAAAAAAAAAAAAAAAAMLhe..."
    clean = redact(dirty)
    assert "AAAAAAAAAAAAAAAAAAAAAMLhe" not in clean
    assert "Bearer" in clean


def test_logging_redacts_auth_token_cookie():
    from xsa.logging_setup import redact

    dirty = "Set-Cookie: auth_token=abc123xyz; ct0=def456; path=/"
    clean = redact(dirty)
    assert "abc123xyz" not in clean
    assert "def456" not in clean


def test_logging_preserves_normal_text():
    from xsa.logging_setup import redact

    normal = "Space detected: host=@ahmed state=live"
    assert redact(normal) == normal


# ---------------------------------------------------------------------------
# 2. Paths — safe_id and layout
# ---------------------------------------------------------------------------


def test_safe_id_strips_special_chars():
    from xsa.paths import safe_id

    assert safe_id("1rmAnM2YkJY0K1sL7Ru") == "1rmAnM2YkJY0K1sL7Ru"
    assert safe_id("abc-123_def") == "abc-123_def"


def test_safe_id_rejects_empty_result():
    from xsa.paths import safe_id

    with pytest.raises(ValueError):
        safe_id("!@#$%")


def test_space_paths_structure():
    from xsa.paths import SpacePaths

    base = Path(tempfile.mkdtemp())
    ts = datetime(2025, 3, 15, tzinfo=UTC)
    sp = SpacePaths.for_space(base, "testSpace123", ts).ensure()
    assert sp.root == base / "spaces" / "2025" / "03" / "15" / "030000 - testSpace123"
    assert sp.root.exists()
    assert not sp.avatars.exists()
    assert not sp.screenshots.exists()
    assert not sp.audio_dir.exists()
    assert not sp.transcript_dir.exists()
    assert sp.segment(0) == sp.root / "recording_000.mkv"
    assert sp.segment(1) == sp.root / "recording_001.mkv"
    assert sp.space_json.name == "space.json"
    assert sp.speaker_events_live.suffix == ".jsonl"


# ---------------------------------------------------------------------------
# 3. Database — models
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    import time

    from xsa.db import database as _db_mod
    from xsa.db import get_session, init_db

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    with get_session() as session:
        yield session
    # Dispose engine so SQLite WAL is released before unlink (Windows)
    if _db_mod._engine:
        _db_mod._engine.dispose()
        _db_mod._engine = None
        _db_mod._SessionLocal = None
    time.sleep(0.05)
    try:
        os.unlink(db_path)
        for ext in ("-wal", "-shm"):
            p = Path(str(db_path) + ext)
            if p.exists():
                p.unlink()
    except PermissionError:
        pass  # non-critical; temp dir will be cleaned eventually


def test_db_add_monitored_account(db_session):
    from xsa.db.models import MonitoredAccount

    acc = MonitoredAccount(username="testuser", added_at=datetime.now(UTC))
    db_session.add(acc)
    db_session.commit()
    result = db_session.query(MonitoredAccount).filter_by(username="testuser").first()
    assert result is not None
    assert result.username == "testuser"
    assert result.active is True


def test_db_add_space(db_session):
    from xsa.db.models import Space

    space = Space(space_id="abc123", state="detected", detected_at=datetime.now(UTC))
    db_session.add(space)
    db_session.commit()
    result = db_session.query(Space).filter_by(space_id="abc123").first()
    assert result is not None
    assert result.state == "detected"


def test_db_speaker_event(db_session):
    from xsa.db.models import Space, SpeakerEvent

    space = Space(space_id="sp1", state="recording", detected_at=datetime.now(UTC))
    db_session.add(space)
    db_session.flush()
    evt = SpeakerEvent(
        space_id=space.id,
        event_type="speaker_started",
        username="ahmed",
        wall_time=datetime.now(UTC),
        recording_offset_ms=34210,
        source="x_dom",
    )
    db_session.add(evt)
    db_session.commit()
    result = db_session.query(SpeakerEvent).filter_by(space_id=space.id).all()
    assert len(result) == 1
    assert result[0].event_type == "speaker_started"
    assert result[0].recording_offset_ms == 34210


def test_db_recording_segment(db_session):
    from xsa.db.models import RecordingSegment, Space

    space = Space(space_id="sp2", state="recording", detected_at=datetime.now(UTC))
    db_session.add(space)
    db_session.flush()
    seg = RecordingSegment(
        space_id=space.id,
        segment_index=0,
        file_path="/data/spaces/2025/03/sp2/recording_000.mkv",
        started_at=datetime.now(UTC),
    )
    db_session.add(seg)
    db_session.commit()
    result = db_session.query(RecordingSegment).filter_by(space_id=space.id).all()
    assert len(result) == 1
    assert result[0].segment_index == 0


# ---------------------------------------------------------------------------
# 4. Config — secret types
# ---------------------------------------------------------------------------


def test_config_bearer_token_is_secret():
    from xsa.config import Settings

    cfg = Settings(X_API_BEARER_TOKEN="secret-value-12345")  # type: ignore[call-arg]
    assert "secret-value-12345" not in repr(cfg)
    assert "secret-value-12345" not in str(cfg)
    assert cfg.x_api_bearer_token is not None
    assert cfg.x_api_bearer_token.get_secret_value() == "secret-value-12345"


def test_config_record_size_parsing():
    from xsa.config import Settings

    cfg = Settings(record_size="1280x720")
    assert cfg.record_width == 1280
    assert cfg.record_height == 720
    mobile_cfg = Settings(record_size="720x1280")
    assert mobile_cfg.record_width == 720
    assert mobile_cfg.record_height == 1280


# ---------------------------------------------------------------------------
# 5. Speaker event JSONL (live write / read)
# ---------------------------------------------------------------------------


def test_speaker_event_jsonl():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = Path(f.name)

    events = [
        {"timestamp": 10.5, "event": "speaker_started", "username": "ahmed"},
        {"timestamp": 42.1, "event": "speaker_stopped", "username": "ahmed"},
    ]
    with path.open("a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["event"] == "speaker_started"
    assert lines[1]["timestamp"] == pytest.approx(42.1)
    path.unlink()


# ---------------------------------------------------------------------------
# 6. Anonymous listening preferences
# ---------------------------------------------------------------------------


def test_config_anonymous_listening_defaults():
    from xsa.config import Settings

    cfg = Settings()
    assert cfg.prefer_anonymous_listening is True
    assert cfg.allow_authenticated_fallback is True


@pytest.mark.asyncio
async def test_open_space_anonymous_fallback_rejection():
    """Verify that openSpace aborts if anonymous listening is not available and fallback is disabled."""
    from unittest.mock import AsyncMock

    from xsa.browser.adapter import XBrowserAdapter

    adapter = XBrowserAdapter(Path("/tmp/fake_profile"))
    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    # No anonymous toggle or button found
    mock_page.query_selector = AsyncMock(return_value=None)
    mock_btn = AsyncMock()
    mock_page.wait_for_selector = AsyncMock(return_value=mock_btn)
    adapter._page = mock_page

    result = await adapter.openSpace(
        "https://x.com/i/spaces/123", prefer_anonymous=True, allow_authenticated_fallback=False
    )
    assert result is False
