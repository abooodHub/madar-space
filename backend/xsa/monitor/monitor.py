"""Account monitor: polls configured accounts for live X Spaces.

The main loop:
  1. For each active MonitoredAccount, call XBrowserAdapter.detectLiveSpace().
  2. If a live Space is found and not already being recorded, create a
     SpaceSession and start recording.
  3. Manage state transitions in the DB.
  4. Track active sessions; stop them when Spaces end.

Health monitors run as background tasks:
  BrowserHealthMonitor — reloads page if stuck
  RecorderHealthMonitor — restarts FFmpeg segment if process dies
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime

from xsa.blocking import run_blocking
from xsa.browser import SpaceState, XBrowserAdapter
from xsa.config import Settings, get_settings
from xsa.db import Job, MonitoredAccount, Space, SystemEvent, get_session
from xsa.logging_setup import get_logger
from xsa.maintenance_gate import maintenance_active
from xsa.monitor.live_discovery import LiveDiscovery
from xsa.monitor.scheduling import AccountPollError, GraphqlWindowBudget, PollMeasurements, next_poll_at
from xsa.paths import SpacePaths
from xsa.recorder.session import SpaceSession

log = get_logger("MONITOR")
TRANSIENT_JOIN_RETRY_COOLDOWN_SECONDS = 600


class AccountMonitor:
    """
    Background service that polls monitored accounts for live Spaces and
    starts/stops recording sessions automatically.
    """

    def __init__(self, adapter: XBrowserAdapter, cfg: Settings | None = None) -> None:
        self._adapter = adapter
        self._cfg = cfg or get_settings()
        self._active_sessions: dict[str, SpaceSession] = {}  # space_id → session
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._starting_spaces: dict[str, tuple[datetime, float]] = {}
        self._post_processing_tasks: dict[str, asyncio.Task] = {}
        self._post_processing_lock = asyncio.Lock()
        self._live_spaces_feed: dict[str, dict] = {}  # space_id → feed metadata
        self.live_discovery = LiveDiscovery(self._cfg.db_path.parent)
        self._running = False
        self._last_sidebar_scan_at = 0.0
        self._poll_browser = None
        self._poll_context = None
        self._poll_page = None
        self._graphql_budget = GraphqlWindowBudget()
        # Profile navigation can run concurrently on isolated polling tabs, but
        # session/slot allocation must stay serialized to prevent duplicate
        # joins when multiple monitored accounts expose the same Space.
        self._session_init_lock = asyncio.Lock()
        self._manual_verification_lock = asyncio.Lock()

    async def _get_polling_page(self):
        """Get or initialize a dedicated headless page for polling accounts in background."""
        if self._poll_page and not self._poll_page.is_closed():
            return self._poll_page

        assert self._adapter._playwright
        self._poll_browser = await self._adapter._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._poll_context = await self._poll_browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        if self._adapter._context:
            cookies = await self._adapter._context.cookies()
            if cookies:
                await self._poll_context.add_cookies(cookies)
                log.debug("Cloned auth cookies to background polling worker", count=len(cookies))

        self._poll_page = await self._poll_context.new_page()
        from xsa.browser.adapter import observe_viewer_page_error, observe_viewer_response

        self._adapter._capture_x_bearer_from_requests(self._poll_page)
        self._poll_context.on("response", observe_viewer_response)
        self._poll_context.on("response", self._graphql_budget.on_response)
        await self._poll_context.route("https://x.com/i/api/graphql/**", self._admit_graphql_route)
        self._poll_page.on("pageerror", observe_viewer_page_error)
        return self._poll_page

    async def _admit_graphql_route(self, route) -> None:
        """Apply the shared discovery budget before a request reaches X."""
        if self._graphql_budget.available(self._cfg.max_graphql_requests_per_cycle):
            self._graphql_budget.on_request(route.request)
            await route.continue_()
        else:
            await route.abort()

    async def start(self) -> None:
        self._running = True
        log.info("Account monitor started", poll_interval=self._cfg.poll_interval_seconds)
        while self._running:
            if maintenance_active(self._cfg.db_path.parent):
                log.info("Maintenance gate is armed; automatic Space discovery is paused")
                await asyncio.sleep(self._cfg.poll_interval_seconds)
                continue
            try:
                # The home feed is useful discovery, but loading it every
                # minute creates unnecessary Viewer traffic. Keep it on a
                # separate, slower cadence; rate-limit handling remains global.
                loop = asyncio.get_running_loop()
                if (
                    not any(
                        row.get("capture_request_state") == "queued"
                        for row in self.live_discovery.entries.values()
                    )
                    and self._graphql_budget.available(self._cfg.max_graphql_requests_per_cycle)
                    and loop.time() - self._last_sidebar_scan_at >= self._cfg.sidebar_scan_interval_seconds
                ):
                    await self._scan_live_sidebar()
                    self._last_sidebar_scan_at = loop.time()
            except Exception as e:
                log.debug("Sidebar scan error", error=str(e))
            try:
                # 2. Spread profile polling across one page. A small gap avoids
                # bursts of navigations; explicit host verification at join
                # remains the authority before any recording can start.
                # During a Viewer rate-limit window, viewer_poll_allowed() gates
                # polls to one every ~5 minutes to avoid completely missing a
                # live Space while still respecting X's backoff.
                from xsa.browser.adapter import viewer_poll_allowed

                if viewer_poll_allowed():
                    await self._inspect_pending_spaces()
                    await self._poll_all_accounts()
                else:
                    from xsa.browser.adapter import viewer_retry_remaining

                    await self._park_polling_page()
                    log.debug(
                        "X Viewer rate limit: skipping profile poll this cycle",
                        retry_after_seconds=round(viewer_retry_remaining()),
                    )
            except Exception as e:
                log.error("Poll cycle error", error=str(e))
            if not self._graphql_budget.available(self._cfg.max_graphql_requests_per_cycle):
                await self._park_polling_page()
            await asyncio.sleep(self._cfg.poll_interval_seconds)

    async def _park_polling_page(self) -> None:
        """Stop automatic X-page retries on the dedicated discovery tab only."""
        page = self._poll_page
        if page and hasattr(page, "is_closed") and not page.is_closed() and page.url != "about:blank":
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)

    async def _scan_live_sidebar(self) -> None:
        """Scan home page / sidebar for live X Spaces to catch all active broadcasts quickly."""
        from xsa.browser.adapter import viewer_retry_remaining

        if viewer_retry_remaining() > 0:
            return
        if not self._graphql_budget.available(self._cfg.max_graphql_requests_per_cycle):
            return
        try:
            page = await self._get_polling_page()
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(2)
            items = await page.evaluate("""() => {
                const results = [];
                const links = Array.from(document.querySelectorAll('a[href*="/spaces/"]'));
                for (const a of links) {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/\\/spaces\\/([A-Za-z0-9]+)/);
                    if (!m) continue;
                    const space_id = m[1];
                    const text = a.innerText ? a.innerText.split('\\n').map(s => s.trim()).filter(Boolean) : [];
                    let title = '';
                    let displayName = '';
                    let username = '';
                    let role = 'host';
                    let listeners = 0;
                    for (const line of text) {
                        if (line.includes('يستضيف') || line.includes('Hosting')) role = 'host';
                        else if (line.includes('يتحدث') || line.includes('Speaking')) role = 'speaker';
                        else if (line.includes('يستمع') || line.includes('Listening')) role = 'listener';
                        else if (line.includes('+') || line.match(/^\\d+/)) {
                            const num = parseInt(line.replace(/[^0-9]/g, ''));
                            if (num) listeners = num;
                        } else if (!displayName) {
                            displayName = line;
                        } else if (!title) {
                            title = line;
                        }
                    }
                    const img = a.querySelector('img[src*="profile_images"]');
                    const avatar = img ? img.getAttribute('src') : '';
                    results.push({
                        space_id: space_id,
                        url: href.startsWith('http') ? href : 'https://x.com' + href,
                        title: title || 'مساحة مباشرة',
                        display_name: displayName,
                        username: username,
                        avatar: avatar,
                        role: role,
                        listeners: listeners
                    });
                }
                return results;
            }""")
            visible_space_ids = {item.get("space_id") for item in items if item.get("space_id")}
            for it in items:
                sid = it["space_id"]
                self.live_discovery.remember(sid)
                self._live_spaces_feed[sid] = {
                    "space_id": sid,
                    "title": it.get("title") or "مساحة مباشرة",
                    "host_username": it.get("username") or it.get("display_name") or "unknown",
                    "host_display_name": it.get("display_name") or "مساحة مباشرة",
                    "host_avatar": it.get("avatar") or "",
                    "role": it.get("role") or "host",
                    "listeners_count": it.get("listeners") or 0,
                    "is_recording": sid in self._active_sessions,
                    "url": it.get("url") or f"https://x.com/i/spaces/{sid}",
                    "verification_state": "pending",
                    "host_verified": False,
                }
            # Drop stale sidebar entries after a successful scan. Active sessions
            # are added independently by /live/feed.
            for sid in set(self._live_spaces_feed) - visible_space_ids:
                self._live_spaces_feed.pop(sid, None)
        except Exception as e:
            log.debug("Live sidebar scanner non-critical error", error=str(e))

    async def stop(self) -> None:
        self._running = False
        sessions = list(self._active_sessions.items())
        tasks = list(self._active_tasks.values())

        # Finalize the capture processes before waiting on transcription or AI
        # work; post-processing may take minutes and must not prolong recording.
        for space_id, session in sessions:
            log.info("Stopping session on shutdown", space_id=space_id)
            try:
                await session.stop(reason="shutdown")
            except Exception as e:
                log.error("Error stopping session", space_id=space_id, error=str(e))

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        post_tasks = list(self._post_processing_tasks.values())
        for task in post_tasks:
            if getattr(task, "_xsa_waiting", False):
                task.cancel()
        if post_tasks:
            log.info("Waiting for post-processing tasks", count=len(post_tasks))
            await asyncio.gather(*post_tasks, return_exceptions=True)

        if self._poll_context:
            with contextlib.suppress(Exception):
                await self._poll_context.close()
        if self._poll_browser:
            with contextlib.suppress(Exception):
                await self._poll_browser.close()

    async def stop_space(self, space_id: str, reason: str) -> dict | None:
        """Gracefully stop one capture and persist an explicit interruption."""
        session = self._active_sessions.get(space_id)
        if not session:
            return None
        joining = False
        try:
            with get_session() as db:
                row = db.query(Space).filter_by(space_id=space_id).first()
                joining = bool(row and row.state == "joining")
        except RuntimeError:
            # Unit callers may exercise the graceful recording path without a DB;
            # the production lifespan always initializes the database first.
            pass
        if joining:
            return await self.cancel_join(space_id, reason, outcome="cancelled")
        summary = await session.stop(reason=reason)
        task = self._active_tasks.get(space_id)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._active_sessions.pop(space_id, None)
        self._active_tasks.pop(space_id, None)
        if session._space_db_id is not None:
            self._set_space_state(session._space_db_id, "development_interrupted")
            self._record_space_event(
                session._space_db_id,
                "recording",
                "warning",
                "Recording stopped by operator",
                {
                    "classification": "development_interrupted",
                    "reason": reason,
                    "recording_stopped_at": (
                        session.recording_stopped_at.isoformat() if session.recording_stopped_at else None
                    ),
                    "final_file": summary.get("final_file"),
                },
            )
        return summary

    async def stop_all_recordings(self, reason: str) -> dict[str, dict | None]:
        """Stop every active capture through each session's graceful recorder path."""
        space_ids = list(self._active_sessions)
        results = await asyncio.gather(
            *(self.stop_space(space_id, reason) for space_id in space_ids), return_exceptions=True
        )
        return {
            space_id: (result if not isinstance(result, BaseException) else {"error": type(result).__name__})
            for space_id, result in zip(space_ids, results, strict=True)
        }

    async def cancel_join(
        self, space_id: str, reason: str = "Join cancelled by operator", outcome: str = "cancelled"
    ) -> dict | None:
        """Cancel a pending join and wait until its browser/slot/FFmpeg cleanup completes."""
        session = self._active_sessions.get(space_id)
        task = self._active_tasks.get(space_id)
        if not task or task.done():
            return None
        with get_session() as db:
            row = db.query(Space).filter_by(space_id=space_id).first()
            if not row or row.state != "joining":
                return None
        if session:
            session.join_outcome = outcome
            session.join_last_error = reason[:1200]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not session:
            with get_session() as db:
                row = db.query(Space).filter_by(space_id=space_id).first()
                if row:
                    row.state = "error"
                    row.join_outcome = outcome
                    row.join_last_error = reason[:1200]
                    db.commit()
            return {"space_id": space_id, "cancelled": True, "ffmpeg_alive": False, "reason": reason}
        recorder_alive = False
        try:
            proc = getattr(session._recorder, "_proc", None)
            recorder_alive = bool(proc and proc.poll() is None)
        except Exception:
            recorder_alive = True
        if recorder_alive:
            try:
                await session.abort_join()
            except Exception as cleanup_error:
                log.error(
                    "Join cancellation cleanup failed", space_id=space_id, error=type(cleanup_error).__name__
                )
            try:
                proc = getattr(session._recorder, "_proc", None)
                recorder_alive = bool(proc and proc.poll() is None)
            except Exception:
                recorder_alive = True
        if recorder_alive:
            log.critical("Cancelled join still has a live recorder", space_id=space_id)
        return {"space_id": space_id, "cancelled": True, "ffmpeg_alive": recorder_alive, "reason": reason}

    async def cancel_processing(self, space_id: str, reason: str) -> bool:
        """Cancel one tracked post-processing task and persist a blocked state."""
        task = self._post_processing_tasks.get(space_id)
        if task and not task.done():
            with get_session() as db:
                space = db.query(Space).filter_by(space_id=space_id).first()
                if space:
                    db.query(Job).filter(
                        Job.space_id == space.id, Job.job_type == "post_process", Job.state == "running"
                    ).update({Job.state: "cancelling"})
                    db.commit()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with get_session() as db:
            space = db.query(Space).filter_by(space_id=space_id).first()
            if not space:
                return False
            for job in db.query(Job).filter(
                Job.space_id == space.id,
                Job.job_type == "post_process",
                Job.state.in_(("queued", "running", "retrying", "cancelling")),
            ):
                job.state = "cancelled"
                job.completed_at = datetime.now(UTC).replace(tzinfo=None)
                job.error = reason[:1200]
            space.state = "blocked"
            db.add(
                SystemEvent(
                    event_time=datetime.now(UTC).replace(tzinfo=None),
                    category="post_processing",
                    level="warning",
                    message="Space processing cancelled by operator",
                    space_id=space.id,
                    details={"reason": reason[:250], "state": "blocked"},
                )
            )
            db.commit()
        return True

    async def quarantine_space(self, space_id: str, reason: str) -> bool:
        """Block a Space from automatic recapture and record why it was quarantined."""
        if space_id in self._active_sessions:
            await self.stop_space(space_id, f"Quarantined: {reason}")
        if space_id in self._post_processing_tasks:
            await self.cancel_processing(space_id, f"Quarantined: {reason}")
        with get_session() as db:
            space = db.query(Space).filter_by(space_id=space_id).first()
            if not space:
                return False
            space.state = "blocked"
            db.add(
                SystemEvent(
                    event_time=datetime.now(UTC).replace(tzinfo=None),
                    category="recording",
                    level="warning",
                    message="Space quarantined by operator",
                    space_id=space.id,
                    details={"reason": reason[:250], "state": "blocked"},
                )
            )
            db.commit()
        return True

    async def _poll_all_accounts(self) -> None:
        from xsa.browser.adapter import viewer_retry_remaining

        if viewer_retry_remaining() > 0:
            log.warning(
                "X Viewer rate limit: discovery backing off",
                retry_after_seconds=round(viewer_retry_remaining()),
            )
            await self._park_polling_page()
            return
        if not self._graphql_budget.available(self._cfg.max_graphql_requests_per_cycle):
            return
        cycle_started = asyncio.get_running_loop().time()
        if not self._active_sessions and not await self._adapter.ensureLoggedIn():
            log.warning(
                "Cannot poll — browser not logged in. Please log in via noVNC at http://localhost:6080"
            )
            return

        now = datetime.now(UTC).replace(tzinfo=None)
        with get_session() as db:
            accounts = (
                db.query(MonitoredAccount)
                .filter(
                    MonitoredAccount.active.is_(True),
                    MonitoredAccount.priority != "P3",
                    (MonitoredAccount.next_check_at.is_(None)) | (MonitoredAccount.next_check_at <= now),
                )
                .order_by(
                    MonitoredAccount.priority.asc(),
                    MonitoredAccount.last_checked_at.asc().nulls_first(),
                    MonitoredAccount.added_at.asc(),
                    MonitoredAccount.id.asc(),
                )
                .all()
            )[: self._cfg.max_active_accounts]
        total_accounts = len(accounts)

        if len(self._active_sessions) >= self._cfg.max_concurrent_recordings:
            log.debug(
                "Max concurrent recordings reached — skipping poll",
                active=len(self._active_sessions),
                max=self._cfg.max_concurrent_recordings,
            )
            return

        if not accounts:
            return

        # A single profile worker spreads Viewer requests across the account
        # list instead of sending a simultaneous burst that can extend 429s.
        worker_count = 1
        await self._get_polling_page()
        pages = [self._poll_page]
        pages.extend(await asyncio.gather(*(self._poll_context.new_page() for _ in range(worker_count - 1))))
        from xsa.browser.adapter import observe_viewer_page_error

        for page in pages[1:]:
            if hasattr(page, "on"):
                page.on("pageerror", observe_viewer_page_error)
        pending = asyncio.Queue()
        for account in accounts:
            pending.put_nowait(account.username)
        cycle_requests = 0

        async def poll_worker(page) -> None:
            nonlocal cycle_requests
            while not pending.empty():
                if (
                    not self._graphql_budget.available(self._cfg.max_graphql_requests_per_cycle)
                    or cycle_requests >= self._cfg.max_graphql_requests_per_cycle
                ):
                    log.warning("Shared GraphQL cycle budget reached", requests=cycle_requests)
                    return
                if maintenance_active(self._cfg.db_path.parent):
                    return
                if viewer_retry_remaining() > 0:
                    return
                if len(self._active_sessions) >= self._cfg.max_concurrent_recordings:
                    return
                username = pending.get_nowait()
                measurements = PollMeasurements()
                measured = hasattr(page, "on") and hasattr(page, "remove_listener")
                if measured:
                    page.on("request", measurements.on_request)
                    page.on("response", measurements.on_response)
                started = asyncio.get_running_loop().time()
                outcome = "error"
                try:
                    started_space_id = await self._poll_account(username, page=page)
                    outcome = measurements.outcome(bool(started_space_id), viewer_retry_remaining())
                    if started_space_id:
                        log.info(
                            "Concurrent Space recording initiated",
                            space_id=started_space_id,
                            host=username,
                            total_active=len(self._active_sessions),
                        )
                except AccountPollError as e:
                    outcome = e.outcome
                    log.warning("Account discovery failed", username=username, outcome=outcome)
                except Exception as e:
                    log.error("Account poll error", username=username, error=str(e))
                except asyncio.CancelledError:
                    outcome = "interrupted"
                    raise
                finally:
                    if measured:
                        page.remove_listener("request", measurements.on_request)
                        page.remove_listener("response", measurements.on_response)
                    cycle_requests += measurements.requests
                    retry_after = viewer_retry_remaining()
                    if retry_after > 0:
                        outcome = "rate_limited"
                    with get_session() as db:
                        acc = db.query(MonitoredAccount).filter_by(username=username).first()
                        if acc:
                            checked_at = datetime.now(UTC).replace(tzinfo=None)
                            acc.last_checked_at = checked_at
                            acc.last_poll_outcome = outcome
                            acc.last_poll_duration_seconds = round(
                                asyncio.get_running_loop().time() - started, 3
                            )
                            acc.last_poll_request_count = measurements.requests if measured else None
                            failures = (
                                int(getattr(acc, "poll_failure_streak", 0) or 0) + 1
                                if outcome in {"error", "authentication_required"}
                                else 0
                            )
                            acc.poll_failure_streak = failures
                            acc.next_check_at = next_poll_at(
                                now=checked_at,
                                interval=self._cfg.poll_interval_seconds,
                                outcome=outcome,
                                failures=failures,
                                retry_after=retry_after,
                                jitter_seconds=self._cfg.account_poll_jitter_seconds,
                            )
                            db.commit()
                if viewer_retry_remaining() > 0:
                    await self._park_polling_page()
                    return
                if not pending.empty() and self._cfg.account_poll_spacing_seconds:
                    await asyncio.sleep(self._cfg.account_poll_spacing_seconds)

        try:
            await asyncio.gather(*(poll_worker(page) for page in pages))
        finally:
            self._last_cycle_requests = cycle_requests
            for page in pages[1:]:
                with contextlib.suppress(Exception):
                    await page.close()
            log.info(
                "Account poll cycle completed",
                accounts_checked=len(accounts) - pending.qsize(),
                accounts_total=len(accounts),
                accounts_active=total_accounts,
                worker_count=worker_count,
                duration_seconds=round(asyncio.get_running_loop().time() - cycle_started, 1),
            )

    async def _detect_space_on_page(self, page, username: str) -> SpaceState | None:
        """Detect live space on a profile using the dedicated background polling page."""
        url = f"https://x.com/{username.lstrip('@')}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            await asyncio.sleep(2)
        except Exception as e:
            log.debug("Failed to load profile in background polling worker", username=username, error=str(e))
            raise AccountPollError("error") from e
        if "/i/flow/login" in page.url or "/account/access" in page.url:
            raise AccountPollError("authentication_required")

        # Cache avatar and display name
        try:
            img = await page.query_selector('img[src*="pbs.twimg.com/profile_images"]')
            avatar_url = (await img.get_attribute("src") or "") if img else ""
            user_el = await page.query_selector('[data-testid="UserName"]')
            display_name = ""
            if user_el:
                lines = (await user_el.inner_text()).split("\n")
                if lines:
                    display_name = lines[0].strip()

            if avatar_url or display_name:
                with get_session() as db:
                    acc = db.query(MonitoredAccount).filter_by(username=username).first()
                    if acc:
                        if avatar_url:
                            acc.avatar_url = avatar_url.replace("_normal.", "_400x400.")
                        if display_name:
                            acc.display_name = display_name
                        db.commit()
        except Exception:
            pass

        # Modern robust Space detection (Avatar ring, header link, or author's own tweet)
        try:
            detected_info = await page.evaluate("""() => {
                // 1. Look for Space links in profile header / avatar (outside of tweets)
                const headerLinks = Array.from(document.querySelectorAll('a[href*="/spaces/"]')).filter(a => !a.closest('[data-testid="tweet"]'));
                for (const a of headerLinks) {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/\\/spaces\\/([A-Za-z0-9]+)/);
                    if (m) {
                        return { space_id: m[1], url: href.startsWith('http') ? href : 'https://x.com' + href, title: '' };
                    }
                }

                // 2. Look for Avatar live space badge / ring
                const liveBadges = Array.from(document.querySelectorAll('[aria-label*="Space" i], [aria-label*="مساحة"]')).filter(el => {
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    return aria.includes('active') || aria.includes('نشطة') || aria.includes('join') || aria.includes('انضم');
                });
                for (const el of liveBadges) {
                    const a = el.closest('a[href*="/spaces/"]') || el.querySelector('a[href*="/spaces/"]');
                    if (a) {
                        const href = a.getAttribute('href') || '';
                        const m = href.match(/\\/spaces\\/([A-Za-z0-9]+)/);
                        if (m) {
                            return { space_id: m[1], url: href.startsWith('http') ? href : 'https://x.com' + href, title: '' };
                        }
                    }
                }

                // 3. Look for the user's OWN tweets with a Space card (skipping retweets/reposts)
                const tweets = Array.from(document.querySelectorAll('[data-testid="tweet"]'));
                for (const tweet of tweets) {
                    const social = tweet.querySelector('[data-testid="socialContext"]');
                    if (social) {
                        const text = (social.innerText || '').toLowerCase();
                        if (text.includes('reposted') || text.includes('أعاد') || text.includes('أعادت')) {
                            continue; // Skip retweets!
                        }
                    }
                    const spaceLink = tweet.querySelector('a[href*="/spaces/"]');
                    if (spaceLink) {
                        const href = spaceLink.getAttribute('href') || '';
                        const m = href.match(/\\/spaces\\/([A-Za-z0-9]+)/);
                        if (m) {
                            let title = '';
                            const card = spaceLink.closest('[data-testid="placementTracking"], [data-testid="audiospaceCard"]') || spaceLink;
                            const titleEl = card.querySelector('h2, h3, span[dir]');
                            if (titleEl) {
                                title = titleEl.innerText ? titleEl.innerText.trim() : '';
                            }
                            return { space_id: m[1], url: href.startsWith('http') ? href : 'https://x.com' + href, title: title };
                        }
                    }
                }

                return null;
            }""")

            if detected_info and detected_info.get("space_id"):
                space_id = detected_info["space_id"]
                raw_url = detected_info.get("url") or f"https://x.com/i/spaces/{space_id}"
                url = f"https://x.com/i/spaces/{space_id}/peek" if not raw_url.endswith("/peek") else raw_url
                title = detected_info.get("title", "")
                return SpaceState(
                    space_id=space_id,
                    title=title,
                    host_username=username,
                    state="detected",
                    url=url,
                )
        except Exception as e:
            log.debug("Space detection evaluation error", username=username, error=str(e))
            raise AccountPollError("error") from e

        return None

    async def _poll_account(self, username: str, page=None) -> str | None:
        if maintenance_active(self._cfg.db_path.parent):
            return None
        log.debug("Polling account", username=username)

        # Always use dedicated background polling page to keep display :99 clean and avoid race conditions
        try:
            poll_page = page or await self._get_polling_page()
            space_state = await self._detect_space_on_page(poll_page, username)
        except Exception as e:
            log.debug("Polling error", username=username, error=str(e))
            raise

        if not space_state:
            log.debug("No live Space", username=username)
            return None
        self.live_discovery.remember(space_state.space_id, username)
        discovered = await self._inspect_candidate(poll_page, space_state.space_id)
        if not discovered or discovered.get("state") != "live" or not discovered.get("host_username"):
            return None
        actual_host = discovered["host_username"]
        with get_session() as db:
            allowed = {
                row.username.casefold().lstrip("@")
                for row in db.query(MonitoredAccount).filter(MonitoredAccount.active.is_(True)).all()
            }
        if actual_host.casefold().lstrip("@") not in allowed:
            # A monitored account can be a guest in someone else's Space.
            # Discovery remains visible, but it does not authorize capture.
            return None
        space_state.host_username = actual_host
        space_state.title = discovered.get("title") or space_state.title
        async with self._session_init_lock:
            return await self._start_detected_space(actual_host, space_state)

    async def _inspect_candidate(self, page, space_id: str) -> dict | None:
        from xsa.browser.adapter import viewer_retry_remaining
        from xsa.browser.graphql import fetch_audio_space_by_id

        row = self.live_discovery.entries[space_id]
        if row.get("next_check_epoch", 0) > time.time():
            checked = row.get("checked_at_epoch")
            return row if checked and time.time() - checked <= 180 else None
        if viewer_retry_remaining() > 0:
            return None
        try:
            value = await asyncio.wait_for(fetch_audio_space_by_id(page, space_id), timeout=20)
        except Exception as exc:
            log.warning(
                "Space discovery verification unavailable", space_id=space_id, error_type=type(exc).__name__
            )
            value = None
        applied_at = time.time()
        row = self.live_discovery.apply_metadata(space_id, value)
        return row if value and (row.get("checked_at_epoch") or 0) >= applied_at else None

    async def verify_manual_capture(self, space_id: str) -> dict | None:
        """Verify an explicit request on its own page without racing profile navigation."""
        from xsa.browser.adapter import viewer_retry_remaining

        if (
            viewer_retry_remaining() > 0
            or maintenance_active(self._cfg.db_path.parent)
            or not self._graphql_budget.available(self._cfg.max_graphql_requests_per_cycle)
        ):
            return None
        async with self._manual_verification_lock:
            row = self.live_discovery.entries.get(space_id)
            if not row or row.get("capture_request_state") != "queued":
                return None
            await self._get_polling_page()
            page = await self._poll_context.new_page()
            self._adapter._capture_x_bearer_from_requests(page)
            try:
                await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20000)
                result = await self._inspect_candidate(page, space_id)
                await self._start_manual_capture(space_id, result)
                return result
            except Exception as exc:
                log.warning(
                    "Manual Space verification deferred", space_id=space_id, error_type=type(exc).__name__
                )
                return None
            finally:
                with contextlib.suppress(Exception):
                    await page.close()

    async def _start_manual_capture(self, space_id: str, row: dict | None) -> str | None:
        """Consume only a current, exact-ID, live, creator-verified request."""
        from xsa.browser.adapter import viewer_retry_remaining

        if (
            not row
            or row.get("space_id") != space_id
            or row.get("state") != "live"
            or row.get("role") != "host"
            or not row.get("host_username")
            or time.time() - float(row.get("checked_at_epoch") or 0) > 180
            or viewer_retry_remaining() > 0
            or maintenance_active(self._cfg.db_path.parent)
        ):
            return None
        async with self._session_init_lock:
            current = self.live_discovery.entries.get(space_id)
            if current is not row or current.get("capture_request_state") != "queued":
                return None
            if space_id in self._active_sessions:
                current["capture_request_state"] = "started"
                self.live_discovery.save()
                return space_id
            if space_id in self._starting_spaces:
                return None
            current["capture_request_state"] = "starting"
            self.live_discovery.save()
            space = SpaceState(
                space_id=space_id,
                url=f"https://x.com/i/spaces/{space_id}/peek",
                host_username=row["host_username"],
                state="live",
            )
            try:
                started = await self._start_detected_space(row["host_username"], space, manual_retry=True)
            except Exception as exc:
                log.warning("Manual capture start deferred", space_id=space_id, error_type=type(exc).__name__)
                started = None
            current["capture_request_state"] = "started" if started else "queued"
            current["next_check_epoch"] = time.time() + 120 if not started else current["next_check_epoch"]
            self.live_discovery.save()
            return started

    async def _inspect_pending_spaces(self) -> None:
        from xsa.browser.adapter import viewer_retry_remaining

        pending = self.live_discovery.pending_ids()
        if (
            not pending
            or viewer_retry_remaining() > 0
            or not self._graphql_budget.available(self._cfg.max_graphql_requests_per_cycle)
        ):
            return
        page = await self._get_polling_page()
        if not str(page.url).startswith("https://x.com/"):
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20000)
        for space_id in pending[:10]:
            if (
                viewer_retry_remaining() > 0
                or maintenance_active(self._cfg.db_path.parent)
                or not self._graphql_budget.available(self._cfg.max_graphql_requests_per_cycle)
            ):
                break
            discovered = await self._inspect_candidate(page, space_id)
            if discovered and discovered.get("capture_request_state") == "queued":
                await self._start_manual_capture(space_id, discovered)
            if self._cfg.account_poll_spacing_seconds:
                await asyncio.sleep(self._cfg.account_poll_spacing_seconds)

    async def _start_detected_space(
        self, username: str, space_state: SpaceState, *, manual_retry: bool = False
    ) -> str | None:
        """Reserve synchronously before the first await, shared by manual/automatic starts."""
        space_id = space_state.space_id
        if not hasattr(self, "_starting_spaces"):
            self._starting_spaces = {}
        if space_id in self._starting_spaces or space_id in self._active_sessions:
            return None
        started = (datetime.now(UTC), asyncio.get_running_loop().time())
        self._starting_spaces[space_id] = started
        owner_task = asyncio.current_task()
        self._active_tasks[space_id] = owner_task
        try:
            if manual_retry:
                # A human retry can repeat a failed pre-capture join, but never
                # overwrite a captured or administratively stopped session.
                # Retriable outcomes include both error states and rejected states
                # that may succeed now (e.g. anonymous listening is now available,
                # or allow_authenticated_fallback has been enabled).
                _RETRIABLE_OUTCOMES = {
                    "anonymous_listening_unavailable",
                    "graphql_failure",
                    "browser_failure",
                    "player_not_ready",
                    "join_timeout",
                    "host_mismatch",
                }
                with get_session() as db:
                    row = db.query(Space).filter_by(space_id=space_id).first()
                    paths = SpacePaths.resolve_existing(self._cfg.data_dir, space_id)
                    has_capture = paths is not None and any(paths.root.glob("recording*.mkv"))
                    is_retriable = (
                        row is not None
                        and row.state in {"error", "rejected"}
                        and row.recording_started_at is None
                        and not has_capture
                        and (row.join_outcome in _RETRIABLE_OUTCOMES or row.state == "error")
                    )
                    if is_retriable:
                        db.add(
                            SystemEvent(
                                space_id=row.id,
                                category="recording",
                                level="info",
                                message="Explicit retry of failed pre-capture join",
                                details={
                                    "previous_outcome": row.join_outcome,
                                    "previous_checkpoint": row.join_checkpoint,
                                    "previous_error": row.join_last_error,
                                },
                            )
                        )
                        row.state = "detected"
                        row.join_outcome = None
                        row.join_last_error = None
                        db.commit()
            async with asyncio.timeout(float(self._cfg.session_start_timeout_seconds)):
                return await self._prepare_detected_space(username, space_state, manual_capture=manual_retry)
        except (TimeoutError, asyncio.CancelledError) as exc:
            with get_session() as db:
                row = db.query(Space).filter_by(space_id=space_id).first()
                if row:
                    row.state = "error"
                    row.join_outcome = "join_timeout" if isinstance(exc, TimeoutError) else "cancelled"
                    row.join_last_error = "Start attempt ended during preparation"
                    row.join_age_seconds = asyncio.get_running_loop().time() - started[1]
                    db.commit()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return None
        finally:
            if space_id not in self._active_sessions:
                self._starting_spaces.pop(space_id, None)
                if self._active_tasks.get(space_id) is owner_task:
                    self._active_tasks.pop(space_id, None)

    async def _prepare_detected_space(
        self, username: str, space_state: SpaceState, *, manual_capture: bool = False
    ) -> str | None:

        from xsa.browser.adapter import viewer_retry_remaining

        if viewer_retry_remaining() > 0:
            return None
        if maintenance_active(self._cfg.db_path.parent):
            log.info(
                "Maintenance gate armed during detection; Space will not be joined",
                space_id=space_state.space_id,
            )
            return None

        space_id = space_state.space_id
        if len(self._active_sessions) >= self._cfg.max_concurrent_recordings:
            log.info(
                "Detected Space was not joined because recording capacity is full",
                space_id=space_id,
                active=len(self._active_sessions),
                max=self._cfg.max_concurrent_recordings,
            )
            return None
        if space_id in self._active_sessions:
            log.debug("Already recording", space_id=space_id)
            return None

        # Do not restart a development capture after an administrative stop.
        # This is a terminal recording decision even if X still shows the
        # Space as live (or another monitored account exposes the same ID).
        # Outcomes that are permanently terminal — never retry automatically.
        _PERMANENT_OUTCOMES = {
            "success",
            "space_ended_before_join",
            "space_not_live",
            "identity_exposure_guard",
            "development_interrupted",
        }
        with get_session() as db:
            existing = db.query(Space).filter_by(space_id=space_id).first()
            if existing:
                is_terminal_state = existing.state in (
                    "ended",
                    "completed",
                    "analyzed",
                    "recovered_stale",
                    "development_interrupted",
                    "blocked",
                )
                is_permanent_outcome = getattr(existing, "join_outcome", None) in _PERMANENT_OUTCOMES
                # `rejected` state is terminal only when the outcome is permanent
                # (e.g. Space ended before join, identity exposed). Transient
                # rejections like anonymous_listening_unavailable or host_mismatch
                # may succeed if conditions change; skip them only for the current
                # poll cycle by checking `_starting_spaces` (already guarded above).
                is_rejected_permanent = existing.state == "rejected" and is_permanent_outcome
                if is_terminal_state or is_permanent_outcome or is_rejected_permanent:
                    log.debug(
                        "Space is already terminal — skipping",
                        space_id=space_id,
                        state=existing.state,
                        join_outcome=getattr(existing, "join_outcome", None),
                    )
                    return None

                # A failed pre-capture join can otherwise be retried on every
                # profile scan (including when the same Space appears on a
                # different monitored account). That repeats the expensive X
                # Viewer/GraphQL work and can amplify rate limits. A manual
                # retry clears the failure state above and bypasses this delay.
                failed_before_capture = (
                    existing.state in {"error", "rejected"}
                    and existing.recording_started_at is None
                    and not is_permanent_outcome
                )
                retry_checkpoint = existing.join_checkpoint_at or existing.last_progress_at
                if failed_before_capture and retry_checkpoint:
                    now = datetime.now(UTC).replace(tzinfo=None)
                    age = max(0.0, (now - retry_checkpoint).total_seconds())
                    retry_after = TRANSIENT_JOIN_RETRY_COOLDOWN_SECONDS - age
                    if retry_after > 0:
                        log.info(
                            "Automatic retry deferred after failed pre-capture join",
                            space_id=space_id,
                            retry_after_seconds=round(retry_after),
                            previous_outcome=existing.join_outcome,
                        )
                        return None

        log.info("Live Space detected!", space_id=space_id, host=username, url=space_state.url)

        # Persist to DB
        with get_session() as db:
            account = db.query(MonitoredAccount).filter_by(username=username).first()
            existing = db.query(Space).filter_by(space_id=space_id).first()
            if not existing:
                new_space = Space(
                    space_id=space_id,
                    # Card text is often the host's name, not the Space title.
                    # The recorder fills this from AudioSpaceById metadata.
                    title=None,
                    state="detected",
                    url=space_state.url,
                    host_account_id=account.id if account else None,
                    verified_host_username=username,
                    detected_at=datetime.now(UTC),
                )
                db.add(new_space)
                db.commit()
                db.refresh(new_space)
                space_db_id = new_space.id
            else:
                existing.state = "detected"
                existing.verified_host_username = username
                if not existing.host_account_id and account:
                    existing.host_account_id = account.id
                db.commit()
                space_db_id = existing.id

        # Build paths
        paths = SpacePaths.for_space(
            self._cfg.data_dir,
            space_id,
            datetime.now(UTC),
            title="عنوان غير مسجل",
            host=f"@{username}",
        )

        # Acquire isolated virtual display and audio slot
        from xsa.recorder.slot_pool import get_slot_pool

        pool = get_slot_pool(max_slots=self._cfg.max_concurrent_recordings)
        started_at, started_mono = self._starting_spaces[space_id]
        self._begin_join(space_db_id, started_at, float(self._cfg.session_start_timeout_seconds))
        self._update_join_progress(space_db_id, "allocating_resources", {}, datetime.now(UTC))
        slot = await pool.acquire_slot()
        if not slot:
            self._set_space_state(space_db_id, "error")
            log.warning("No available recording slots for detected Space", space_id=space_id)
            return None

        session_adapter = self._adapter
        try:
            session_adapter = await self._adapter.spawn_worker_adapter(
                display=slot.display,
                sink=slot.sink_name,
                worker_id=slot.slot_id,
            )
            self._update_join_progress(space_db_id, "browser_prepared", {}, datetime.now(UTC))

            # The gate may have been armed while the isolated browser was being
            # prepared. Release the slot/browser before any join or recorder starts.
            if maintenance_active(self._cfg.db_path.parent):
                await session_adapter.close()
                await pool.release_slot(slot)
                with get_session() as db:
                    row = db.get(Space, space_db_id)
                    row.state = "error"
                    row.join_outcome = "cancelled"
                    row.join_last_error = "Maintenance gate armed during preparation"
                    db.commit()
                return None

            # Create and start the session
            session = SpaceSession(
                space_id=space_id,
                space_url=space_state.url,
                host_username=username,
                paths=paths,
                adapter=session_adapter,
                cfg=self._cfg,
                slot=slot,
                manual_capture=manual_capture,
            )
            session._space_db_id = space_db_id
            session.start_attempt_at = started_at
            session.start_attempt_monotonic = started_mono
            session.join_progress_callback = lambda checkpoint, details, at: self._update_join_progress(
                space_db_id, checkpoint, details, at
            )
            session.adapter.join_progress_callback = lambda checkpoint, details: session.report_join_progress(
                checkpoint, details
            )
            self._active_sessions[space_id] = session

            # Run the session in a background task
            task = asyncio.create_task(self._run_session(session, space_id, space_db_id))
            self._active_tasks[space_id] = task
            return space_id
        except BaseException as e:
            log.error("Failed to initialize session for Space", space_id=space_id, error=str(e))
            self._set_space_state(space_db_id, "error")
            self._record_space_event(
                space_db_id,
                "recording",
                "error",
                "Failed to initialize Space recording",
                {"stage": "session_initialization", "error_type": type(e).__name__, "error": str(e)[:1200]},
            )
            self._active_sessions.pop(space_id, None)
            if session_adapter is not self._adapter:
                with contextlib.suppress(Exception):
                    await session_adapter.close()
            await pool.release_slot(slot)
            if isinstance(e, asyncio.CancelledError):
                raise
            return None

    async def _run_session(self, session: SpaceSession, space_id: str, space_db_id: int) -> None:
        stage = "pre_join"
        join_started = getattr(session, "start_attempt_at", datetime.now(UTC))
        join_started_mono = getattr(session, "start_attempt_monotonic", asyncio.get_running_loop().time())
        join_deadline = float(self._cfg.session_start_timeout_seconds)
        if hasattr(session, "join_started_monotonic"):
            session.join_started_monotonic = join_started_mono
        try:
            if maintenance_active(self._cfg.db_path.parent):
                log.info(
                    "Maintenance gate armed before Space join; releasing prepared session", space_id=space_id
                )
                self._finish_join_failure(
                    space_db_id,
                    "cancelled",
                    "Maintenance gate armed before join",
                    stage,
                    join_started_mono,
                    join_deadline,
                    session.adapter,
                )
                return
            # Update state
            stage = "joining_space"
            if not hasattr(session, "start_attempt_at"):
                self._begin_join(space_db_id, join_started, join_deadline)
            try:
                # The single outer timeout covers navigation, anonymous listening,
                # host/identity verification, metadata, and FFmpeg startup together.
                remaining = max(0.0, join_deadline - (asyncio.get_running_loop().time() - join_started_mono))
                async with asyncio.timeout(remaining):
                    await session.start()
            except TimeoutError:
                session.join_outcome = "join_timeout"
                session.join_last_error = f"Join exceeded {join_deadline}s deadline"
                raise
            except asyncio.CancelledError:
                session.join_outcome = session.join_outcome or "cancelled"
                session.join_last_error = session.join_last_error or "Join cancelled"
                self._finish_join_failure(
                    space_db_id,
                    session.join_outcome,
                    session.join_last_error,
                    stage,
                    join_started_mono,
                    join_deadline,
                    session.adapter,
                )
                try:
                    await session.abort_join()
                except Exception as cleanup_error:
                    log.error(
                        "Join cancellation could not stop FFmpeg",
                        space_id=space_id,
                        error=type(cleanup_error).__name__,
                    )
                raise
            except Exception as exc:
                outcome = self._classify_join_failure(session, exc)
                session.join_outcome = outcome
                session.join_last_error = str(exc)[:1200]
                state = self._finish_join_failure(
                    space_db_id,
                    outcome,
                    session.join_last_error,
                    stage,
                    join_started_mono,
                    join_deadline,
                    session.adapter,
                )
                log.warning(
                    "Space join ended without recording",
                    space_id=space_id,
                    join_outcome=outcome,
                    terminal_state=state,
                    error_type=type(exc).__name__,
                )
                try:
                    await session.abort_join()
                except Exception as cleanup_error:
                    log.error(
                        "Join failure could not stop FFmpeg",
                        space_id=space_id,
                        error=type(cleanup_error).__name__,
                    )
                return
            else:
                self._finish_join_success(space_db_id, session, join_started_mono, join_deadline)
            self._set_space_state(space_db_id, "recording")
            log.info("Recording session started", space_id=space_id)

            stage = "recording"
            await session.run_until_ended()

            stage = "finalizing_recording"
            await session.stop()
            self._set_space_state(
                space_db_id,
                "processing",
                # A completed file is not proof that the X Space ended. Keep
                # ended_at reserved for explicit end evidence.
                ended_at=session.space_end_detected_at,
            )
            if session._stop_reason in {
                "space_state_unverified_after_silence",
                "audio_silence_timeout",
                "audio_monitoring_unavailable_timeout",
            }:
                self._record_space_event(
                    space_db_id,
                    "recording",
                    "warning",
                    (
                        "Capture stopped after continuous audio silence; Space end was not confirmed"
                        if session._stop_reason == "audio_silence_timeout"
                        else "Capture stopped because audio monitoring stayed unavailable; Space end was not confirmed"
                        if session._stop_reason == "audio_monitoring_unavailable_timeout"
                        else "Capture stopped after prolonged silence while Space state remained unverified"
                    ),
                    {
                        "classification": session._stop_reason,
                        "audio_silence": session._recorder.get_audio_silence_status(),
                        "space_state": session.end_detector.state.value,
                        "explicit_space_end_confirmed": False,
                        "recording_stopped_at": session.recording_stopped_at.isoformat()
                        if session.recording_stopped_at
                        else None,
                    },
                )
            log.info("Session completed", space_id=space_id)

            # Automatically run Whisper transcription & AI analysis in background
            self.schedule_post_processing(space_id, space_db_id, session.paths)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Session failed", space_id=space_id, error=str(e))
            if stage == "joining_space":
                outcome = session.join_outcome or self._classify_join_failure(session, e)
                session.join_outcome = outcome
                session.join_last_error = str(e)[:1200]
                self._finish_join_failure(
                    space_db_id,
                    outcome,
                    session.join_last_error,
                    stage,
                    join_started_mono,
                    join_deadline,
                    session.adapter,
                )
                try:
                    await session.abort_join()
                except Exception as stop_err:
                    log.warning("Join cleanup error", space_id=space_id, error=type(stop_err).__name__)
            else:
                self._set_space_state(space_db_id, "error")
                self._record_space_event(
                    space_db_id,
                    "recording",
                    "error",
                    "Space recording failed",
                    {"stage": stage, "error_type": type(e).__name__, "error": str(e)[:1200]},
                )
                try:
                    await session.stop(reason="error")
                except Exception as stop_err:
                    log.warning("Emergency session stop error", space_id=space_id, error=str(stop_err))
        finally:
            getattr(self, "_starting_spaces", {}).pop(space_id, None)
            self._active_sessions.pop(space_id, None)
            self._active_tasks.pop(space_id, None)
            # Never recycle the display/audio slot while FFmpeg may still be
            # capturing it. A failed stop used to release the slot anyway,
            # allowing a later Space to share the same screen/sink while the
            # previous FFmpeg process continued recording.
            recorder_alive = False
            try:
                proc = getattr(session._recorder, "_proc", None)
                recorder_alive = bool(proc and proc.poll() is None)
            except Exception as health_err:
                recorder_alive = True
                log.error(
                    "Cannot confirm recorder stopped; retaining capture slot",
                    space_id=space_id,
                    error=type(health_err).__name__,
                )
            if recorder_alive:
                log.critical(
                    "FFmpeg may still be running; retaining isolated capture slot",
                    space_id=space_id,
                    slot_id=getattr(session.slot, "slot_id", None),
                )
            else:
                browser_closed = True
                if session.adapter is not self._adapter:
                    try:
                        await session.adapter.close()
                    except Exception as close_err:
                        browser_closed = False
                        log.warning(
                            "Failed to close isolated Space browser", space_id=space_id, error=str(close_err)
                        )
                try:
                    from xsa.recorder.slot_pool import get_slot_pool

                    if browser_closed:
                        await get_slot_pool(max_slots=self._cfg.max_concurrent_recordings).release_slot(
                            session.slot
                        )
                except Exception as release_err:
                    log.warning(
                        "Failed to release isolated recording resources",
                        space_id=space_id,
                        error=type(release_err).__name__,
                    )

    def _begin_join(self, space_db_id: int, started_at: datetime, deadline: float) -> None:
        with get_session() as db:
            row = db.get(Space, space_db_id)
            if row:
                row.state = "joining"
                row.joining_started_at = started_at.replace(tzinfo=None)
                row.join_deadline_seconds = deadline
                row.join_age_seconds = 0.0
                row.join_checkpoint = "join_started"
                row.join_checkpoint_at = started_at.replace(tzinfo=None)
                row.last_progress_at = started_at.replace(tzinfo=None)
                row.join_outcome = None
                row.join_last_error = None
                row.join_host_verification_state = "pending"
                row.join_anonymous_listen_state = "not_attempted"
                row.join_browser_url = row.url
                row.join_browser_title = None
                db.commit()

    def _update_join_progress(self, space_db_id: int, checkpoint: str, details: dict, at: datetime) -> None:
        with get_session() as db:
            row = db.get(Space, space_db_id)
            if not row or row.state != "joining":
                return
            stamp = at.replace(tzinfo=None)
            row.join_checkpoint = checkpoint[:100]
            row.join_checkpoint_at = stamp
            row.last_progress_at = stamp
            if row.joining_started_at:
                row.join_age_seconds = max(0.0, (stamp - row.joining_started_at).total_seconds())
            if details.get("browser_url"):
                row.join_browser_url = str(details["browser_url"])[:1000]
            if details.get("browser_title"):
                row.join_browser_title = str(details["browser_title"])[:300]
            if details.get("host_verification_state"):
                row.join_host_verification_state = str(details["host_verification_state"])
            if details.get("anonymous_listen_state"):
                row.join_anonymous_listen_state = str(details["anonymous_listen_state"])
            db.commit()

    @staticmethod
    def _classify_join_failure(session, exc: Exception) -> str:
        outcome = getattr(session, "join_outcome", None) or getattr(session.adapter, "join_outcome", None)
        if outcome:
            return outcome
        message = str(exc).lower()
        if "host verification" in message:
            return "host_mismatch"
        if "anonymous listening" in message:
            return "anonymous_listening_unavailable"
        if "graphql" in message:
            return "graphql_failure"
        if "space ended" in message or "ended before join" in message:
            return "space_ended_before_join"
        if "not live" in message:
            return "space_not_live"
        if "identity" in message or "visible as a participant" in message:
            return "identity_exposure_guard"
        if "player" in message or "dock" in message:
            return "player_not_ready"
        if type(exc).__module__.startswith("playwright"):
            return "browser_failure"
        return "browser_failure"

    def _finish_join_failure(
        self,
        space_db_id: int,
        outcome: str,
        error: str,
        stage: str,
        started_mono: float,
        deadline: int,
        adapter,
    ) -> str:
        age = max(0.0, asyncio.get_running_loop().time() - started_mono)
        terminal_state = (
            "rejected"
            if outcome
            in {
                "space_ended_before_join",
                "space_not_live",
                "host_mismatch",
                "anonymous_listening_unavailable",
                "identity_exposure_guard",
            }
            else "error"
        )
        checkpoint = getattr(adapter, "join_checkpoint", None) or stage
        with get_session() as db:
            row = db.get(Space, space_db_id)
            if row:
                row.state = terminal_state
                row.join_outcome = outcome
                row.join_last_error = error[:1200]
                row.join_checkpoint = checkpoint[:100]
                row.join_age_seconds = age
                row.join_deadline_seconds = deadline
                row.join_checkpoint_at = datetime.now(UTC).replace(tzinfo=None)
                row.last_progress_at = row.join_checkpoint_at
                row.join_host_verification_state = getattr(adapter, "join_host_verification_state", None)
                row.join_anonymous_listen_state = getattr(adapter, "join_anonymous_listen_state", None)
                row.join_space_state = getattr(adapter, "join_space_state", None)
                row.join_browser_url = getattr(getattr(adapter, "_page", None), "url", None)
                db.commit()
        self._record_space_event(
            space_db_id,
            "recording",
            "warning" if terminal_state == "rejected" else "error",
            "Space join ended before recording",
            {
                "stage": stage,
                "reason": outcome,
                "join_age_seconds": round(age, 2),
                "join_deadline_seconds": deadline,
                "last_checkpoint": checkpoint,
                "error": error[:1200],
            },
        )
        return terminal_state

    def _finish_join_success(self, space_db_id: int, session, started_mono: float, deadline: int) -> None:
        age = max(0.0, asyncio.get_running_loop().time() - started_mono)
        with get_session() as db:
            row = db.get(Space, space_db_id)
            if row:
                row.join_outcome = "success"
                row.join_age_seconds = age
                row.join_deadline_seconds = deadline
                row.join_checkpoint = "recording_started"
                row.join_checkpoint_at = datetime.now(UTC).replace(tzinfo=None)
                row.last_progress_at = row.join_checkpoint_at
                row.join_host_verification_state = getattr(
                    session.adapter, "join_host_verification_state", None
                )
                row.join_anonymous_listen_state = getattr(
                    session.adapter, "join_anonymous_listen_state", None
                )
                row.join_space_state = getattr(session.adapter, "join_space_state", None)
                db.commit()

    def _set_post_processing_progress(
        self, job_id: int, progress: float, stage: str, details: dict | None = None
    ) -> None:
        with get_session() as db:
            job = db.get(Job, job_id)
            if job and job.state == "running":
                job.progress = max(0.0, min(1.0, progress))
                job.result = {"stage": stage, **(details or {})}
                db.commit()

    async def _run_post_processing(
        self, space_id: str, space_db_id: int, paths: SpacePaths, job_id: int | None = None
    ) -> None:
        """Run thumbnail generation, playback remux, Whisper transcription and AI summarization."""
        try:
            log.info("Starting post-processing pipeline", space_id=space_id)
            from xsa.recorder.media import (
                create_playback_mp4,
                create_thumbnail,
                get_recording_source,
                has_audio_video,
                playback_readiness_for_paths,
            )

            # Recover the complete recording and reject stale or incomplete derivatives.
            if job_id:
                self._set_post_processing_progress(job_id, 0.08, "validate_media")
            rec_file = await run_blocking(get_recording_source, paths)
            if rec_file is None:
                raise FileNotFoundError(f"No complete audio/video recording found in {paths.root}")

            if job_id:
                self._set_post_processing_progress(job_id, 0.18, "build_playback")
            playback_valid = await run_blocking(has_audio_video, paths.playback)
            playback_is_current = (
                playback_valid and (await run_blocking(playback_readiness_for_paths, paths))[0]
            )
            if not playback_is_current and not await run_blocking(
                create_playback_mp4, rec_file, paths.playback
            ):
                log.warning("Playback MP4 generation failed", space_id=space_id)

            if job_id:
                self._set_post_processing_progress(job_id, 0.28, "thumbnail")
            if not paths.thumbnail.exists():
                try:
                    created = await run_blocking(create_thumbnail, rec_file, paths.thumbnail, seek_seconds=15)
                    if not created:
                        raise RuntimeError("thumbnail generation failed")
                    log.info("Thumbnail generated", space_id=space_id, path=str(paths.thumbnail))
                except Exception as thumb_err:
                    log.debug("Thumbnail generation error", error=str(thumb_err))

            if job_id:
                self._set_post_processing_progress(job_id, 0.35, "transcription")
            from xsa.transcriber.worker import process_space_transcription

            def update_transcription_progress(completed: int, total: int) -> None:
                if job_id and total > 0:
                    self._set_post_processing_progress(
                        job_id,
                        0.35 + 0.43 * completed / total,
                        "transcription",
                        {"completed_windows": completed, "total_windows": total},
                    )

            await process_space_transcription(
                space_id, self._cfg, progress_callback=update_transcription_progress
            )

            # Diarization is optional and runs in its own container. Queueing it
            # must never turn a successful transcript/media run into a failure.
            if job_id:
                self._set_post_processing_progress(job_id, 0.78, "diarization_queue")
            try:
                self._enqueue_diarization(space_db_id)
            except Exception as queue_err:
                log.warning(
                    "Could not enqueue optional diarization",
                    space_id=space_id,
                    error=type(queue_err).__name__,
                )

            if job_id:
                self._set_post_processing_progress(job_id, 0.84, "semantic_analysis")
            from xsa.analyzer.summarizer import analyze_space

            def update_analysis_progress(ratio: float) -> None:
                if job_id:
                    self._set_post_processing_progress(job_id, 0.84 + 0.11 * ratio, "semantic_analysis")

            await analyze_space(space_id, self._cfg, progress_callback=update_analysis_progress)

            if job_id:
                self._set_post_processing_progress(job_id, 0.97, "finalize_report")
            from xsa.analysis_platform import build_space_analysis, media_inventory_record

            with get_session() as db:
                row = db.get(Space, space_db_id)
                report_space = {
                    "space_id": space_id,
                    "title": row.title if row else None,
                    "host": row.host_account.username
                    if row and row.host_account
                    else (row.verified_host_username if row else None),
                }
            inventory_record = await run_blocking(media_inventory_record, paths)
            await run_blocking(
                build_space_analysis, paths, space=report_space, inventory_record=inventory_record
            )
            for required in (
                paths.transcript_json,
                paths.summary_json,
                paths.root / "analysis" / "full_analysis.json",
            ):
                if not required.is_file():
                    raise RuntimeError(f"Missing required post-processing artifact: {required.name}")
                import json

                value = json.loads(required.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise RuntimeError(f"Invalid JSON object in {required.name}")
                if (
                    required.name == "full_analysis.json"
                    and value.get("space", {}).get("space_id") != space_id
                ):
                    raise RuntimeError("Report belongs to a different or unknown Space")
            if not await run_blocking(has_audio_video, paths.playback):
                raise RuntimeError("Required playback artifact failed validation")
            manifest = paths.root / "recording_manifest.json"
            manifest_partial = False
            if manifest.is_file():
                try:
                    import json

                    manifest_partial = (
                        json.loads(manifest.read_text(encoding="utf-8")).get("status") == "PARTIAL"
                    )
                except (OSError, UnicodeError, ValueError, AttributeError):
                    manifest_partial = True
            # Keep a valid but interrupted capture distinguishable from a
            # naturally ended Space, even when its usable prefix was analyzed.
            self._set_space_state(
                space_db_id,
                "development_interrupted" if manifest_partial else "analyzed",
            )
            log.info("Post-processing completed successfully", space_id=space_id)
        except Exception as e:
            log.error("Post-processing error", space_id=space_id, error=str(e))
            self._set_space_state(space_db_id, "error")
            self._record_space_event(
                space_db_id,
                "post_processing",
                "error",
                "Space post-processing failed",
                {"stage": "post_processing", "error_type": type(e).__name__, "error": str(e)[:1200]},
            )
            raise

    @staticmethod
    def _enqueue_diarization(space_db_id: int) -> None:
        """Create at most one outstanding/completed diarization job per Space."""
        with get_session() as db:
            existing = (
                db.query(Job)
                .filter(
                    Job.space_id == space_db_id,
                    Job.job_type == "diarize",
                )
                .order_by(Job.id.desc())
                .first()
            )
            if existing:
                if existing.state in {"failed", "cancelled"}:
                    existing.state = "queued"
                    existing.attempt = 0
                    existing.error = None
                    existing.result = None
                    existing.completed_at = None
                    db.commit()
                return
            db.add(Job(space_id=space_db_id, job_type="diarize", state="queued", priority=5, max_attempts=0))
            db.commit()

    def schedule_post_processing(
        self,
        space_id: str,
        space_db_id: int,
        paths: SpacePaths,
        *,
        retry: bool = False,
        job_type: str = "post_process",
    ) -> bool:
        """Persist and run media or full post-processing after an interruption."""
        if job_type not in {"post_process", "prepare_media"}:
            raise ValueError("Unsupported processing job type")
        task = self._post_processing_tasks.get(space_id)
        if task and not task.done():
            return False

        with get_session() as db:
            job = (
                db.query(Job)
                .filter(Job.space_id == space_db_id, Job.job_type == job_type)
                .order_by(Job.id.desc())
                .first()
            )
            if job is None:
                job = Job(space_id=space_db_id, job_type=job_type, state="queued", priority=10)
                db.add(job)
                db.flush()
            elif job.state == "running":
                return False
            elif job.state == "retrying":
                job.state = "queued"
            elif retry:
                job.state = "queued"
                job.attempt = 0
                job.progress = 0.0
                job.result = None
                job.error = None
                job.completed_at = None
                job.worker_id = None
            elif job.state != "queued":
                return False
            job_id = job.id
            db.commit()

        task = asyncio.create_task(
            self._execute_post_processing_job(space_id, space_db_id, paths, job_id, job_type=job_type)
        )
        task._xsa_waiting = True
        self._post_processing_tasks[space_id] = task
        task.add_done_callback(
            lambda completed, sid=space_id: (
                self._post_processing_tasks.pop(sid, None)
                if self._post_processing_tasks.get(sid) is completed
                else None
            )
        )
        return True

    def schedule_media_preparation(self, space_id: str, space_db_id: int, paths: SpacePaths) -> bool:
        """Queue a media-only repair without rerunning transcription or analysis."""
        return self.schedule_post_processing(
            space_id, space_db_id, paths, retry=True, job_type="prepare_media"
        )

    async def _run_media_preparation(self, paths: SpacePaths, job_id: int) -> None:
        from xsa.recorder.media import (
            create_playback_mp4,
            create_thumbnail,
            get_recording_source,
            has_audio_video,
            playback_readiness_for_paths,
        )

        self._set_post_processing_progress(job_id, 0.1, "validate_media")
        source = await run_blocking(get_recording_source, paths)
        if source is None:
            raise FileNotFoundError("No complete audio/video source is available")
        self._set_post_processing_progress(job_id, 0.3, "build_playback")
        playback_valid = await run_blocking(has_audio_video, paths.playback)
        playback_current = playback_valid and (await run_blocking(playback_readiness_for_paths, paths))[0]
        if not playback_current and not await run_blocking(create_playback_mp4, source, paths.playback):
            raise RuntimeError("Playback MP4 preparation failed")
        self._set_post_processing_progress(job_id, 0.8, "thumbnail")
        if not paths.thumbnail.is_file():
            created = False
            for second in (15, 1, 0):
                if await run_blocking(create_thumbnail, source, paths.thumbnail, seek_seconds=second):
                    created = True
                    break
            if not created:
                raise RuntimeError("Thumbnail preparation failed")
        if not await run_blocking(has_audio_video, paths.playback):
            raise RuntimeError("Prepared playback failed audio/video validation")

    async def _execute_post_processing_job(
        self,
        space_id: str,
        space_db_id: int,
        paths: SpacePaths,
        job_id: int,
        *,
        job_type: str = "post_process",
    ) -> None:
        """Run a durable post-processing job and persist its terminal outcome."""
        async with self._post_processing_lock:
            while getattr(self, "_active_sessions", {}) or maintenance_active(
                getattr(self._cfg, "db_path", self._cfg.data_dir / "state.sqlite3").parent
            ):
                await asyncio.sleep(2)
            from sqlalchemy import text

            from xsa.job_ownership import claim

            with get_session() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                job = db.get(Job, job_id)
                if job is None or job.state != "queued":
                    return
                job.state = "running"
                job.attempt += 1
                job.started_at = datetime.now(UTC).replace(tzinfo=None)
                job.completed_at = None
                job.progress = 0.05
                job.worker_id = "api-media-preparation" if job_type == "prepare_media" else "api-post-process"
                claim(job)
                asyncio.current_task()._xsa_waiting = False
                space = db.get(Space, space_db_id)
                if space and job_type == "post_process":
                    space.state = "processing"
                db.commit()

            async def lease_heartbeat():
                from xsa.job_ownership import renew

                while True:
                    await asyncio.sleep(10)
                    if not renew(job_id):
                        return

            heartbeat = asyncio.create_task(lease_heartbeat())
            try:
                if job_type == "prepare_media":
                    await self._run_media_preparation(paths, job_id)
                else:
                    await self._run_post_processing(space_id, space_db_id, paths, job_id)
            except asyncio.CancelledError:
                # Leave the row running. Startup recovery will put it back in the queue.
                raise
            except Exception as exc:
                with get_session() as db:
                    job = db.get(Job, job_id)
                    if job:
                        job.state = "failed"
                        job.error = f"{type(exc).__name__}: {exc}"[:4000]
                        job.progress = 0.0
                        job.completed_at = datetime.now(UTC).replace(tzinfo=None)
                    db.commit()
                return
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

            with get_session() as db:
                job = db.get(Job, job_id)
                if job:
                    import json

                    manifest = paths.root / "recording_manifest.json"
                    incomplete = (
                        job_type == "post_process"
                        and manifest.exists()
                        and json.loads(manifest.read_text(encoding="utf-8")).get("status") == "PARTIAL"
                    )
                    job.state = "partial" if incomplete else "completed"
                    job.progress = 1.0
                    job.result = (
                        {"space_id": space_id, "stage": "ready", "playback_ready": True}
                        if job_type == "prepare_media"
                        else {"space_id": space_id, "state": "partial" if incomplete else "analyzed"}
                    )
                    job.error = None
                    job.completed_at = datetime.now(UTC).replace(tzinfo=None)
                    db.commit()

    def resume_queued_post_processing(self) -> int:
        """Schedule durable post-processing jobs left queued after a restart."""
        scheduled = 0
        with get_session() as db:
            rows = (
                db.query(Job, Space)
                .join(Space, Job.space_id == Space.id)
                .filter(Job.job_type.in_(("post_process", "prepare_media")), Job.state == "queued")
                .order_by(Job.priority.desc(), Job.id)
                .all()
            )
            candidates = [
                (space.space_id, space.id, space.started_at or space.detected_at, job.job_type)
                for job, space in rows
                if space.state not in {"joining", "recording", "finalizing"}
            ]
        for space_id, space_db_id, started_at, job_type in candidates:
            paths = SpacePaths.for_space(self._cfg.data_dir, space_id, started_at)
            if self.schedule_post_processing(space_id, space_db_id, paths, job_type=job_type):
                scheduled += 1
        return scheduled

    def _set_space_state(self, space_db_id: int, state: str, ended_at: datetime | None = None) -> None:
        try:
            with get_session() as db:
                space = db.get(Space, space_db_id)
                if space:
                    space.state = state
                    if ended_at:
                        space.ended_at = ended_at
                    db.commit()
        except Exception as e:
            log.error("DB state update failed", error=str(e))

    def _record_space_event(
        self, space_db_id: int, category: str, level: str, message: str, details: dict
    ) -> None:
        """Persist concise failure evidence for the dashboard and later diagnosis."""
        try:
            with get_session() as db:
                db.add(
                    SystemEvent(
                        event_time=datetime.now(UTC),
                        category=category,
                        level=level,
                        message=message,
                        space_id=space_db_id,
                        details=details,
                    )
                )
                db.commit()
        except Exception as e:
            log.error("Could not persist Space diagnostic event", error_type=type(e).__name__)

    @property
    def active_space_ids(self) -> list[str]:
        return list(self._active_sessions.keys())
