"""Explicit conversions between media, recording, Space, and wall-clock time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class Timebase:
    """An affine media-time mapping anchored to UTC wall time.

    ``media_origin_wall_time`` must describe media timestamp zero (not merely
    the time the FFmpeg process was requested). ``space_started_at`` is optional
    because X metadata often does not expose a trustworthy start instant.
    """

    media_origin_wall_time: datetime
    space_started_at: datetime | None = None
    media_timestamp_zero_s: float = 0.0

    def __post_init__(self) -> None:
        if self.media_origin_wall_time.tzinfo is None:
            raise ValueError("media_origin_wall_time must be timezone-aware")
        if self.space_started_at is not None and self.space_started_at.tzinfo is None:
            raise ValueError("space_started_at must be timezone-aware")

    def media_to_wall(self, recording_time_s: float) -> datetime:
        if recording_time_s < 0:
            raise ValueError("recording_time_s must be non-negative")
        return self.media_origin_wall_time.astimezone(UTC) + timedelta(
            seconds=recording_time_s - self.media_timestamp_zero_s
        )

    def wall_to_media(self, wall_time: datetime) -> float:
        if wall_time.tzinfo is None:
            raise ValueError("wall_time must be timezone-aware")
        return (
            self.media_timestamp_zero_s
            + (wall_time.astimezone(UTC) - self.media_origin_wall_time.astimezone(UTC)).total_seconds()
        )

    def media_to_space(self, recording_time_s: float) -> float | None:
        if self.space_started_at is None:
            return None
        return (self.media_to_wall(recording_time_s) - self.space_started_at.astimezone(UTC)).total_seconds()
