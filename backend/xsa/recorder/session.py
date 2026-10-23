"""Space session: combines browser adapter + recorder + speaker event collection.

This is the runtime object for one live Space recording session. It is created
by the monitor when a Space is detected and destroyed when the Space ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xsa.blocking import run_blocking
from xsa.browser import SpaceParticipantInfo, XBrowserAdapter
from xsa.config import Settings, get_settings
from xsa.db import Space as SpaceModel
from xsa.db import get_session
from xsa.db.models import MonitoredAccount, RecordingSegment, SpeakerEvent, SystemEvent
from xsa.db.participant_persistence import persist_space_participant
from xsa.logging_setup import get_logger
from xsa.paths import SpacePaths
from xsa.recorder.recorder import DockerRecorder
from xsa.space_document import write_space_info_html
from xsa.space_end import SpaceEndDetector, SpaceEndState, should_stop_unverified_silence
from xsa.speaker_observation import SpeakerObservationTracker, append_jsonl, participant_payload

log = get_logger("SPACE")


class SpaceSession:
    """Manages one recording session for one live X Space."""

    def __init__(
        self,
        space_id: str,
        space_url: str,
        host_username: str,
        paths: SpacePaths,
        adapter: XBrowserAdapter,
        cfg: Settings | None = None,
        slot: Any | None = None,
        manual_capture: bool = False,
    ) -> None:
        self.space_id = space_id
        self.space_url = space_url
        self.host_username = host_username
        self.space_title: str | None = None
        self.paths = paths
        self.adapter = adapter
        self.cfg = cfg or get_settings()
        self.slot = slot
        self.manual_capture = manual_capture

        # Route video and audio to dedicated virtual display and pulse sink
        display = slot.display if slot else self.cfg.master_display
        sink = slot.sink_name if slot else "xspace"
        self._recorder = DockerRecorder(self.cfg, display=display, sink=sink)

        self._segment_index = 0
        self._segment_started_at: datetime | None = None
        self._running = False
        self._started_at: datetime | None = None
        self._ended_at: datetime | None = None
        self._speaker_event_file: Path | None = None
        self._last_active_speaker: str | None = None  # username
        self._speaker_start_offset: float | None = None
        self._rec_wall_start: float | None = None
        # Speaker timeline: username → cumulative speaking ms
        self._speaker_durations: dict[str, int] = {}
        self._speaker_turn_counts: dict[str, int] = {}
        # DB space row id (set by monitor after creation)
        self._space_db_id: int | None = None
        self._stop_lock = asyncio.Lock()
        self._stop_summary: dict | None = None
        self._stop_reason: str | None = None
        self._speaker_observation_file: Path | None = None
        self._speaker_tracker = SpeakerObservationTracker(
            space_id=space_id,
            start_debounce_ms=self.cfg.speaker_start_debounce_ms,
            stop_debounce_ms=self.cfg.speaker_stop_debounce_ms,
        )
        self.speaker_collector_health: dict[str, Any] = {
            "speaker_collector_running": False,
            "last_success_at": None,
            "last_event_at": None,
            "consecutive_failures": 0,
            "observations": 0,
            "events": 0,
            "last_error": None,
            "last_error_category": None,
            "coverage_state": "unknown",
            "quality_state": "unknown",
            "consecutive_empty_observations": 0,
            "participants_seen_total": 0,
            "last_participant_at": None,
            "active_speech_supported": False,
        }
        self._speaker_failure_started_at: str | None = None
        self.end_detector = SpaceEndDetector(
            confirm_seconds=float(self.cfg.end_confirm_seconds), space_id=space_id
        )
        self.space_end_detected_at: datetime | None = None
        self.space_end_recording_time: float | None = None
        self.space_end_source: str | None = None
        self.space_end_evidence: str | None = None
        self.space_end_confidence: float | None = None
        self.recording_stopped_at: datetime | None = None
        self.recording_stop_recording_time: float | None = None
        self.end_tail_duration: float | None = None
        self.join_progress_callback = None
        self.join_started_monotonic: float | None = None
        self.join_outcome: str | None = None
        self.join_last_error: str | None = None
        self._capture_recoveries: list[dict[str, Any]] = []

    async def report_join_progress(self, checkpoint: str, details: dict | None = None) -> None:
        """Record a meaningful join checkpoint for API health and operator UI."""
        now = datetime.now(UTC)
        self.adapter.join_checkpoint = checkpoint
        callback = self.join_progress_callback
        if callback:
            result = callback(checkpoint, details or {}, now)
            if inspect.isawaitable(result):
                await result

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._started_at = datetime.now(UTC)
        self.join_started_monotonic = time.monotonic()
        await self.report_join_progress("opening_space")

        # Join the space in the browser (respecting anonymous listening preference)
        joined = await self.adapter.openSpace(
            self.space_url,
            prefer_anonymous=self.cfg.prefer_anonymous_listening,
            allow_authenticated_fallback=self.cfg.allow_authenticated_fallback,
        )
        if not joined:
            self.join_outcome = getattr(self.adapter, "join_outcome", None) or "player_not_ready"
            self.join_last_error = getattr(self.adapter, "join_last_error", None)
            raise RuntimeError(
                self.join_last_error or f"Failed to join Space {self.space_url}: {self.join_outcome}"
            )

        # Dismiss any banners that might block the UI
        await self.report_join_progress("verifying_host")
        await self.adapter.dismiss_cookie_banners()
        await asyncio.sleep(1)

        # Enforce host verification: Only record if host is in the active monitored accounts list
        verified_space_data = None
        if self.host_username:
            self.adapter.join_host_verification_state = "verifying"
            await self.report_join_progress("verifying_host")
            actual_host = None
            graphql_failed = False
            try:
                from xsa.browser.graphql import fetch_audio_space_by_id

                if self.adapter._page:
                    verified_space_data = await asyncio.wait_for(
                        fetch_audio_space_by_id(self.adapter._page, self.space_id),
                        # The GraphQL helper may first inspect X's loaded audio
                        # bundle and then make a browser-context request. Five
                        # seconds routinely cancelled valid host lookups before
                        # they returned, making live Spaces look unverifiable.
                        timeout=20.0,
                    )
                    if verified_space_data:
                        creator = (
                            verified_space_data.get("metadata", {})
                            .get("creator_results", {})
                            .get("result", {})
                        )
                        actual_host = creator.get("legacy", {}).get("screen_name")
                        if not actual_host:
                            admins = verified_space_data.get("participants", {}).get("admins", [])
                            if admins:
                                actual_host = admins[0].get("twitter_screen_name")
            except TimeoutError:
                graphql_failed = True
                log.warning(
                    "Host GraphQL verification timed out",
                    space_id=self.space_id,
                    timeout_seconds=20,
                )
            except Exception as e:
                graphql_failed = True
                log.warning(
                    "Host GraphQL verification failed",
                    space_id=self.space_id,
                    error_type=type(e).__name__,
                )

            if not actual_host:
                try:
                    participants = await asyncio.wait_for(self.adapter.getParticipants(), timeout=5.0)
                    for p in participants:
                        if p.role in ("host", "المضيف", "مضيف") and (p.username or p.display_name):
                            actual_host = p.username or p.display_name
                            break
                except Exception as e:
                    log.debug("Host DOM check error", error=str(e))

            with get_session() as db:
                monitored = db.query(MonitoredAccount).filter(MonitoredAccount.active.is_(True)).all()
                allowed_hosts = {a.username.lower().lstrip("@") for a in monitored}

            monitored_user = (self.host_username or "").lower().lstrip("@")
            host_user = (actual_host or "").lower().lstrip("@")
            is_auto_mode = monitored_user in ("auto", "auto_detect", "manual", "unknown", "")

            # Legacy auto mode may resolve a host after opening the player.
            # It must not mutate the operator's watched-account portfolio.
            if is_auto_mode and actual_host:
                self.host_username = actual_host
                monitored_user = host_user
                with contextlib.suppress(Exception):
                    self.paths = self.paths.update_host(f"@{actual_host}")

            if is_auto_mode:
                verified = bool(host_user and (self.manual_capture or host_user in allowed_hosts))
            else:
                verified = bool(
                    monitored_user
                    and host_user
                    and monitored_user == host_user
                    and (self.manual_capture or monitored_user in allowed_hosts)
                )
            if not verified:
                self.adapter.join_host_verification_state = "mismatch" if host_user else "unverified"
                if not host_user and graphql_failed:
                    self.join_outcome = "graphql_failure"
                elif not host_user:
                    try:
                        evidence = await self.adapter.get_space_end_evidence(
                            self.space_id, check_metadata=True
                        )
                        state = str(evidence.get("state") or "").upper()
                        self.adapter.join_space_state = state.lower()
                        if state == "ENDED":
                            self.join_outcome = "space_ended_before_join"
                        elif state in {"NOT_LIVE", "NOT_STARTED"}:
                            self.join_outcome = "space_not_live"
                        else:
                            self.join_outcome = "host_mismatch"
                    except Exception:
                        self.join_outcome = "host_mismatch"
                else:
                    self.join_outcome = "host_mismatch"
                self.join_last_error = (
                    f"expected @{monitored_user or 'unknown'}, observed @{host_user or 'unknown'}"
                )
                await self.report_join_progress(
                    "host_verification_failed",
                    {"host_verification_state": self.adapter.join_host_verification_state},
                )
                log.warning(
                    "Space host identity could not be verified; refusing to record to prevent a wrong-space capture.",
                    space_id=self.space_id,
                    actual_host=actual_host,
                    expected_host=self.host_username,
                )
                try:
                    if self.adapter._page:
                        leave_btn = await self.adapter._page.query_selector(
                            'button[aria-label*="Leave" i], button:has-text("Leave")'
                        )
                        if leave_btn:
                            await leave_btn.click()
                except Exception:
                    pass
                raise RuntimeError(
                    f"Space host verification failed for {self.space_id}: "
                    f"expected @{monitored_user or 'unknown'}, observed @{host_user or 'unknown'}"
                )
            self.adapter.join_host_verification_state = "verified"
            await self.report_join_progress("host_verified", {"host_verification_state": "verified"})

        # Use AudioSpaceById's title as the source of truth. The broad DOM
        # fallback often returns the host's name instead of the Space title.
        try:
            await self.report_join_progress("loading_space_metadata")
            meta = await self.adapter.getSpaceMetadata()
            gql_metadata = (verified_space_data or {}).get("metadata", {})
            if not isinstance(gql_metadata, dict):
                gql_metadata = {}
            title = str(gql_metadata.get("title") or "").strip()
            host_names = {self.host_username.strip().lstrip("@").casefold()}
            with get_session() as db:
                account = db.query(MonitoredAccount).filter_by(username=self.host_username).first()
                if account and account.display_name:
                    host_names.add(account.display_name.strip().lstrip("@").casefold())
            if not title:
                candidate = str(meta.get("title") or "").strip()
                if candidate and candidate.strip().lstrip("@").casefold() not in host_names:
                    title = candidate
            if title.casefold() in {"unknown", "عنوان غير مسجل", "مساحة مسجلة"} or re.fullmatch(
                r"\d{6}", title
            ):
                title = ""
            self.space_title = title or None
            if title:
                self.paths = self.paths.update_title(title)
                # Update title in database
                with get_session() as db:
                    sp = db.query(SpaceModel).filter_by(space_id=self.space_id).first()
                    if sp:
                        sp.title = title
                        sp.space_metadata = {
                            "title": title,
                            "state": gql_metadata.get("state"),
                            "started_at": gql_metadata.get("started_at"),
                            "ended_at": gql_metadata.get("ended_at"),
                            "total_live_listeners": gql_metadata.get("total_live_listeners"),
                        }
                        db.commit()
            else:
                # Clear the old false title if an earlier release stored the
                # host's account name in the Space title field.
                with get_session() as db:
                    sp = db.query(SpaceModel).filter_by(space_id=self.space_id).first()
                    if sp and sp.title and sp.title.strip().lstrip("@").casefold() in host_names:
                        sp.title = None
                        db.commit()
            if title:
                log.info("Space folder renamed to exact title", title=title, folder=str(self.paths.root.name))
        except Exception as e:
            log.debug("Title folder update attempt", error=str(e))

        # A functioning Pulse monitor can capture endless silence even when X
        # never opened an audio output stream. Verify the private browser route.
        from xsa.recorder.recorder import browser_audio_route_ready

        await self.report_join_progress("verifying_audio_route")
        sink = self.slot.sink_name if self.slot else "xspace"
        for _ in range(15):
            if await run_blocking(browser_audio_route_ready, sink):
                break
            await asyncio.sleep(1)
        else:
            self.join_outcome = "player_not_ready"
            self.join_last_error = "No verified Chrome audio output on the reserved capture sink"
            raise RuntimeError(self.join_last_error)

        # Start recording first segment
        existing_segments = [
            int(match.group(1))
            for path in self.paths.root.glob("recording_[0-9][0-9][0-9].mkv")
            if (match := re.fullmatch(r"recording_(\d{3})\.mkv", path.name))
        ]
        self._segment_index = max(existing_segments, default=-1) + 1
        segment_path = self.paths.segment(self._segment_index)
        await self.report_join_progress("starting_recorder")
        self._started_at = datetime.now(UTC)
        self._rec_wall_start = time.monotonic()
        self._segment_started_at = self._started_at
        self._persist_segment_start(segment_path)
        try:
            await self._recorder.start(segment_path, self._segment_index)
        except Exception as recorder_error:
            self._persist_segment_stop(segment_path, error=str(recorder_error))
            raise
        self._running = True
        # Do not create a visible Space folder until identity, listen access,
        # player readiness, and FFmpeg startup have all succeeded. Failed or
        # rejected joins should remain DB diagnostics, not look like recordings.
        self.paths.ensure()
        self._speaker_observation_file = self.paths.root / "speaker_observations.jsonl"
        self._speaker_event_file = self.paths.speaker_events_live
        try:
            # Empty files still make the collector's evidence paths explicit.
            self._speaker_observation_file.touch(exist_ok=True)
            self._speaker_event_file.touch(exist_ok=True)
            self.speaker_collector_health["speaker_collector_running"] = True
            log.info(
                "[SPEAKER] collector_started",
                space_id=self.space_id,
                observations=str(self._speaker_observation_file),
                events=str(self._speaker_event_file),
            )
        except OSError as exc:
            self._speaker_failure("collector output initialization failed", exc)
        if self._space_db_id is not None:
            try:
                with get_session() as db:
                    space = db.get(SpaceModel, self._space_db_id)
                    if space:
                        space.recording_started_at = self._started_at.replace(tzinfo=None)
                        if not space.started_at:
                            space.started_at = self._started_at.replace(tzinfo=None)
                        db.commit()
            except Exception as db_err:
                log.warning(
                    "Recording start metadata persistence failed", space_id=self.space_id, error=str(db_err)
                )
        log.info(
            "Space session started",
            space_id=self.space_id,
            host=self.host_username,
            segment=str(segment_path),
        )
        await self.report_join_progress("recording_started")

        # Take initial screenshot
        with contextlib.suppress(Exception):
            screenshot_path = self.paths.screenshots / f"join_{int(time.time())}.png"
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            await self.adapter.screenshot(screenshot_path)

    async def run_until_ended(self) -> None:
        """Poll explicit end evidence; sustained silence can bound capture but never proves Space end."""
        speaker_poll_interval = self.cfg.speaker_poll_interval_ms / 1000.0
        end_poll_interval = 1.0
        health_interval = 10.0
        gql_interval = 30.0  # seconds between GQL participant snapshots
        last_health = time.monotonic()
        last_browser_recovery = 0.0
        last_gql = 0.0
        last_speaker_poll = 0.0
        end_metadata_interval = 5.0
        last_end_metadata = 0.0
        silence_warning_logged = False
        space_state_unknown_since: float | None = None
        self._running = True

        while self._running:
            now = time.monotonic()

            # Check the locked Space before touching the UI or audio. A definitive
            # end returns immediately to the caller, which stops FFmpeg first.
            try:
                check_metadata = now - last_end_metadata >= end_metadata_interval
                if check_metadata:
                    last_end_metadata = now
                observation = await self.adapter.get_space_end_evidence(
                    self.space_id, check_metadata=check_metadata
                )
            except Exception as exc:
                observation = {"state": "DISCONNECTED", "source": "browser", "evidence": type(exc).__name__}
            detected_state = self.end_detector.observe(observation, now)
            if detected_state in (SpaceEndState.UNKNOWN, SpaceEndState.DISCONNECTED):
                if space_state_unknown_since is None:
                    space_state_unknown_since = now
            else:
                space_state_unknown_since = None
            if detected_state == SpaceEndState.ENDED:
                self.space_end_detected_at = datetime.fromisoformat(self.end_detector.detected_at)
                self.space_end_recording_time = self._current_offset_ms() / 1000.0
                self.space_end_source = str(observation.get("source") or "unknown")
                self.space_end_evidence = str(observation.get("evidence") or "")
                self.space_end_confidence = observation.get("confidence")
                log.warning(
                    "Locked X Space end confirmed; stopping capture immediately",
                    space_id=self.space_id,
                    source=self.space_end_source,
                    evidence=self.space_end_evidence,
                    detected_at=self.space_end_detected_at.isoformat(),
                    recording_time=self.space_end_recording_time,
                )
                self._running = False
                break

            silence = self._recorder.get_audio_silence_status()
            unknown_seconds = (
                max(0.0, now - space_state_unknown_since) if space_state_unknown_since is not None else 0.0
            )
            if should_stop_unverified_silence(
                silence_seconds=float(silence.get("silence_duration_seconds") or 0),
                space_state=detected_state,
                unknown_seconds=unknown_seconds,
            ):
                self._stop_reason = "space_state_unverified_after_silence"
                log.error(
                    "Stopping capture after prolonged silence and sustained inability to verify the locked Space",
                    space_id=self.space_id,
                    silence_seconds=silence["silence_duration_seconds"],
                    space_state=detected_state.value,
                    unknown_seconds=round(unknown_seconds, 1),
                )
                self._running = False
                break
            if silence.get("monitoring_available") is True and float(
                silence.get("silence_duration_seconds") or 0
            ) >= float(self.cfg.silence_capture_stop_seconds):
                # Silence can stop a capture that is no longer producing useful
                # media, but it must never be represented as proof that X ended
                # the Space. The Space state has already been checked above and
                # remains independently persisted by the explicit end detector.
                self._stop_reason = "audio_silence_timeout"
                log.error(
                    "Stopping capture after continuous measured audio silence; Space end remains unconfirmed",
                    space_id=self.space_id,
                    silence_seconds=silence.get("silence_duration_seconds"),
                    silence_limit_seconds=self.cfg.silence_capture_stop_seconds,
                    space_state=detected_state.value,
                )
                self._running = False
                break
            if float(silence.get("audio_monitoring_unavailable_seconds") or 0) >= float(
                self.cfg.silence_capture_stop_seconds
            ):
                self._stop_reason = "audio_monitoring_unavailable_timeout"
                log.error(
                    "Stopping capture because audio quality monitoring stayed unavailable",
                    space_id=self.space_id,
                    unavailable_seconds=silence.get("audio_monitoring_unavailable_seconds"),
                    unavailable_limit_seconds=self.cfg.silence_capture_stop_seconds,
                    space_state=detected_state.value,
                )
                self._running = False
                break
            if silence["silence_suspected"] and not silence_warning_logged:
                silence_warning_logged = True
                log.warning(
                    "Sustained audio silence; verifying locked Space state, recording remains active until explicit end evidence",
                    space_id=self.space_id,
                    silence_duration_seconds=silence["silence_duration_seconds"],
                    threshold_seconds=silence["silence_threshold_seconds"],
                )
                # Bypass the normal metadata interval once so silence immediately
                # prompts an exact-ID X state recheck. The regular independent
                # GraphQL/DOM checks and end debounce still decide whether to stop.
                try:
                    last_end_metadata = time.monotonic()
                    verification = await self.adapter.get_space_end_evidence(
                        self.space_id, check_metadata=True
                    )
                    detected_state = self.end_detector.observe(verification, time.monotonic())
                    if detected_state == SpaceEndState.ENDED:
                        self.space_end_detected_at = datetime.fromisoformat(self.end_detector.detected_at)
                        self.space_end_recording_time = self._current_offset_ms() / 1000.0
                        self.space_end_source = str(verification.get("source") or "unknown")
                        self.space_end_evidence = str(verification.get("evidence") or "")
                        self.space_end_confidence = verification.get("confidence")
                        self._running = False
                        break
                except Exception as exc:
                    log.info(
                        "Silence-triggered Space verification inconclusive; capture continues",
                        space_id=self.space_id,
                        error=type(exc).__name__,
                    )
            elif not silence["silence_suspected"]:
                silence_warning_logged = False

            evidence = observation.get("evidence")
            route_locked = not (evidence and str(evidence).startswith("space_id_mismatch"))
            if detected_state in (SpaceEndState.UNKNOWN, SpaceEndState.DISCONNECTED):
                if not route_locked:
                    # The dedicated browser has left its locked Space. Silence and
                    # pause page media immediately; route mismatch alone is not end proof.
                    await self.adapter.quarantine_space_media()
                elif detected_state == SpaceEndState.DISCONNECTED and now - last_browser_recovery >= 10.0:
                    last_browser_recovery = now
                    try:
                        recovered = await self.adapter.recoverSession()
                        log.info(
                            "Space browser recovery attempted", space_id=self.space_id, recovered=recovered
                        )
                        if not recovered and not await self._restart_segment(reason="browser_disconnected"):
                            self._stop_reason = self._stop_reason or "browser_recovery_failed"
                            self._running = False
                            break
                    except Exception as recovery_err:
                        log.warning(
                            "Space browser recovery failed",
                            space_id=self.space_id,
                            error=type(recovery_err).__name__,
                        )

            # Speaker polling is independent of end detection. No voice activity
            # result is used to stop or extend a recording.
            if route_locked and now - last_speaker_poll >= speaker_poll_interval:
                last_speaker_poll = now
                try:
                    await self.adapter.focusSpeakersView()
                    use_gql = now - last_gql >= gql_interval
                    if use_gql:
                        last_gql = now
                    await self._collect_speaker_observation(use_gql=use_gql)
                except Exception as e:
                    self._speaker_failure("participant observation failed", e)

            # --- recorder health check ---
            if now - last_health >= health_interval:
                last_health = now
                if not self._recorder.health():
                    log.error("Recorder health check failed — restarting segment")
                    if not await self._restart_segment():
                        self._stop_reason = self._stop_reason or "recorder_restart_failed"
                        log.error(
                            "Capture stopped because the replacement FFmpeg segment did not become healthy",
                            space_id=self.space_id,
                        )
                        self._running = False
                        break

            await asyncio.sleep(end_poll_interval)

    async def stop(self, *, reason: str | None = None) -> dict:
        """Stop once even when shutdown and the session task overlap."""
        # Signal recovery before waiting on its lock so it cannot start a new
        # FFmpeg after an operator/shutdown stop has already been requested.
        self._running = False
        async with self._stop_lock:
            if self._stop_summary is not None:
                return self._stop_summary
            self._stop_reason = reason or getattr(self, "_stop_reason", None)
            summary = await self._stop_impl()
            self._stop_summary = summary
            return summary

    async def abort_join(self) -> None:
        """Cancel a pre-recording join without writing an empty recording document."""
        self._running = False
        proc = getattr(self._recorder, "_proc", None)
        if proc is not None and proc.poll() is None:
            await self._recorder.stop()

    async def _finalize_recording_media(self, final_file: Path | None) -> Path | None:
        """Run blocking FFmpeg/ffprobe work off the API event loop."""
        if not final_file:
            return None
        from xsa.recorder.media import (
            create_playback_mp4,
            create_thumbnail,
            finalize_recording,
            has_audio_video,
            playback_readiness_for_paths,
        )

        segments = sorted(self.paths.root.glob("recording_[0-9][0-9][0-9].mkv"))
        if len(segments) > 1:
            merged = await run_blocking(finalize_recording, self.paths, segments)
            if merged:
                final_file = merged
                log.info("Recording segments finalized", file=str(merged), count=len(segments))
            else:
                log.error("Could not merge recording segments; keeping original segments")

        source_path = Path(final_file)
        if not source_path.exists():
            return final_file
        if getattr(self, "_stop_reason", None):
            manifest_path = self.paths.root / "recording_manifest.json"
            try:
                manifest = (
                    json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
                )
                manifest["status"] = "PARTIAL"
                manifest["capture_stop_reason"] = self._stop_reason
                manifest["explicit_space_end_confirmed"] = self.space_end_detected_at is not None
                temp_manifest = manifest_path.with_suffix(".json.tmp")
                temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                temp_manifest.replace(manifest_path)
            except (OSError, UnicodeError, json.JSONDecodeError) as manifest_err:
                log.warning(
                    "Could not mark interrupted recording manifest as partial",
                    space_id=self.space_id,
                    error_type=type(manifest_err).__name__,
                )
        try:
            source_valid = await run_blocking(has_audio_video, source_path)
            playback_valid = await run_blocking(has_audio_video, self.paths.playback)
            if not source_valid:
                log.error("Recording source is missing audio or video", file=str(final_file))
            elif not playback_valid or not (await run_blocking(playback_readiness_for_paths, self.paths))[0]:
                if await run_blocking(create_playback_mp4, source_path, self.paths.playback):
                    log.info("Auto-remuxed recording to faststart MP4", mp4=str(self.paths.playback))
                else:
                    log.error("MP4 remux failed; the original MKV remains available", source=str(final_file))

            if not self.paths.thumbnail.exists() and await run_blocking(
                create_thumbnail, source_path, self.paths.thumbnail
            ):
                log.info("Auto-generated thumbnail", thumb=str(self.paths.thumbnail))
        except Exception as media_err:
            log.warning("Post-session media generation error", error=str(media_err))
        return final_file

    async def _stop_impl(self) -> dict:
        """Stop recording and return a summary dict."""
        self._running = False
        self.speaker_collector_health["speaker_collector_running"] = False
        self._ended_at = self.space_end_detected_at
        if not self._ended_at and not self._stop_reason:
            self._stop_reason = "interrupted_unverified"

        # Critical ordering: FFmpeg must be gone before any browser cleanup,
        # database work, media merge, or post-processing can begin.
        final_file = await self._stop_capture_before_browser_cleanup()

        # Emit final speaker_stopped if someone was speaking
        if self._last_active_speaker:
            stop_offset_ms = self._current_offset_ms()
            started_offset_ms = (
                int(self._speaker_start_offset) if self._speaker_start_offset is not None else stop_offset_ms
            )
            duration_ms = max(0, stop_offset_ms - started_offset_ms)
            username = self._last_active_speaker
            self._speaker_durations[username] = self._speaker_durations.get(username, 0) + duration_ms
            await self._emit_speaker_event(
                "speaker_stopped",
                username,
                offset_ms=stop_offset_ms,
                extra={"duration_ms": duration_ms},
            )
            self._last_active_speaker = None
            self._speaker_start_offset = None

        if self._space_db_id is not None:
            try:
                with get_session() as db:
                    space = db.get(SpaceModel, self._space_db_id)
                    if space:
                        space.recording_ended_at = self.recording_stopped_at.replace(tzinfo=None)
                        db.commit()
            except Exception as db_err:
                log.warning(
                    "Recording end metadata persistence failed", space_id=self.space_id, error=str(db_err)
                )

        # Preserve every segment if FFmpeg had to restart during the Space.
        # Long media finalization remains awaitable without starving API/health routes.
        final_file = await self._finalize_recording_media(final_file)

        # `COMPLETE` in the media manifest means the container/segments are
        # structurally readable. Persist independent audio-quality evidence so
        # a valid but silent file is not presented as a successful capture.
        manifest_path = self.paths.root / "recording_manifest.json"
        try:
            manifest = (
                json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
            )
            silence = self._recorder.get_audio_silence_status()
            manifest["status_scope"] = "container_and_stream_integrity_only"
            manifest["quality"] = {
                "audio_measurement": silence.get("audio_measurement", "unavailable"),
                "audio_input_ready": silence.get("audio_input_ready", False),
                "audio_silent_at_stop": silence.get("audio_silent"),
                "silence_duration_seconds": silence.get("silence_duration_seconds", 0.0),
                "audio_monitoring_unavailable_seconds": silence.get(
                    "audio_monitoring_unavailable_seconds", 0.0
                ),
                "silence_stop_threshold_seconds": silence.get("capture_stop_seconds"),
                "capture_stop_reason": self._stop_reason or "explicit_space_end",
                "explicit_space_end_confirmed": self.space_end_detected_at is not None,
            }
            temp_manifest = manifest_path.with_suffix(".json.tmp")
            temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_manifest.replace(manifest_path)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as manifest_err:
            log.warning(
                "Could not persist recording quality evidence",
                space_id=self.space_id,
                error_type=type(manifest_err).__name__,
            )

        # Take final screenshot
        with contextlib.suppress(Exception):
            screenshot_path = self.paths.screenshots / f"end_{int(time.time())}.png"
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            await self.adapter.screenshot(screenshot_path)

        # Fetch final metadata
        meta = {}
        with contextlib.suppress(Exception):
            meta = await self.adapter.getSpaceMetadata()

        # Write space.json
        riyadh_tz = timezone(timedelta(hours=3), "Asia/Riyadh")
        start_local = self._started_at.astimezone(riyadh_tz) if self._started_at else None
        end_local = self._ended_at.astimezone(riyadh_tz) if self._ended_at else None
        host_display_name = self.host_username
        host_avatar_url = ""
        try:
            with get_session() as db:
                account = db.query(MonitoredAccount).filter_by(username=self.host_username).first()
                if account:
                    host_display_name = account.display_name or account.username
                    host_avatar_url = account.avatar_url or ""
        except Exception as host_meta_err:
            log.debug("Host display metadata unavailable", space_id=self.space_id, error=str(host_meta_err))
        summary = {
            **meta,
            "space_id": self.space_id,
            "title": self.space_title or "",
            "url": self.space_url,
            "folder_relative": self.paths.root.relative_to(self.cfg.data_dir).as_posix(),
            "timezone": "Asia/Riyadh",
            "host": self.host_username,
            "host_display_name": host_display_name,
            "host_avatar_url": host_avatar_url,
            "icon_path": "thumbnail.jpg" if self.paths.thumbnail.is_file() else "",
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "started_at_local": start_local.isoformat() if start_local else None,
            "ended_at": self._ended_at.isoformat() if self._ended_at else None,
            "ended_at_local": end_local.isoformat() if end_local else None,
            "space_end": {
                **self.end_detector.snapshot(),
                "audio_silence": self._recorder.get_audio_silence_status(),
                "development_interrupted": bool(
                    self._stop_reason
                    and self._stop_reason
                    not in {
                        "space_state_unverified_after_silence",
                        "audio_silence_timeout",
                        "audio_monitoring_unavailable_timeout",
                    }
                ),
                "capture_stop_reason": self._stop_reason or "explicit_space_end",
                "interruption_reason": self._stop_reason,
                "detected_at": self.space_end_detected_at.isoformat() if self.space_end_detected_at else None,
                "recording_time": self.space_end_recording_time,
                "source": self.space_end_source,
                "evidence": self.space_end_evidence,
                "confidence": self.space_end_confidence,
                "recording_stop_at": self.recording_stopped_at.isoformat()
                if self.recording_stopped_at
                else None,
                "recording_stop_recording_time": self.recording_stop_recording_time,
                "end_tail_duration": self.end_tail_duration,
            },
            "recording_segments": self._segment_index + 1,
            "capture_recoveries": self._capture_recoveries,
            "final_file": str(final_file),
            "recording_interruption": (
                {
                    "classification": (
                        self._stop_reason
                        if self._stop_reason
                        in {
                            "space_state_unverified_after_silence",
                            "audio_silence_timeout",
                            "audio_monitoring_unavailable_timeout",
                        }
                        else "development_interrupted"
                    ),
                    "reason": self._stop_reason,
                    "stopped_at": self.recording_stopped_at.isoformat()
                    if self.recording_stopped_at
                    else None,
                }
                if self._stop_reason
                else None
            ),
            "speaker_stats": {
                username: {
                    "speaking_ms": self._speaker_durations.get(username, 0),
                    "speaking_seconds": round(self._speaker_durations.get(username, 0) / 1000, 1),
                    "turn_count": self._speaker_turn_counts.get(username, 0),
                }
                for username in set(
                    list(self._speaker_durations.keys()) + list(self._speaker_turn_counts.keys())
                )
            },
        }
        try:
            temp_summary = self.paths.space_json.with_suffix(".json.tmp")
            temp_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_summary.replace(self.paths.space_json)
        except Exception as meta_err:
            log.warning("Space metadata save failed", space_id=self.space_id, error=str(meta_err))
        try:
            info_page = write_space_info_html(self.paths.root, summary)
            log.info("Space info document saved", space_id=self.space_id, path=str(info_page))
        except Exception as page_err:
            log.warning("Space info document save failed", space_id=self.space_id, error=str(page_err))
        try:
            events = []
            if self.paths.speaker_events_live.is_file():
                with self.paths.speaker_events_live.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        if line.strip():
                            try:
                                events.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            from xsa.speaker_timeline import build_speaker_timeline

            timeline = build_speaker_timeline(
                space_id=self.space_id,
                title=summary.get("title"),
                host=self.host_username,
                space_url=self.space_url,
                detected_at=self._started_at,
                started_at=self._started_at,
                ended_at=self._ended_at,
                events=events,
                folder=self.paths.root,
                data_dir=self.cfg.data_dir,
            )
            temp_timeline = self.paths.speaker_timeline_json.with_suffix(".json.tmp")
            temp_timeline.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_timeline.replace(self.paths.speaker_timeline_json)
        except Exception as timeline_err:
            log.warning(
                "Speaker timeline report save failed", space_id=self.space_id, error=str(timeline_err)
            )
        try:
            await self.adapter.close_space_page(self.space_id)
        except Exception as browser_err:
            log.warning(
                "Dedicated Space page cleanup failed",
                space_id=self.space_id,
                error=type(browser_err).__name__,
            )
        # Slot zero also owns an isolated worker browser.
        browser_closed = True
        if self.slot:
            try:
                await self.adapter.close()
                log.info("Worker browser adapter closed", slot_id=self.slot.slot_id)
            except Exception as e:
                browser_closed = False
                log.warning("Worker adapter close error", error=str(e))

        # Release the virtual display / audio slot
        if self.slot and browser_closed:
            try:
                from xsa.recorder.slot_pool import get_slot_pool

                pool = get_slot_pool()
                if await pool.release_slot(self.slot):
                    log.info("Released display slot", slot_id=self.slot.slot_id)
                    # The same slot object may immediately be reserved by a new
                    # session. The monitor's final cleanup must not release it again.
                    self.slot = None
            except Exception as e:
                log.warning("Slot release error", error=str(e))

        return summary

    async def _stop_capture_before_browser_cleanup(self) -> Path | None:
        """Stop FFmpeg first, timestamp it, then silence the dedicated browser."""
        final_file = await self._recorder.stop()
        self.recording_stopped_at = datetime.now(UTC)
        self.recording_stop_recording_time = self._current_offset_ms() / 1000.0
        self._persist_segment_stop(final_file)
        if self.space_end_recording_time is not None:
            self.end_tail_duration = max(
                0.0, self.recording_stop_recording_time - self.space_end_recording_time
            )
        try:
            await self.adapter.quarantine_space_media()
        except Exception as browser_err:
            log.warning(
                "Browser media quarantine failed after recorder stop",
                space_id=self.space_id,
                error=type(browser_err).__name__,
            )
        self._persist_space_end_events()
        return final_file

    def _persist_segment_start(self, segment_path: Path) -> None:
        """Persist segment ownership before FFmpeg starts so crashes are visible."""
        if self._space_db_id is None:
            return
        started = self._segment_started_at or datetime.now(UTC)
        try:
            with get_session() as db:
                row = (
                    db.query(RecordingSegment)
                    .filter_by(space_id=self._space_db_id, segment_index=self._segment_index)
                    .one_or_none()
                )
                if row is None:
                    row = RecordingSegment(
                        space_id=self._space_db_id,
                        segment_index=self._segment_index,
                        file_path=str(segment_path),
                        state="recording",
                        started_at=started.replace(tzinfo=None),
                    )
                    db.add(row)
                else:
                    row.file_path = str(segment_path)
                    row.state = "recording"
                    row.started_at = started.replace(tzinfo=None)
                    row.ended_at = None
                    row.error = None
                db.commit()
        except Exception as db_err:
            log.error(
                "Recording segment start persistence failed",
                space_id=self.space_id,
                segment=self._segment_index,
                error_type=type(db_err).__name__,
            )

    def _persist_segment_stop(self, segment_path: Path | None, *, error: str | None = None) -> None:
        """Persist final segment size and terminal state without hiding failures."""
        if getattr(self, "_space_db_id", None) is None:
            return
        stopped = self.recording_stopped_at or datetime.now(UTC)
        path = segment_path or self.paths.segment(self._segment_index)
        try:
            size = path.stat().st_size if path.is_file() else 0
            status = self._recorder.get_status()
            clean_exit = status.state == "idle"
            with get_session() as db:
                row = (
                    db.query(RecordingSegment)
                    .filter_by(space_id=self._space_db_id, segment_index=self._segment_index)
                    .one_or_none()
                )
                if row is None:
                    row = RecordingSegment(
                        space_id=self._space_db_id,
                        segment_index=self._segment_index,
                        file_path=str(path),
                        started_at=(self._segment_started_at or stopped).replace(tzinfo=None),
                    )
                    db.add(row)
                row.file_path = str(path)
                row.ended_at = stopped.replace(tzinfo=None)
                row.duration_seconds = max(
                    0.0, (stopped - (self._segment_started_at or stopped)).total_seconds()
                )
                row.file_bytes = size
                row.state = "complete" if clean_exit and size > 0 else "corrupt"
                row.error = (
                    (error or status.error or ("empty recording segment" if size == 0 else None))[:1200]
                    if (error or status.error or size == 0)
                    else None
                )
                db.commit()
        except Exception as db_err:
            log.error(
                "Recording segment finalization persistence failed",
                space_id=self.space_id,
                segment=self._segment_index,
                error_type=type(db_err).__name__,
            )

    def _persist_space_end_events(self) -> None:
        """Persist detected end and FFmpeg stop timestamps without delaying FFmpeg stop."""
        payloads = []
        if self.space_end_detected_at:
            payloads.append(
                {
                    "event": "space_ended",
                    "space_id": self.space_id,
                    "recording_time": self.space_end_recording_time,
                    "wall_clock_time": self.space_end_detected_at.isoformat(),
                    "source": self.space_end_source,
                    "evidence": self.space_end_evidence,
                    "confidence": self.space_end_confidence,
                }
            )
        payloads.append(
            {
                "event": "recording_stopped",
                "space_id": self.space_id,
                "recording_time": self.recording_stop_recording_time,
                "wall_clock_time": self.recording_stopped_at.isoformat()
                if self.recording_stopped_at
                else datetime.now(UTC).isoformat(),
                "source": "ffmpeg",
                "reason": self._stop_reason or "explicit_space_end",
                "explicit_space_end_confirmed": self.space_end_detected_at is not None,
                "space_state": self.end_detector.state.value,
                "end_tail_duration": self.end_tail_duration,
            }
        )
        for payload in payloads:
            try:
                with self.paths.space_end_events.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except OSError as exc:
                log.error(
                    "Space end event file persistence failed",
                    space_id=self.space_id,
                    error=type(exc).__name__,
                )
            if self._space_db_id is not None:
                try:
                    with get_session() as db:
                        db.add(
                            SystemEvent(
                                event_time=datetime.fromisoformat(payload["wall_clock_time"]).replace(
                                    tzinfo=None
                                ),
                                category="recording",
                                level=("info" if payload["event"] == "space_ended" else "warning"),
                                message=payload["event"],
                                space_id=self._space_db_id,
                                details=payload,
                            )
                        )
                        db.commit()
                except Exception as db_err:
                    log.warning(
                        "Recording end/stop event DB persistence failed",
                        space_id=self.space_id,
                        event=payload["event"],
                        error=type(db_err).__name__,
                    )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _poll_speakers(self) -> None:
        """
        Check current speaker state and emit events on changes.
        Also tracks cumulative speaking duration per user.
        """
        active = await self.adapter.getActiveSpeaker()
        active_username = active.username if active else None

        if active_username != self._last_active_speaker:
            now_offset = self._current_offset_ms()

            # Someone stopped speaking
            if self._last_active_speaker:
                duration_ms = 0
                if self._speaker_start_offset is not None:
                    duration_ms = now_offset - self._speaker_start_offset
                    # Accumulate speaking time
                    prev = self._last_active_speaker
                    self._speaker_durations[prev] = self._speaker_durations.get(prev, 0) + duration_ms
                await self._emit_speaker_event(
                    "speaker_stopped",
                    self._last_active_speaker,
                    offset_ms=now_offset,
                    extra={"duration_ms": duration_ms},
                )
                self._speaker_start_offset = None

            # Someone started speaking
            if active_username:
                self._speaker_start_offset = now_offset
                self._speaker_turn_counts[active_username] = (
                    self._speaker_turn_counts.get(active_username, 0) + 1
                )
                await self._emit_speaker_event(
                    "speaker_started", active_username, participant=active, offset_ms=now_offset
                )

            self._last_active_speaker = active_username

    async def _poll_participants_gql(self) -> None:
        """
        Periodic GQL snapshot: fetch all participants and log any new speakers/hosts.
        This catches participants we may have missed via DOM polling.
        """
        try:
            speakers = await self.adapter.getSpeakersGQL(self.space_id)
            if not speakers:
                return
            now_offset = self._current_offset_ms()
            speaker_names = [p.username for p in speakers if p.username]
            log.info(
                "GQL participant snapshot",
                space_id=self.space_id,
                speakers=speaker_names,
                count=len(speakers),
                offset_ms=now_offset,
            )
            # Write snapshot to JSONL as a 'participant_snapshot' event
            if self._speaker_event_file:
                import json as _json

                event = {
                    "event": "participant_snapshot",
                    "timestamp": now_offset / 1000.0,
                    "wall_time": datetime.now(UTC).isoformat(),
                    "participants": [
                        {
                            "username": p.username,
                            "display_name": p.display_name,
                            "role": p.role,
                            "avatar": p.avatar_url,
                            "is_muted": p.is_muted,
                            "source": p.source,
                        }
                        for p in speakers
                    ],
                    "source": "gql",
                }
                with self._speaker_event_file.open("a", encoding="utf-8") as f:
                    f.write(_json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            self._speaker_failure("GQL participant snapshot failed", e)

    async def _collect_speaker_observation(self, *, use_gql: bool) -> None:
        """Persist one raw participant snapshot and any debounced role changes."""
        source = "x_gql" if use_gql else "x_dom"
        participants = await (
            self.adapter.getParticipantsGQL(self.space_id) if use_gql else self.adapter.getParticipants()
        )
        source = getattr(self.adapter, "_last_participant_source", "x_gql" if use_gql else "x_dom")
        now = datetime.now(UTC)
        offset_ms = self._current_offset_ms()
        payloads = [participant_payload(p) for p in participants]
        # Empty and partial DOM/GQL views are not complete participant rosters;
        # do not infer that someone left from absence in these snapshots.
        observation, events = self._speaker_tracker.observe(
            payloads,
            observed_at=now,
            recording_time=offset_ms / 1000.0,
            source=source,
            complete_snapshot=False,
            active_indicator_validated=False,
        )
        # Retain the Phase B timebase key while making its launch-anchor
        # uncertainty explicit (see docs/TIMEBASE.md).
        observation["timestamp"] = observation["recording_time"]

        # If session is running in auto/manual mode, adopt discovered host dynamically
        if (self.host_username or "").lower() in ("auto", "auto_detect", "manual", "unknown", ""):
            for p in participants:
                role = getattr(p, "role", "") or ""
                uname = getattr(p, "username", "") or ""
                if role in ("host", "المضيف", "مضيف") and uname and uname.lower() not in ("auto", "unknown"):
                    self._adopt_discovered_host(uname, getattr(p, "display_name", None))
                    break
        try:
            if not self._speaker_observation_file:
                raise OSError("observation file path is unavailable")
            append_jsonl(self._speaker_observation_file, observation)
            self.speaker_collector_health["observations"] += 1
            previous_failure = self._speaker_failure_started_at
            if previous_failure:
                gap = {
                    "schema_version": 1,
                    "space_id": self.space_id,
                    "recording_time": offset_ms / 1000.0,
                    "timestamp": offset_ms / 1000.0,
                    "space_time": None,
                    "wall_time": observation["observed_at"],
                    "event": "observation_gap",
                    "gap_start": previous_failure,
                    "gap_end": observation["observed_at"],
                    "reason": self.speaker_collector_health.get("last_error") or "collector_unavailable",
                    "source": source,
                    "confidence": None,
                }
                self._speaker_failure_started_at = None
                await self._persist_observed_event(gap)
            self.speaker_collector_health["last_success_at"] = observation["observed_at"]
            self.speaker_collector_health["consecutive_failures"] = 0
            self.speaker_collector_health["last_error"] = None
            self.speaker_collector_health["last_error_category"] = None
            self.speaker_collector_health["speaker_collector_running"] = True
            if payloads:
                self.speaker_collector_health["consecutive_empty_observations"] = 0
                self.speaker_collector_health["participants_seen_total"] = len(self._speaker_tracker._state)
                self.speaker_collector_health["last_participant_at"] = observation["observed_at"]
                self.speaker_collector_health["coverage_state"] = "partial"
                self.speaker_collector_health["quality_state"] = "partial"
            else:
                empty_count = self.speaker_collector_health["consecutive_empty_observations"] + 1
                self.speaker_collector_health["consecutive_empty_observations"] = empty_count
                if empty_count >= 3:
                    self.speaker_collector_health["coverage_state"] = "unavailable"
                    self.speaker_collector_health["quality_state"] = "unavailable"
                elif self.speaker_collector_health["observations"] <= 1:
                    self.speaker_collector_health["coverage_state"] = "unknown"
                    self.speaker_collector_health["quality_state"] = "unknown"
            log.debug(
                "[SPEAKER] poll_success",
                space_id=self.space_id,
                participants=len(payloads),
                source=source,
                offset_ms=offset_ms,
            )
            if payloads:
                log.debug(
                    "[SPEAKER] participants_seen", space_id=self.space_id, count=len(payloads), source=source
                )
            elif source == "x_dom":
                log.warning(
                    "[SPEAKER] selector_unavailable",
                    space_id=self.space_id,
                    source=source,
                    coverage_state=self.speaker_collector_health["coverage_state"],
                )
        except Exception as exc:
            self._speaker_failure("observation JSONL write failed", exc)
        if self._space_db_id is not None and payloads:
            try:
                with get_session() as db:
                    for payload in payloads:
                        persist_space_participant(
                            db,
                            space_db_id=self._space_db_id,
                            evidence=payload,
                            observed_at=now.replace(tzinfo=None),
                        )
                    db.commit()
            except Exception as exc:
                self._speaker_failure("participant database persistence failed", exc)
        for event in events:
            event["timestamp"] = event["recording_time"]
            await self._persist_observed_event(event)

    async def _persist_observed_event(self, event: dict[str, Any]) -> None:
        """Write JSONL and DB independently; a failure in one never skips the other."""
        event_path = self._speaker_event_file
        try:
            if not event_path:
                raise OSError("event file path is unavailable")
            append_jsonl(event_path, event)
            self.speaker_collector_health["events"] += 1
            self.speaker_collector_health["last_event_at"] = event.get("wall_time")
            log.info(
                "[SPEAKER] event_written",
                space_id=self.space_id,
                event=event.get("event"),
                username=event.get("username"),
            )
        except Exception as exc:
            self._speaker_failure("event JSONL write failed", exc)
        if self._space_db_id is not None:
            try:
                with get_session() as db:
                    observed_at = datetime.fromisoformat(event["wall_time"].replace("Z", "+00:00"))
                    if observed_at.tzinfo is not None:
                        observed_at = observed_at.astimezone(UTC).replace(tzinfo=None)
                    participant, _, _, _ = persist_space_participant(
                        db,
                        space_db_id=self._space_db_id,
                        evidence=event,
                        observed_at=observed_at,
                    )
                    db.add(
                        SpeakerEvent(
                            space_id=self._space_db_id,
                            recording_offset_ms=round(event["recording_time"] * 1000),
                            wall_time=observed_at,
                            event_type=event["event"],
                            participant_id=participant.id if participant else None,
                            username=event.get("username"),
                            x_user_id=event.get("x_user_id"),
                            source=event.get("source", "x_dom"),
                            confidence=event.get("confidence"),
                            raw_data={
                                k: v
                                for k, v in event.items()
                                if k
                                not in {
                                    "schema_version",
                                    "space_id",
                                    "recording_time",
                                    "timestamp",
                                    "wall_time",
                                    "event",
                                    "username",
                                    "source",
                                    "confidence",
                                }
                            },
                        )
                    )
                    db.commit()
            except Exception as exc:
                self._speaker_failure("event database write failed", exc)

    def _speaker_failure(self, message: str, error: Exception) -> None:
        if self._speaker_failure_started_at is None:
            self._speaker_failure_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.speaker_collector_health["consecutive_failures"] += 1
        self.speaker_collector_health["last_error"] = str(error)
        self.speaker_collector_health["last_error_category"] = type(error).__name__
        self.speaker_collector_health["coverage_state"] = "unavailable"
        self.speaker_collector_health["quality_state"] = "unavailable"
        log.warning(
            "[SPEAKER] collector_error",
            space_id=self.space_id,
            message=message,
            error=str(error),
            consecutive_failures=self.speaker_collector_health["consecutive_failures"],
        )

    async def _emit_speaker_event(
        self,
        event_type: str,
        username: str,
        participant: SpaceParticipantInfo | None = None,
        offset_ms: int | None = None,
        extra: dict | None = None,
    ) -> None:
        event = {
            "timestamp": offset_ms / 1000.0 if offset_ms is not None else None,
            "wall_time": datetime.now(UTC).isoformat(),
            "event": event_type,
            "username": username,
            "display_name": participant.display_name if participant else "",
            "avatar": participant.avatar_url if participant else "",
            "role": participant.role if participant else "unknown",
            "source": participant.source if participant else "x_dom",
            **(extra or {}),
        }
        if self._speaker_event_file:
            with self._speaker_event_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self._space_db_id is not None:
            try:
                with get_session() as db:
                    observed_at = datetime.now(UTC).replace(tzinfo=None)
                    participant_row, _, _, _ = persist_space_participant(
                        db,
                        space_db_id=self._space_db_id,
                        evidence={
                            "username": username,
                            "display_name": event["display_name"],
                            "avatar_url": event["avatar"],
                            "role": event["role"],
                            "x_user_id": participant.x_user_id if participant else None,
                            "source": event["source"],
                            "is_muted": participant.is_muted if participant else None,
                        },
                        observed_at=observed_at,
                    )
                    db.add(
                        SpeakerEvent(
                            space_id=self._space_db_id,
                            recording_offset_ms=offset_ms,
                            wall_time=observed_at,
                            event_type=event_type,
                            participant_id=participant_row.id if participant_row else None,
                            username=username,
                            source=event["source"],
                            raw_data={
                                "display_name": event["display_name"],
                                "role": event["role"],
                                "avatar": event["avatar"],
                                **(extra or {}),
                            },
                        )
                    )
                    db.commit()
            except Exception as db_err:
                log.warning("Speaker event DB persistence failed", space_id=self.space_id, error=str(db_err))
        log.info("Speaker event", **{k: v for k, v in event.items() if k != "avatar"})

    async def _restart_segment(self, *, reason: str | None = None) -> bool:
        """Recover only this session, preserving media and the slot reservation."""
        async with self._stop_lock:
            if not self._running:
                return False
            if len(self._capture_recoveries) >= self.cfg.max_capture_recoveries:
                self._stop_reason = "capture_recovery_exhausted"
                return False
            began = time.monotonic()
            event: dict[str, Any] = {
                "space_id": self.space_id,
                "attempt": len(self._capture_recoveries) + 1,
                "previous_segment": self._segment_index,
                "detected_at": datetime.now(UTC).isoformat(),
                "status": "recovering",
                "reason": reason or self._recorder.get_status().error or "recorder_unhealthy",
                "loss_started_at": None,
                "timing_basis": "wall_clock; exact pre-detection loss is unknown",
                "resources_recreated": False,
            }
            self._capture_recoveries.append(event)
            self._persist_capture_recovery(event)
            try:
                async with asyncio.timeout(self.cfg.capture_recovery_timeout_seconds):
                    old_path = self._recorder.get_current_file()
                    # No resource/browser teardown is permitted until exit is
                    # confirmed by Recorder.stop(). Its failure quarantines us.
                    await self._recorder.stop()
                    event["confirmed_capture_stopped_at"] = datetime.now(UTC).isoformat()
                    self._persist_segment_stop(old_path)
                    if not self.slot:
                        raise RuntimeError("Automatic recovery requires an isolated capture slot")
                    try:
                        await run_blocking(self.slot.check_resources)
                        resources_ok = True
                    except Exception:
                        resources_ok = False
                    observation = {}
                    if resources_ok:
                        with contextlib.suppress(Exception):
                            observation = await self.adapter.get_space_end_evidence(
                                self.space_id, check_metadata=True
                            )
                    if not resources_ok or observation.get("state") != "LIVE":
                        # Keep the private profile/cookies, but never launch a
                        # second browser if closing the old one fails.
                        await self.adapter.close(remove_profile=False)
                        if not resources_ok:
                            from xsa.recorder.slot_pool import get_slot_pool

                            await get_slot_pool().recover_slot(self.slot)
                            event["resources_recreated"] = True
                        if not self._running:
                            event["status"] = "cancelled"
                            return False
                        await self.adapter.start()
                        joined = await self.adapter.openSpace(
                            self.space_url,
                            prefer_anonymous=self.cfg.prefer_anonymous_listening,
                            allow_authenticated_fallback=self.cfg.allow_authenticated_fallback,
                        )
                        if not joined:
                            raise RuntimeError("Recovery did not pass Space listening/identity checks")
                        observation = await self.adapter.get_space_end_evidence(
                            self.space_id, check_metadata=True
                        )
                    if (
                        observation.get("state") != "LIVE"
                        or str(observation.get("evidence", "")).startswith("space_id_mismatch")
                        or self.adapter._active_space_id != self.space_id
                    ):
                        raise RuntimeError("Recovery could not verify the original live Space")
                    from xsa.recorder.recorder import browser_audio_route_ready

                    for _ in range(15):
                        if await run_blocking(browser_audio_route_ready, self.slot.sink_name):
                            break
                        await asyncio.sleep(1)
                    else:
                        raise RuntimeError("Recovery has no browser audio on the private capture sink")
                    if not self._running:
                        event["status"] = "cancelled"
                        return False
                    self._segment_index += 1
                    segment_path = self.paths.segment(self._segment_index)
                    while segment_path.exists():
                        self._segment_index += 1
                        segment_path = self.paths.segment(self._segment_index)
                    self._segment_started_at = datetime.now(UTC)
                    self._persist_segment_start(segment_path)
                    try:
                        await self._recorder.start(segment_path, self._segment_index)
                        if not self._recorder.health():
                            raise RuntimeError("Replacement recorder did not become healthy")
                    except Exception as exc:
                        self._persist_segment_stop(segment_path, error=str(exc))
                        raise
                    event["replacement_segment"] = self._segment_index
                    event["capture_resumed_at"] = datetime.now(UTC).isoformat()
                    event["confirmed_gap_seconds"] = round(
                        (
                            datetime.fromisoformat(event["capture_resumed_at"])
                            - datetime.fromisoformat(event["confirmed_capture_stopped_at"])
                        ).total_seconds(),
                        3,
                    )
                    event["status"] = "recovered"
                    return True
            except asyncio.CancelledError:
                event["status"] = "cancelled"
                raise
            except Exception as exc:
                event["status"] = "failed"
                event["error_type"] = type(exc).__name__
                event["error"] = str(exc)[:500]
                log.error(
                    "Capture recovery failed; prior segments retained",
                    space_id=self.space_id,
                    error_type=type(exc).__name__,
                )
                return False
            finally:
                event["finished_at"] = datetime.now(UTC).isoformat()
                event["recovery_seconds"] = round(time.monotonic() - began, 3)
                self._persist_capture_recovery(event)

    def _persist_capture_recovery(self, event: dict[str, Any]) -> None:
        """Append both attempt and result so a second crash leaves evidence."""
        try:
            append_jsonl(self.paths.root / "capture_recoveries.jsonl", event)
            if self._space_db_id is not None:
                with get_session() as db:
                    db.add(
                        SystemEvent(
                            space_id=self._space_db_id,
                            category="recording",
                            level="info" if event["status"] == "recovered" else "warning",
                            message=f"Capture recovery {event['status']}",
                            details=dict(event),
                        )
                    )
                    db.commit()
        except Exception as exc:
            log.warning("Capture recovery evidence persistence failed", error_type=type(exc).__name__)

    def _adopt_discovered_host(self, host_username: str, display_name: str | None = None) -> None:
        """Dynamically update Space session and database record when host is detected post-join."""
        clean_host = (host_username or "").lstrip("@").strip()
        if not clean_host or clean_host.lower() in ("auto", "auto_detect", "manual", "unknown"):
            return
        self.host_username = clean_host
        try:
            with get_session() as db:
                from sqlalchemy import func

                acc = (
                    db.query(MonitoredAccount)
                    .filter(func.lower(MonitoredAccount.username) == clean_host.lower())
                    .first()
                )
                sp = db.query(SpaceModel).filter_by(space_id=self.space_id).first()
                if sp:
                    sp.verified_host_username = clean_host
                    if acc and sp.host_account_id != acc.id:
                        sp.host_account_id = acc.id
                    db.commit()
            log.info("Dynamically adopted Space host", space_id=self.space_id, host=clean_host)
        except Exception as err:
            log.debug("Failed to adopt host dynamically", error=str(err))

    def _current_offset_ms(self) -> int:
        if self._rec_wall_start is None:
            return 0
        return int((time.monotonic() - self._rec_wall_start) * 1000)
