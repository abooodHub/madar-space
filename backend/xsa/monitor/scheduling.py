"""Persisted discovery cadence and per-profile transport measurements."""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit


class AccountPollError(RuntimeError):
    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


class GraphqlWindowBudget:
    """Count X GraphQL requests across profile, sidebar and manual tabs."""

    def __init__(self, window_seconds: float = 300) -> None:
        self.window_seconds = window_seconds
        self._requests: deque[float] = deque()
        self._authentication_failure_at: float | None = None

    def used(self) -> int:
        now = time.monotonic()
        while self._requests and self._requests[0] <= now - self.window_seconds:
            self._requests.popleft()
        return len(self._requests)

    def available(self, limit: int) -> bool:
        return self.used() < limit

    def on_request(self, request) -> None:
        if PollMeasurements._is_graphql(request.url):
            self._requests.append(time.monotonic())

    def on_response(self, response) -> None:
        if not PollMeasurements._is_graphql(response.url):
            return
        if response.status in (401, 403):
            self._authentication_failure_at = time.monotonic()
        elif response.status == 200:
            self._authentication_failure_at = None

    def authentication_required(self) -> bool:
        return (
            self._authentication_failure_at is not None
            and time.monotonic() - self._authentication_failure_at < 600
        )


def next_poll_at(
    *,
    now: datetime,
    interval: float,
    outcome: str,
    failures: int,
    retry_after: float = 0,
    jitter_seconds: float = 0,
) -> datetime:
    """Never shorten a server cooldown; jitter only adds a bounded delay."""
    delay = interval
    if outcome in {"error", "authentication_required"}:
        delay = max(interval, min(3600, interval * 2 ** min(max(failures, 1), 6)))
    delay = max(delay, retry_after)
    return now + timedelta(seconds=delay + random.uniform(0, max(0, jitter_seconds)))


@dataclass
class PollMeasurements:
    """Count actual X GraphQL traffic without retaining URLs or credentials."""

    requests: int = 0
    statuses: list[int] = field(default_factory=list)

    @staticmethod
    def _is_graphql(url: str) -> bool:
        parts = urlsplit(url)
        return parts.hostname == "x.com" and parts.path.startswith("/i/api/graphql/")

    def on_request(self, request) -> None:
        if self._is_graphql(request.url):
            self.requests += 1

    def on_response(self, response) -> None:
        if self._is_graphql(response.url):
            self.statuses.append(response.status)

    def outcome(self, detected: bool, retry_after: float) -> str:
        if retry_after > 0 or 429 in self.statuses:
            return "rate_limited"
        if any(status in (401, 403) for status in self.statuses):
            return "authentication_required"
        if any(status >= 500 for status in self.statuses):
            return "error"
        return "join_scheduled" if detected else "checked"
