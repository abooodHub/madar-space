"""SQLAlchemy ORM models for XSpace Analyzer.

The DB lives on a Linux named volume (POSIX-locking safe) so WAL mode works.
All tables use Integer PKs (faster joins), never raw UUIDs as PKs.
External IDs (X user IDs, space IDs) are stored as TEXT and indexed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow_naive() -> datetime:
    """Return a naive UTC timestamp for legacy SQLite DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Core identity tables
# ---------------------------------------------------------------------------


class MonitoredAccount(Base):
    __tablename__ = "monitored_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)  # without @
    display_name: Mapped[str | None] = mapped_column(String(200))
    x_user_id: Mapped[str | None] = mapped_column(String(50), index=True)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    avatar_cached_at: Mapped[datetime | None] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[str] = mapped_column(String(2), default="P2", server_default="P2", nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_poll_outcome: Mapped[str | None] = mapped_column(String(40))
    last_poll_duration_seconds: Mapped[float | None] = mapped_column(Float)
    last_poll_request_count: Mapped[int | None] = mapped_column(Integer)
    poll_failure_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    tags: Mapped[str | None] = mapped_column(Text, default="")
    notes: Mapped[str | None] = mapped_column(Text)

    spaces: Mapped[list[Space]] = relationship(
        "Space", back_populates="host_account", foreign_keys="Space.host_account_id"
    )


class Participant(Base):
    """A unique X user seen across any Space."""

    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    x_user_id: Mapped[str | None] = mapped_column(String(50), unique=True, index=True)
    periscope_id: Mapped[str | None] = mapped_column(String(80), index=True)
    username: Mapped[str | None] = mapped_column(String(100), index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    avatar_cached_path: Mapped[str | None] = mapped_column(Text)
    avatar_cached_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, onupdate=_utcnow_naive, nullable=False
    )


# ---------------------------------------------------------------------------
# Space
# ---------------------------------------------------------------------------


class Space(Base):
    __tablename__ = "spaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(30), default="detected", nullable=False, index=True)
    # lifecycle: detected | joining | recording | finalizing | processing | completed
    # exceptional: rejected | error | interrupted | development_interrupted |
    #              recovered_stale | blocked
    url: Mapped[str | None] = mapped_column(Text)
    media_key: Mapped[str | None] = mapped_column(String(100), index=True)
    hls_url: Mapped[str | None] = mapped_column(Text)

    host_account_id: Mapped[int | None] = mapped_column(ForeignKey("monitored_accounts.id"))
    verified_host_username: Mapped[str | None] = mapped_column(String(100))
    host_account: Mapped[MonitoredAccount | None] = relationship(
        "MonitoredAccount", back_populates="spaces", foreign_keys=[host_account_id]
    )
    host_participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"))

    detected_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    recording_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    recording_ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    transcript_generation_id: Mapped[str | None] = mapped_column(String(64))

    # Join lifecycle diagnostics. Keep these separate from recording timestamps:
    # a detected/joining Space has not necessarily started media capture.
    joining_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    join_checkpoint: Mapped[str | None] = mapped_column(String(100))
    join_checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime)
    join_deadline_seconds: Mapped[float | None] = mapped_column(Float)
    join_age_seconds: Mapped[float | None] = mapped_column(Float)
    join_outcome: Mapped[str | None] = mapped_column(String(80))
    join_last_error: Mapped[str | None] = mapped_column(Text)
    join_host_verification_state: Mapped[str | None] = mapped_column(String(40))
    join_anonymous_listen_state: Mapped[str | None] = mapped_column(String(40))
    join_space_state: Mapped[str | None] = mapped_column(String(40))
    join_browser_url: Mapped[str | None] = mapped_column(Text)
    join_browser_title: Mapped[str | None] = mapped_column(Text)

    total_live_listeners: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(10))
    is_ticketed: Mapped[bool] = mapped_column(Boolean, default=False)
    replay_available: Mapped[bool] = mapped_column(Boolean, default=False)

    space_metadata: Mapped[dict | None] = mapped_column(JSON)  # raw API response

    recording_segments: Mapped[list[RecordingSegment]] = relationship(
        "RecordingSegment", back_populates="space", order_by="RecordingSegment.segment_index"
    )
    speaker_events: Mapped[list[SpeakerEvent]] = relationship("SpeakerEvent", back_populates="space")
    space_participants: Mapped[list[SpaceParticipant]] = relationship(
        "SpaceParticipant", back_populates="space"
    )
    jobs: Mapped[list[Job]] = relationship("Job", back_populates="space")


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class RecordingSegment(Base):
    __tablename__ = "recording_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), nullable=False, index=True)
    space: Mapped[Space] = relationship("Space", back_populates="recording_segments")

    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="recording", nullable=False)
    # states: recording | complete | corrupt | merged

    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    file_bytes: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    video_codec: Mapped[str | None] = mapped_column(String(30))
    audio_codec: Mapped[str | None] = mapped_column(String(30))
    dropped_frames: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("space_id", "segment_index", name="uq_segment"),)


# ---------------------------------------------------------------------------
# Participants in a space
# ---------------------------------------------------------------------------


class SpaceParticipant(Base):
    __tablename__ = "space_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), nullable=False, index=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), nullable=False)
    space: Mapped[Space] = relationship("Space", back_populates="space_participants")
    participant: Mapped[Participant] = relationship("Participant")

    role: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # host | cohost | speaker | listener | unknown
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    total_speaking_ms: Mapped[int] = mapped_column(Integer, default=0)
    speaking_turns: Mapped[int] = mapped_column(Integer, default=0)
    is_muted: Mapped[bool | None] = mapped_column(Boolean)

    __table_args__ = (
        UniqueConstraint("space_id", "participant_id", name="uq_space_participant"),
        Index("ix_sp_space_role", "space_id", "role"),
    )


# ---------------------------------------------------------------------------
# Speaker events (live DOM observations)
# ---------------------------------------------------------------------------


class SpeakerEvent(Base):
    __tablename__ = "speaker_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), nullable=False, index=True)
    space: Mapped[Space] = relationship("Space", back_populates="speaker_events")

    recording_offset_ms: Mapped[int | None] = mapped_column(Integer)  # ms since rec_start
    wall_time: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)

    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # speaker_started | speaker_stopped | joined | left | became_speaker | became_listener | host | cohost

    participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"), index=True)
    username: Mapped[str | None] = mapped_column(String(100))
    x_user_id: Mapped[str | None] = mapped_column(String(50))

    source: Mapped[str] = mapped_column(String(20), default="x_dom")
    # x_dom | diarization | inferred | api
    confidence: Mapped[float | None] = mapped_column(Float)
    raw_data: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (Index("ix_se_space_time", "space_id", "recording_offset_ms"),)


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), nullable=False, index=True)

    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_raw: Mapped[str | None] = mapped_column(Text)  # before normalization
    language: Mapped[str | None] = mapped_column(String(10))
    confidence: Mapped[float | None] = mapped_column(Float)
    no_speech_prob: Mapped[float | None] = mapped_column(Float)

    # speaker identity
    speaker_label: Mapped[str | None] = mapped_column(String(50))  # SPEAKER_00 from diarization
    participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"), index=True)
    speaker_confidence: Mapped[float | None] = mapped_column(Float)
    speaker_source: Mapped[str | None] = mapped_column(String(20))

    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_ts_space_start", "space_id", "start_ms"),)


class TranscriptWord(Base):
    __tablename__ = "transcript_words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("transcript_segments.id"), nullable=False, index=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), nullable=False, index=True)

    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    word: Mapped[str] = mapped_column(String(200), nullable=False)
    probability: Mapped[float | None] = mapped_column(Float)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


class Mention(Base):
    __tablename__ = "mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), nullable=False, index=True)
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("transcript_segments.id"))

    start_ms: Mapped[int] = mapped_column(Integer)
    mention_text: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    mention_type: Mapped[str] = mapped_column(String(30))  # username | name | topic | keyword
    mentioned_participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"))
    speaker_participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"))
    context: Mapped[str | None] = mapped_column(Text)  # 50-word window


class AnalysisEvent(Base):
    """Evidence-backed classification (insults, attacks, keyword matches etc)."""

    __tablename__ = "analysis_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), nullable=False, index=True)
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("transcript_segments.id"))

    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    classification: Mapped[str | None] = mapped_column(String(60))
    confidence: Mapped[float | None] = mapped_column(Float)
    speaker_participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"))
    target_participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id"))
    excerpt: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(20), default="keyword")


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20), default="llm")


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[int | None] = mapped_column(ForeignKey("spaces.id"), index=True)
    space: Mapped[Space | None] = relationship("Space", back_populates="jobs")

    job_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    # post_process | prepare_media | diarize; older rows may use other stage names.

    state: Mapped[str] = mapped_column(String(20), default="queued", nullable=False, index=True)
    # queued | running | completed | failed | retrying | cancelled
    priority: Mapped[int] = mapped_column(Integer, default=5)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    owner_id: Mapped[str | None] = mapped_column(String(100))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempt_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_job_state_prio", "state", "priority"),)


# ---------------------------------------------------------------------------
# System events log
# ---------------------------------------------------------------------------


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_time: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    space_id: Mapped[int | None] = mapped_column(ForeignKey("spaces.id"))
    details: Mapped[dict | None] = mapped_column(JSON)
