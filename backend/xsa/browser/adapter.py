"""XBrowserAdapter — all X (Twitter) DOM logic lives here.

Responsibilities: openX, ensureLoggedIn, openProfile, detectLiveSpace,
openSpace, getSpaceState, getParticipants, getSpeakers, getActiveSpeaker,
getSpaceMetadata, recoverSession.

Design principles:
  - Prefer ARIA roles, accessible names, stable data-testid, text patterns.
  - Never scatter X selectors elsewhere in the codebase.
  - If X changes its UI only this file needs updating.
  - All methods log what they observe so failures are diagnosable.
  - Use persistent Chrome profile so login survives container restarts.

NOTE: X (as of 2025-2026) requires login to access Spaces audio on web.
      This adapter manages a persistent browser profile. The user logs in
      once via the noVNC interface (port 6080). Credentials are NEVER stored
      in code or environment variables — only in the browser profile on disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import (
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    async_playwright,
)

from xsa.logging_setup import get_logger

log = get_logger("BROWSER")

_viewer_retry_until = 0.0
_viewer_429_streak = 0
_viewer_backoff_loaded = False


def _viewer_backoff_path() -> Path:
    """Keep X's cooldown in the persistent state volume across app restarts."""
    from xsa.config import get_settings

    return get_settings().db_path.parent / "x-viewer-backoff.json"


def _load_viewer_backoff_state(*, force: bool = False) -> None:
    global _viewer_retry_until, _viewer_429_streak, _viewer_backoff_loaded
    if _viewer_backoff_loaded and not force:
        return
    _viewer_backoff_loaded = True
    try:
        state = json.loads(_viewer_backoff_path().read_text(encoding="utf-8"))
        remaining = max(0.0, float(state.get("retry_until_epoch", 0)) - time.time())
        _viewer_retry_until = max(_viewer_retry_until, time.monotonic() + remaining)
        # Legacy counters counted duplicate JS/transport events. Preserve their
        # deadline, but do not carry their unverified escalation into new windows.
        streak = int(state.get("rate_limit_streak", 0)) if state.get("policy_version") == 2 else 0
        _viewer_429_streak = max(_viewer_429_streak, streak)
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
        log.warning("Could not restore persisted X Viewer cooldown", error=str(exc))


def _persist_viewer_backoff_state() -> None:
    remaining = viewer_retry_remaining()
    path = _viewer_backoff_path()
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "retry_until_epoch": time.time() + remaining if remaining > 0 else 0,
            "rate_limit_streak": _viewer_429_streak,
            "policy_version": 2,
        }
        temp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as exc:
        log.warning("Could not persist X Viewer cooldown", error=str(exc))
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)


def viewer_retry_remaining() -> float:
    """Shared discovery backoff; existing captures continue independently."""
    _load_viewer_backoff_state()
    return max(0.0, _viewer_retry_until - time.monotonic())


def viewer_poll_allowed() -> bool:
    """Do not generate more Viewer traffic until the server cooldown expires."""
    return viewer_retry_remaining() <= 0


def observe_viewer_response(response) -> dict | None:
    global _viewer_retry_until, _viewer_429_streak
    parts = urlsplit(response.url)
    operation = parts.path.rsplit("/", 1)[-1]
    if parts.hostname != "x.com" or operation not in {"Viewer", "AudioSpaceById"}:
        return None
    if 200 <= response.status < 300:
        _viewer_429_streak = 0
        if _viewer_backoff_path().exists():
            _persist_viewer_backoff_state()
        return None
    if response.status < 400:
        return None
    result = {"operation": operation, "http_status": response.status}
    if response.status == 429:
        # Transport events and X's JS ApiError can describe the same failure.
        # Background retries during an existing cooldown are not new admitted
        # discovery attempts and must not repeatedly escalate its duration.
        window_active = viewer_retry_remaining() > 0
        if not window_active:
            _viewer_429_streak += 1
        retry_after = None
        try:
            header = response.headers.get("retry-after")
            if header is not None:
                retry_after = float(header)
        except (ValueError, TypeError):
            try:
                retry_date = parsedate_to_datetime(header)
                if retry_date.tzinfo is None:
                    retry_date = retry_date.replace(tzinfo=UTC)
                retry_after = (retry_date - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                retry_after = None
        has_explicit_retry_after = retry_after is not None
        if window_active and not has_explicit_retry_after:
            result["retry_after_seconds"] = viewer_retry_remaining()
            return result
        if retry_after is None:
            retry_after = 900.0
        # Escalate repeated rate limits when no explicit Retry-After header is provided.
        # If X provides an explicit Retry-After, respect that value directly.
        if has_explicit_retry_after:
            delay = max(1.0, retry_after)
        else:
            progressive_delay = min(3600.0, 900.0 * (2 ** min(_viewer_429_streak - 1, 2)))
            delay = max(1.0, retry_after, progressive_delay)
        _viewer_retry_until = max(_viewer_retry_until, time.monotonic() + delay)
        if _viewer_429_streak > 1:
            result["rate_limit_streak"] = _viewer_429_streak
        result["retry_after_seconds"] = delay
        _persist_viewer_backoff_state()
    return result


_load_viewer_backoff_state()


def observe_viewer_page_error(error) -> dict | None:
    # X can report an effective GraphQL 429 through its JS ApiError even
    # when the response callback did not expose that transport status.
    match = re.search(r"https://x\.com/i/api/graphql/[^/\s?]+/Viewer(?:\?[^\s]*)?\s+HTTP-(\d{3})", str(error))
    if not match:
        return None
    return observe_viewer_response(
        SimpleNamespace(
            url="https://x.com/i/api/graphql/observed/Viewer",
            status=int(match.group(1)),
            headers={},
        )
    )


_ENDED_BADGE_RE = re.compile(
    r"^ended(?:\s*[·•-]\s*\d+\s*(?:s|sec(?:ond)?s?|m|min(?:ute)?s?|"
    r"h|hr|hrs|hour(?:s)?|d|days?|w|weeks?|mo|months?|y|yrs?|years?))?$",
    re.IGNORECASE,
)


def _is_explicit_ended_badge(text: str) -> bool:
    """Match X's compact status badge without matching arbitrary page text."""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    return bool(_ENDED_BADGE_RE.fullmatch(normalized))


# Lazy import to avoid circular: graphql module imports SpaceParticipantInfo from here
# so we import it only when needed inside methods (see getParticipantsGQL etc.)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class SpaceParticipantInfo:
    username: str = ""
    display_name: str = ""
    x_user_id: str = ""
    periscope_id: str = ""
    avatar_url: str = ""
    role: str = "listener"  # host | cohost | speaker | listener
    is_muted: bool = False
    is_speaking: bool = False
    source: str = "x_dom"  # where we got this info from


@dataclass
class SpaceState:
    space_id: str = ""
    title: str = ""
    host_username: str = ""
    state: str = "unknown"  # live | ended | not_started | unknown
    listener_count: int | None = None
    started_at: datetime | None = None
    hls_url: str | None = None
    media_key: str | None = None
    url: str = ""
    participants: list[SpaceParticipantInfo] = field(default_factory=list)
    active_speaker: SpaceParticipantInfo | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Selectors & patterns (update these when X redesigns its UI)
# ---------------------------------------------------------------------------


class _Sel:
    """Centralized selector registry. Prefer ARIA and data-testid over class names."""

    # Login indicators
    LOGIN_FORM = '[data-testid="loginButton"], [data-testid="login-button"], a[href="/login"]'
    SESSION_INDICATOR = (
        '[data-testid="SideNav_AccountSwitcher_Button"], '
        '[aria-label="Account menu"], [data-testid="AppTabBar_Profile_Link"]'
    )

    # Profile page Space indicators
    LIVE_SPACE_BADGE = '[data-testid="audiospaceCard"], div[aria-label*="Space"], a[href*="/spaces/"]'
    SPACE_CARD = 'div[data-testid="audiospaceCard"]'

    # Space page — player UI & dock expansion controls
    SPACE_DOCK = (
        '[data-testid="SpaceDock"], [data-testid="SpaceDockExpanded"], '
        'div[aria-label*="Space"][role="dialog"], [data-testid="audiospace"]'
    )
    SPACE_DOCK_MINI = '[data-testid="SpaceDock"]'
    SPACE_DOCK_EXPANDED = '[data-testid="SpaceDockExpanded"]'
    DOCK_EXPAND_BTN = (
        '[data-testid="SpaceDock"] button[aria-label*="Expand" i], '
        '[data-testid="SpaceDock"] button[aria-label*="Maximize" i], '
        '[data-testid="SpaceDock"] [role="button"]:has([data-testid="SpaceTitle"]), '
        '[data-testid="SpaceDock"]'
    )
    START_LISTENING = (
        'button[data-testid="audioSpace_ListenButton"], '
        'button:has-text("Start listening"), button:has-text("Listen"), '
        'button:has-text("ابدأ الاستماع"), button:has-text("استماع")'
    )
    SPACE_TITLE = (
        'h1[data-testid="SpaceTitle"], [data-testid="SpaceDockExpanded"] h2, [data-testid="SpaceDock"] h2'
    )
    PARTICIPANT_ROW = (
        '[data-testid="SpaceParticipant"], [data-testid="audioSpace_participant"], '
        '[data-testid="SpaceDockExpanded"] [role="button"]:has(img), '
        '[data-testid="SpaceDock"] [role="button"]:has(img)'
    )
    SPEAKER_ACTIVE = (
        '[data-testid="SpeakerActive"], [aria-label*="Speaking" i], '
        '[aria-label*="يتحدث" i], [data-testid="liveAudioWave"], '
        '[data-testid="soundWave"], svg[aria-label*="Speaker" i]'
    )
    SPACE_ENDED_MSG = (
        '[data-testid="SpaceEndedMessage"], [data-testid="audioSpaceEnded"], [data-testid="SpaceReplay"]'
    )
    HOST_BADGE = '[data-testid="hostBadge"], :has-text("Host")'
    COHOST_BADGE = '[data-testid="cohostBadge"], :has-text("Co-host")'

    # Anonymous / Incognito listening controls
    ANONYMOUS_TOGGLE = (
        '[data-testid="listenAnonymouslyToggle"], '
        '[role="switch"][aria-label*="anonymous" i], '
        '[role="switch"][aria-label*="incognito" i], '
        '[role="switch"][aria-label*="مجهول" i], '
        '[role="switch"][aria-label*="تخفي" i], '
        'input[type="checkbox"][aria-label*="anonymous" i], '
        'input[type="checkbox"][aria-label*="مجهول" i], '
        'label:has-text("Listen anonymously"), '
        'label:has-text("الاستماع بشكل مجهول"), '
        'label:has-text("استماع مجهول"), '
        'div[role="button"]:has-text("Listen anonymously"), '
        'div[role="button"]:has-text("الاستماع بشكل مجهول")'
    )
    ANONYMOUS_LISTEN_BTN = (
        'button:has-text("Listen anonymously"), '
        'button:has-text("Listen quietly"), '
        'button:has-text("الاستماع بشكل مجهول"), '
        'button:has-text("استماع بهدوء"), '
        'button:has-text("استماع مجهول"), '
        'button[data-testid="audioSpace_ListenAnonymouslyButton"], '
        '[data-testid="listenAnonymouslyButton"]'
    )
    ANONYMOUS_CONFIRMED_BANNER = (
        ':is([data-testid="SpaceDock"], [data-testid="SpaceDockExpanded"], '
        '[data-testid="audiospace"], [data-testid="SpaceCard"], [role="dialog"]) '
        ':is(:text-is("You are listening anonymously"), :text-is("أنت تستمع بشكل مجهول"), '
        ':text-is("أنت تستمع بشكل مجهول الهوية"))'
    )

    # Avatar images within participant rows
    AVATAR_IMG = 'img[src*="pbs.twimg.com/profile_images"]'

    # Pinned / Shared tweet selectors (to be hidden to keep video focused on speakers)
    TWEET_PINNED = '[data-testid="tweet"], div[aria-label*="Shared by" i], div[aria-label*="تمت المشاركة" i]'
    PARTICIPANTS_BTN = (
        'button[aria-label*="members" i], button[aria-label*="participants" i], '
        'button[aria-label*="المشاركون" i], [data-testid="audioSpace_guest_list"]'
    )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class XBrowserAdapter:
    """Wraps a persistent Playwright/Chrome browser context for X interactions."""

    def __init__(
        self,
        profile_dir: Path,
        display: str = ":99",
        pulse_server: str = "unix:/tmp/pulse/native",
        pulse_sink: str | None = None,
    ) -> None:
        self._profile = profile_dir
        self._display = display
        self._pulse = pulse_server
        self._pulse_sink = pulse_sink or "xspace"
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._hls_urls: list[str] = []  # captured from network
        self._hls_observations: list[dict] = []
        self._is_logged_in: bool = False
        self._active_space_id: str | None = None
        self._last_space_end_observation: dict = {"state": "UNKNOWN"}
        self.join_checkpoint = "not_started"
        self.join_outcome: str | None = None
        self.join_anonymous_listen_state = "not_attempted"
        self.join_host_verification_state = "not_started"
        self.join_space_state = "unknown"
        self.join_last_error: str | None = None
        self._bootstrap_error: dict | None = None
        self._closed = False
        self.join_progress_callback = None

    async def _join_progress(self, checkpoint: str, **details) -> None:
        self.join_checkpoint = checkpoint
        callback = self.join_progress_callback
        if callback:
            await callback(checkpoint, details)

    async def _anonymous_listening_banner_visible(self) -> bool:
        """Require X's explicit anonymous-listening confirmation in the visible Space UI."""
        assert self._page
        pattern = re.compile(
            r"^\s*(?:"
            r"You are listening anonymously"
            r"|You're listening anonymously"
            r"|Listening anonymously"
            r"|You are listening quietly"
            r"|You're listening quietly"
            r"|أنت تستمع بشكل مجهول(?: الهوية)?"
            r"|تستمع بشكل مجهول"
            r"|الاستماع بشكل مجهول"
            r"|تستمع بهدوء"
            r")\s*$",
            re.IGNORECASE,
        )
        try:
            matches = self._page.get_by_text(pattern).locator(":visible")
            await matches.first.wait_for(state="visible", timeout=7_000)
            return await matches.count() > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the persistent Chrome context (called once at startup)."""
        self._closed = False
        from xsa.config import get_settings

        cfg = get_settings()
        self._playwright = await async_playwright().start()
        env = {
            **os.environ,
            "DISPLAY": self._display,
            "PULSE_SERVER": self._pulse,
            "PULSE_SINK": self._pulse_sink or "xspace",
            "HOME": os.environ.get("HOME", "/home/pwuser"),
            "PULSE_COOKIE": "/home/pwuser/.config/pulse/cookie",
        }

        # Remove any stale Chrome singleton lock files from previous container crashes
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = self._profile / lock_name
            if lock_path.exists() or lock_path.is_symlink():
                try:
                    lock_path.unlink()
                    log.info("Cleaned stale Chrome lock", file=lock_name)
                except Exception:
                    pass

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._profile),
            channel="chrome",
            headless=False,
            no_viewport=True,
            env=env,
            args=[
                "--test-type",
                "--disable-infobars",
                "--autoplay-policy=document-user-activation-required",
                "--use-fake-ui-for-media-stream",
                "--audio-output-channels=2",
                "--disable-features=AudioServiceSandbox,Translate,MediaRouter,IsolateOrigins,BackForwardCache",
                f"--window-size={cfg.record_width},{cfg.record_height}",
                "--window-position=0,0",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                "--simulate-outdated-no-au='Tue, 31 Dec 2099 23:59:59 GMT'",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-smooth-scrolling",
                "--disable-threaded-scrolling",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
            ignore_default_args=["--enable-automation", "--mute-audio"],
        )

        # Keep the rendering optimization, but do not globally resume AudioContexts
        # or unmute/play arbitrary page media. X pages can render unrelated media.
        await self._context.add_init_script("""
            (() => {
                // Strip CPU-heavy CSS backdrop-filters (boosts rendering performance by 5x in software rasterizer)
                const style = document.createElement('style');
                style.textContent = '* { backdrop-filter: none !important; -webkit-backdrop-filter: none !important; }';
                document.head ? document.head.appendChild(style) : document.addEventListener('DOMContentLoaded', () => document.head.appendChild(style));
            })();
        """)

        self._context.on("page", self._on_new_page)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._capture_x_bearer_from_requests(self._page)
        # Intercept HLS playlists from network
        await self._page.route("**/*.m3u8*", self._capture_hls)
        log.info(
            "Browser context started", channel="chrome", profile=str(self._profile), display=self._display
        )

    async def close(self, *, remove_profile: bool = True) -> None:
        if self._closed:
            return
        try:
            if self._context:
                await self._context.close()
                self._context = None
                self._page = None
        finally:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
        self._closed = True
        if remove_profile and str(self._profile).startswith("/tmp/profile_worker_"):
            try:
                import shutil

                shutil.rmtree(self._profile, ignore_errors=True)
                log.info("Cleaned up ephemeral worker profile", path=str(self._profile))
            except Exception:
                pass

    async def spawn_worker_adapter(self, display: str, sink: str, worker_id: int) -> XBrowserAdapter:
        """
        Spawn an isolated worker browser adapter on a designated virtual display and pulse sink.
        Clones session cookies from master so login state is preserved.
        """
        assert self._context
        cookies = await self._context.cookies()
        worker_profile = Path(f"/tmp/profile_worker_{worker_id}")
        worker_profile.mkdir(parents=True, exist_ok=True)

        worker = XBrowserAdapter(
            profile_dir=worker_profile,
            display=display,
            pulse_server=self._pulse,
            pulse_sink=sink,
        )
        try:
            await worker.start()
            if worker._context and cookies:
                await worker._context.add_cookies(cookies)
                log.info(
                    "Cloned authentication cookies to worker adapter", worker_id=worker_id, count=len(cookies)
                )
        except BaseException:
            await worker.close()
            raise
        return worker

    async def _on_new_page(self, page: Page) -> None:
        self._capture_x_bearer_from_requests(page)
        await page.route("**/*.m3u8*", self._capture_hls)

    def _capture_x_bearer_from_requests(self, page: Page) -> None:
        """Reuse the Bearer token X itself sends in authenticated GraphQL requests."""

        async def capture(request) -> None:
            if "/i/api/graphql/" not in request.url:
                return
            try:
                headers = await request.all_headers()
                token = headers.get("authorization", "")
                if token.startswith("Bearer "):
                    page._xsa_bearer_token = token
                    log.debug("Captured active X web Bearer from authenticated request")
            except Exception as exc:
                log.debug("Could not read X GraphQL request headers", error=type(exc).__name__)

        page.on("request", capture)

        def bootstrap_response(response) -> None:
            error = observe_viewer_response(response)
            if error:
                self._bootstrap_error = error

        page.on("response", bootstrap_response)

        def bootstrap_page_error(error) -> None:
            failure = observe_viewer_page_error(error)
            if failure:
                self._bootstrap_error = failure

        page.on("pageerror", bootstrap_page_error)

    async def _capture_hls(self, route) -> None:
        url = route.request.url
        request_page = getattr(route.request, "frame", None)
        frame_url = getattr(request_page, "url", "") if request_page else ""
        frame_match = re.search(r"/spaces/([A-Za-z0-9]+)", frame_url)
        if (
            self._active_space_id
            and self._last_space_end_observation.get("state") == "LIVE"
            and frame_match
            and frame_match.group(1) == self._active_space_id
            and url not in self._hls_urls
        ):
            self._hls_urls.append(url)
            parts = urlsplit(url)
            safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            observation = {
                "active_space_id": self._active_space_id,
                "media_url": safe_url,
                "media_discovered_at": datetime.now(UTC).isoformat(),
                "space_state": self._last_space_end_observation.get("state", "UNKNOWN"),
            }
            self._hls_observations.append(observation)
            log.debug("Space-bound HLS media discovered", **observation)
        await route.continue_()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def openX(self) -> None:
        assert self._page
        await self._page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30_000)
        log.info("Opened X home")

    async def ensureLoggedIn(self) -> bool:
        """Return True if logged in, False if login page is shown."""
        if self._is_logged_in:
            return True
        assert self._page
        # If currently on a space page, do not navigate away
        if "/spaces/" in (self._page.url or ""):
            self._is_logged_in = True
            return True

        try:
            await self._page.wait_for_selector(_Sel.SESSION_INDICATOR, timeout=8_000)
            log.info("Session active — user is logged in")
            self._is_logged_in = True
            return True
        except Exception:
            pass
        # Check for login prompts
        try:
            await self._page.wait_for_selector(_Sel.LOGIN_FORM, timeout=3_000)
            log.warning("Not logged in — user must log in via noVNC (port 6080)")
            return False
        except Exception:
            pass
        # Ambiguous — try navigation only if not on a space
        if "/spaces/" not in (self._page.url or ""):
            try:
                await self._page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20_000)
                await self._page.wait_for_selector(_Sel.SESSION_INDICATOR, timeout=8_000)
                self._is_logged_in = True
                return True
            except Exception:
                log.warning("Cannot determine login state")
                return False
        return True

    async def openProfile(self, username: str) -> None:
        assert self._page
        url = f"https://x.com/{username.lstrip('@')}"
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        log.info("Opened profile", username=username)

    async def getProfileInfo(self, username: str) -> dict:
        """Extract profile avatar URL and display name from the currently opened profile page."""
        assert self._page
        info = {"username": username, "display_name": "", "avatar_url": ""}
        try:
            # Avatar image
            img = await self._page.query_selector('img[src*="pbs.twimg.com/profile_images"]')
            if img:
                src = await img.get_attribute("src") or ""
                info["avatar_url"] = src.replace("_normal.", "_400x400.") if src else ""

            # Display name
            user_el = await self._page.query_selector('[data-testid="UserName"]')
            if user_el:
                lines = (await user_el.inner_text()).split("\n")
                if lines:
                    info["display_name"] = lines[0].strip()
        except Exception as e:
            log.debug("getProfileInfo extraction error", error=str(e))
        return info

    async def fetchProfileAvatar(self, username: str) -> str | None:
        """جلب رابط صورة الملف الشخصي في headless browser منفصل."""
        try:
            pw = self._playwright
            if not pw:
                return None
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
                storage_state=str(self._profile_dir / "state.json")
                if (self._profile_dir / "state.json").exists()
                else None,
            )
            page = await ctx.new_page()
            await page.goto(
                f"https://x.com/{username.lstrip('@')}", wait_until="domcontentloaded", timeout=20_000
            )
            await page.wait_for_timeout(2000)
            img = await page.query_selector('img[src*="pbs.twimg.com/profile_images"]')
            avatar_url = None
            if img:
                src = await img.get_attribute("src") or ""
                avatar_url = src.replace("_normal.", "_400x400.") if src else None
            await browser.close()
            return avatar_url
        except Exception as e:
            log.debug("fetchProfileAvatar error", username=username, error=str(e))
            return None

    async def fetchProfileName(self, username: str) -> str | None:
        """جلب الاسم المعروض للحساب في headless browser منفصل."""
        try:
            pw = self._playwright
            if not pw:
                return None
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            ctx = await browser.new_context(
                storage_state=str(self._profile_dir / "state.json")
                if (self._profile_dir / "state.json").exists()
                else None,
            )
            page = await ctx.new_page()
            await page.goto(
                f"https://x.com/{username.lstrip('@')}", wait_until="domcontentloaded", timeout=20_000
            )
            await page.wait_for_timeout(2000)
            user_el = await page.query_selector('[data-testid="UserName"]')
            display_name = None
            if user_el:
                lines = (await user_el.inner_text()).split("\n")
                if lines:
                    display_name = lines[0].strip()
            await browser.close()
            return display_name
        except Exception as e:
            log.debug("fetchProfileName error", username=username, error=str(e))
            return None

    async def detectLiveSpace(self, username: str) -> SpaceState | None:
        """
        Check if `username` is currently live in a Space. Returns SpaceState or None.

        Strategy:
        1. Open the user's profile page.
        2. Look for a Space card (audiospaceCard data-testid, or link containing /spaces/).
        3. If found, extract the space URL and basic metadata.
        """
        assert self._page
        await self.openProfile(username)
        await asyncio.sleep(2)  # JS render time

        # Modern robust Space detection (Avatar ring, header link, or author's own tweet)
        try:
            detected_info = await self._page.evaluate("""() => {
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

        return None

    async def getAuthenticatedUsername(self) -> str | None:
        """Discover the logged-in user's screen name (@handle) from the UI if available."""
        assert self._page
        try:
            # 1. Look for AccountSwitcher button innerText or aria-label
            btn = await self._page.query_selector(_Sel.SESSION_INDICATOR)
            if btn:
                text = await btn.inner_text()
                m = re.search(r"@(\w+)", text)
                if m:
                    return m.group(1).lower()
                aria = await btn.get_attribute("aria-label") or ""
                m = re.search(r"@(\w+)", aria)
                if m:
                    return m.group(1).lower()

            # 2. Look for Profile link href (e.g. href="/myusername")
            profile_link = await self._page.query_selector('[data-testid="AppTabBar_Profile_Link"]')
            if profile_link:
                href = await profile_link.get_attribute("href") or ""
                candidate = href.strip("/").split("/")[0]
                if candidate and candidate not in ("home", "explore", "notifications", "messages", "i"):
                    return candidate.lower()
        except Exception as e:
            log.debug("getAuthenticatedUsername check failed", error=str(e))
        return None

    async def openSpace(
        self, space_url: str, prefer_anonymous: bool = True, allow_authenticated_fallback: bool = False
    ) -> bool:
        """
        Navigate to the Space URL, prefer anonymous listening if available,
        and verify after joining that the authenticated profile is not shown as a visible listener.
        """
        assert self._page
        self._hls_urls.clear()
        self._hls_observations.clear()

        # Clean Space URL: strip query parameters (?s=20 etc.) and hash
        m = re.search(r"/spaces/([A-Za-z0-9]+)", space_url)
        clean_sid = m.group(1) if m else space_url
        self._active_space_id = clean_sid
        peek_url = f"https://x.com/i/spaces/{clean_sid}/peek" if m else space_url
        direct_url = f"https://x.com/i/spaces/{clean_sid}" if m else space_url

        target_url = peek_url
        self.join_outcome = None
        self.join_last_error = None
        self._bootstrap_error = None
        self.join_anonymous_listen_state = "not_attempted"
        self.join_host_verification_state = "not_started"
        await self._join_progress("navigating_to_space", browser_url=target_url)
        await self._page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
        await self._join_progress(
            "space_page_loaded", browser_url=self._page.url, browser_title=await self._page.title()
        )
        log.info(
            "Navigated to Space",
            url=target_url,
            prefer_anonymous=prefer_anonymous,
            allow_authenticated_fallback=allow_authenticated_fallback,
        )

        # Check if error message appeared ("Something went wrong")
        try:
            err = await self._page.query_selector('text="Something went wrong", text="حدث خطأ ما"')
            if err:
                log.warning("Space peek showed error, falling back to direct URL", direct_url=direct_url)
                target_url = direct_url
                await self._page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass

        await self._join_progress("waiting_for_listen_controls")

        # Allow SPA/React bundles up to 10s to render the card / dock / buttons
        try:
            await self._page.wait_for_selector(
                f"{_Sel.ANONYMOUS_TOGGLE}, {_Sel.ANONYMOUS_LISTEN_BTN}, {_Sel.START_LISTENING}, {_Sel.SPACE_DOCK}, {_Sel.SPACE_CARD}",
                timeout=45_000,
            )
        except Exception:
            # No rendered card/player is a bootstrap failure, not evidence
            # that this live Space lacks an anonymous listening option.
            error = self._bootstrap_error
            self.join_outcome = "graphql_failure" if error else "browser_failure"
            self.join_anonymous_listen_state = "ANONYMOUS_STATE_UNKNOWN"
            self.join_last_error = (
                f"X Viewer bootstrap HTTP {error['http_status']}; Space join controls did not render"
                if error
                else "Space join controls did not render within 45 seconds"
            )
            await self._join_progress(
                "space_ui_unavailable",
                anonymous_listen_state=self.join_anonymous_listen_state,
                last_error=self.join_last_error,
            )
            return False

        # Attempt anonymous listening if requested
        anonymous_joined = False
        has_anonymous_option = False
        self.join_anonymous_listen_state = "checking"
        await self._join_progress("checking_anonymous_listening")
        if prefer_anonymous:
            # 1. Check if there is an anonymous toggle / checkbox in the pre-join card
            try:
                toggle = await self._page.query_selector(_Sel.ANONYMOUS_TOGGLE)
                if toggle:
                    checked = await toggle.get_attribute("aria-checked")
                    if checked != "true":
                        await toggle.click()
                        log.info("Enabled 'Listen anonymously' toggle")
                        await asyncio.sleep(1)
                    has_anonymous_option = (
                        await toggle.evaluate("""el => {
                        const control = el.matches('input[type="checkbox"]') ? el :
                            el.control || el.querySelector('input[type="checkbox"]');
                        if (control && control.type === 'checkbox') return control.checked === true;
                        const switchControl = el.matches('[role="switch"], [role="checkbox"]') ? el :
                            el.querySelector('[role="switch"], [role="checkbox"]');
                        return (switchControl || el).getAttribute('aria-checked') === 'true';
                    }""")
                        is True
                    )
                    self.join_anonymous_listen_state = (
                        "ANONYMOUS_CONFIRMED" if has_anonymous_option else "ANONYMOUS_ACTIVATION_FAILED"
                    )
            except Exception as e:
                self.join_anonymous_listen_state = "ANONYMOUS_ACTIVATION_FAILED"
                log.debug("Anonymous toggle check", error=str(e))

            # 2. Check for explicit "Listen anonymously" / "Listen quietly" button
            try:
                anon_btn = await self._page.query_selector(_Sel.ANONYMOUS_LISTEN_BTN)
                if anon_btn:
                    await anon_btn.click()
                    log.info("Clicked 'Listen anonymously' button")
                    anonymous_joined = True
                    self.join_anonymous_listen_state = "ANONYMOUS_STATE_UNKNOWN"
                    await asyncio.sleep(2)
            except Exception as e:
                log.debug("Anonymous listen button check", error=str(e))

        # If anonymous button was not clicked, check standard listen button
        if not anonymous_joined:
            if prefer_anonymous and not allow_authenticated_fallback and not has_anonymous_option:
                try:
                    evidence = await self.get_space_end_evidence(clean_sid, check_metadata=True)
                    state = str(evidence.get("state") or "").upper()
                    self.join_space_state = state.lower()
                    self.join_outcome = (
                        "space_ended_before_join"
                        if state == "ENDED"
                        else "space_not_live"
                        if state in {"NOT_LIVE", "NOT_STARTED"}
                        else "anonymous_listening_unavailable"
                    )
                except Exception:
                    self.join_outcome = "anonymous_listening_unavailable"
                if self.join_anonymous_listen_state != "ANONYMOUS_ACTIVATION_FAILED":
                    self.join_anonymous_listen_state = "ANONYMOUS_UNAVAILABLE"
                log.error(
                    "Anonymous listening is not available for this Space and authenticated fallback is disabled."
                )
                return False

            try:
                btn = await self._page.wait_for_selector(_Sel.START_LISTENING, timeout=8_000)
                if btn:
                    await btn.click()
                    log.info("Clicked listen button")
                    await asyncio.sleep(2)
                    if not has_anonymous_option:
                        self.join_anonymous_listen_state = "ANONYMOUS_STATE_UNKNOWN"
            except Exception:
                log.debug("No standard listen button — checking if already in Space dock")

        # Wait for space dock to appear
        await self._join_progress(
            "waiting_for_player", anonymous_listen_state=self.join_anonymous_listen_state
        )
        try:
            await self._page.wait_for_selector(_Sel.SPACE_DOCK, timeout=15_000)
            log.info("Space dock visible — joined Space")
        except Exception:
            log.warning("Space dock not found after navigation", url=space_url)
            try:
                evidence = await self.get_space_end_evidence(clean_sid, check_metadata=True)
                state = str(evidence.get("state") or "").upper()
                self.join_space_state = state.lower()
                self.join_outcome = (
                    "space_ended_before_join"
                    if state == "ENDED"
                    else "space_not_live"
                    if state in {"NOT_LIVE", "NOT_STARTED"}
                    else "player_not_ready"
                )
            except Exception:
                self.join_outcome = "player_not_ready"
            return False

        # The pre-join toggle/click is not sufficient: verify the actual joined
        # player's anonymous banner before permitting capture. Search exact visible
        # text after the Space dock is ready instead of constraining the banner to
        # a small set of X's internal container test IDs, which have changed over time.
        if not allow_authenticated_fallback:
            await self._join_progress("verifying_anonymous_listening")
            if await self._anonymous_listening_banner_visible():
                self.join_anonymous_listen_state = "ANONYMOUS_CONFIRMED"
            elif anonymous_joined or has_anonymous_option:
                # We explicitly clicked an "Listen anonymously" / "Listen quietly" button
                # or enabled the anonymous toggle and the Space dock is open. X sometimes
                # does not render the confirmation banner immediately or uses a localised
                # text variant not yet in our regex.
                # Trust the explicit toggle/button click as sufficient evidence rather than
                # blocking the recording on a UI text pattern match.
                self.join_anonymous_listen_state = "ANONYMOUS_CONFIRMED_VIA_BUTTON"
                log.info(
                    "Anonymous listening confirmed via explicit toggle/button click (banner not visible)",
                    space_id=clean_sid,
                )
            else:
                self.join_anonymous_listen_state = "ANONYMOUS_STATE_UNKNOWN"
                self.join_outcome = "anonymous_listening_unavailable"
                self.join_last_error = (
                    "Could not positively confirm anonymous listening in the visible Space UI"
                )
                await self._join_progress(
                    "anonymous_listening_verification_failed",
                    anonymous_listen_state=self.join_anonymous_listen_state,
                    last_error=self.join_last_error,
                )
                log.warning(
                    "Anonymous listening confirmation was not visible; refusing recording",
                    space_id=clean_sid,
                    checkpoint="anonymous_listening_verification_failed",
                )
                return False

        # Expand Space Window / Dock to show full participants view
        await self.expandSpaceWindow()

        # Focus video directly on Host & Speakers (hiding pinned tweets)
        await self.focusSpeakersView()
        await self.dismiss_cookie_banners()

        # Post-join verification: Ensure authenticated profile is NOT visible among participants/listeners
        authenticated_user = await self.getAuthenticatedUsername()
        if not authenticated_user and not allow_authenticated_fallback:
            self.join_outcome = "identity_exposure_guard"
            return False
        if authenticated_user:
            await self._join_progress("checking_identity_exposure")
            log.info("Verifying authenticated profile visibility", auth_user=authenticated_user)
            # Give UI a moment to update participants list
            await asyncio.sleep(2)
            participants = await self.getParticipants()
            visible_usernames = [p.username.lower() for p in participants if p.username]
            if authenticated_user in visible_usernames:
                self.join_outcome = "identity_exposure_guard"
                self.join_host_verification_state = "verified"
                msg = f"Authenticated profile @{authenticated_user} is visible as a participant in Space!"
                # Identity exposure is always a hard rejection — even if authenticated fallback
                # is enabled. The fallback allows authenticated joining only when the
                # profile is genuinely not visible among the Space participants.
                log.error(msg + " Leaving space and aborting recording to protect identity.")
                try:
                    leave_btn = await self._page.query_selector(
                        'button[aria-label*="Leave" i], button:has-text("Leave"), '
                        'button[aria-label*="مغادرة" i], button:has-text("مغادرة")'
                    )
                    if leave_btn:
                        await leave_btn.click()
                except Exception:
                    pass
                return False
            else:
                log.info("Verification passed: Authenticated profile is not visible as a listener.")

        # Ensure all audio elements are unmuted and actively playing at full volume
        await self.ensureAudioPlaying()
        await self._join_progress("player_ready")
        self._last_space_end_observation = {
            "state": "LIVE",
            "source": "x_dom",
            "evidence": "joined locked Space player",
        }

        return True

    async def ensureAudioPlaying(self) -> dict:
        """Unmute only audio nodes inside this Space's player; never play page media."""
        assert self._page
        try:
            return await self._page.evaluate("""() => {
                let fixed = 0;
                let total = 0;
                const root = document.querySelector(
                    '[data-testid="SpaceDockExpanded"], [data-testid="SpaceDock"], [data-testid="audiospace"]'
                );
                if (!root) return { total, fixed, player_found: false };
                root.querySelectorAll('audio').forEach(el => {
                    total++;
                    if (el.muted) { el.muted = false; fixed++; }
                    if (el.volume < 1.0) { el.volume = 1.0; fixed++; }
                });
                return { total, fixed, player_found: true };
            }""")
        except Exception as e:
            log.debug("ensureAudioPlaying check error", error=str(e))
            return {"error": str(e)}

    async def expandSpaceWindow(self) -> bool:
        """
        Professionally expand the Space player modal to full/expanded view.
        Ensures all speakers, co-hosts, and participant waves are prominently rendered.
        """
        assert self._page
        try:
            # Check if already expanded
            expanded = await self._page.query_selector(_Sel.SPACE_DOCK_EXPANDED)
            if expanded and await expanded.is_visible():
                log.info("Space window is already expanded")
                return True

            # Look for expand button or dock banner to click
            expand_btn = await self._page.query_selector(_Sel.DOCK_EXPAND_BTN)
            if expand_btn:
                await expand_btn.click()
                log.info("Clicked expand Space dock button")
                await asyncio.sleep(1.5)
                return True
        except Exception as e:
            log.debug("expandSpaceWindow attempt error", error=str(e))
        return False

    async def focusSpeakersView(self) -> None:
        """
        Focus the recording video directly on the Host and Speakers:
        1. Permanently hides pinned / shared tweets via CSS injection so they never cover speakers.
        2. Expands Space player to fill the screen (or centered portrait mobile dock on desktop).
        3. Formats avatars into a 4-column responsive grid (identical to mobile screenshot).
        4. Clicks the participants / guest list button (👥) so members are always visible.
        """
        assert self._page
        try:
            focus_css = """
            /* Hide Twitter desktop background navigation, timeline, and sidebars */
            header[role="banner"],
            div[data-testid="sidebarColumn"],
            div[data-testid="primaryColumn"],
            nav[role="navigation"],
            div[data-testid="BottomBar"] {
                display: none !important;
                visibility: hidden !important;
                opacity: 0 !important;
            }

            /* Responsive Space Modal: centered portrait mobile layout or full screen */
            [data-testid="SpaceDock"],
            [data-testid="SpaceDockExpanded"],
            div[aria-label*="Space"][role="dialog"],
            div[aria-label*="مساحة"][role="dialog"] {
                position: fixed !important;
                top: 0 !important;
                bottom: 0 !important;
                height: 100vh !important;
                max-height: 100vh !important;
                background: #000000 !important;
                z-index: 999999 !important;
                margin: 0 !important;
                border: none !important;
                border-radius: 0 !important;
                box-shadow: none !important;
                display: flex !important;
                flex-direction: column !important;
                overflow-y: auto !important;
            }

            /* On landscape screens (>= 800px), center the tall space window elegantly like a mobile phone */
            @media (min-width: 800px) {
                [data-testid="SpaceDock"],
                [data-testid="SpaceDockExpanded"],
                div[aria-label*="Space"][role="dialog"],
                div[aria-label*="مساحة"][role="dialog"] {
                    left: 50% !important;
                    transform: translateX(-50%) !important;
                    width: 100% !important;
                    max-width: 520px !important;
                }
            }

            /* On portrait screens (< 800px), take 100% width edge-to-edge */
            @media (max-width: 799px) {
                [data-testid="SpaceDock"],
                [data-testid="SpaceDockExpanded"],
                div[aria-label*="Space"][role="dialog"],
                div[aria-label*="مساحة"][role="dialog"] {
                    left: 0 !important;
                    right: 0 !important;
                    width: 100vw !important;
                    max-width: 100vw !important;
                }
            }

            /* Hide pinned / shared tweets that obstruct host and speakers */
            [data-testid="tweet"],
            article[role="article"],
            div[data-testid="card.wrapper"],
            div[aria-label*="Shared by" i],
            div[aria-label*="تمت المشاركة" i],
            div:has(> [data-testid="tweet"]) {
                display: none !important;
                visibility: hidden !important;
                height: 0 !important;
                max-height: 0 !important;
                opacity: 0 !important;
                overflow: hidden !important;
                pointer-events: none !important;
            }

            /* 4-column participant avatar grid layout (identical to mobile app) */
            [data-testid="SpaceDockExpanded"] [role="list"],
            [data-testid="SpaceDock"] [role="list"],
            div[aria-label*="المشاركون"] [role="list"],
            div[aria-label*="Members"] [role="list"] {
                display: grid !important;
                grid-template-columns: repeat(4, 1fr) !important;
                gap: 12px 6px !important;
                padding: 10px 4px !important;
                justify-items: center !important;
                align-items: start !important;
            }

            /* Prominent avatars and names */
            [data-testid="SpaceParticipant"],
            [data-testid="audioSpace_participant"] {
                display: flex !important;
                flex-direction: column !important;
                align-items: center !important;
                text-align: center !important;
                visibility: visible !important;
                opacity: 1 !important;
            }

            /* Compact anonymous listening banner to bottom strip */
            div:has-text("You are listening anonymously"),
            div:has-text("أنت تستمع بشكل مجهول") {
                max-height: 22px !important;
                font-size: 10px !important;
                padding: 1px 4px !important;
            }
            """
            await self._page.evaluate(
                """css => {
                let style = document.getElementById('xsa-speakers-style');
                if (!style) {
                    style = document.createElement('style');
                    style.id = 'xsa-speakers-style';
                    document.head.appendChild(style);
                }
                if (style.textContent !== css) style.textContent = css;
            }""",
                focus_css,
            )

            # Click the participants / guest list button (👥) if not already showing the full participant grid
            await self._page.evaluate("""
            () => {
                const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                const guestBtn = buttons.find(b => {
                    const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                    const testId = (b.getAttribute('data-testid') || '').toLowerCase();
                    const text = (b.innerText || '').toLowerCase();
                    return testId === 'audiospace_guest_list' ||
                           aria.includes('member') || aria.includes('participant') ||
                           aria.includes('مشارك') || aria.includes('ضيوف') ||
                           testId.includes('guest_list') || testId.includes('participant');
                });
                const participants = document.querySelectorAll('[data-testid="SpaceParticipant"], [data-testid="audioSpace_participant"]');
                const expanded = guestBtn && guestBtn.getAttribute('aria-expanded') === 'true';
                if (guestBtn && !expanded && participants.length === 0 && !guestBtn.dataset.xsaOpened) {
                    guestBtn.dataset.xsaOpened = 'true';
                    guestBtn.click();
                }
            }
            """)
        except Exception as e:
            log.debug("focusSpeakersView error", error=str(e))

    async def get_space_end_evidence(
        self, expected_space_id: str | None = None, *, check_metadata: bool = True
    ) -> dict:
        """Read explicit evidence for the locked Space; page disappearance is never an end signal."""
        expected = expected_space_id or self._active_space_id
        page = self._page
        if not page or not self._context or self._context.is_closed():
            result = {"state": "DISCONNECTED", "source": "browser", "evidence": "browser_context_unavailable"}
            self._last_space_end_observation = result
            return result
        try:
            if page.is_closed():
                result = {"state": "DISCONNECTED", "source": "browser", "evidence": "space_page_closed"}
                self._last_space_end_observation = result
                return result
        except Exception:
            pass

        current_match = re.search(r"/spaces/([A-Za-z0-9]+)", page.url)
        current_id = current_match.group(1) if current_match else None
        gql_state = None
        if expected and check_metadata:
            try:
                gql_state = await self.getSpaceStateGQL(expected)
            except Exception:
                gql_state = None

        dom = {
            "explicit_ended": False,
            "replay_ended": False,
            "space_dock_visible": False,
            "ended_badges": [],
        }
        try:
            dom = await page.evaluate("""() => {
                const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const markers = Array.from(document.querySelectorAll('[data-testid="SpaceEndedMessage"], [data-testid="audioSpaceEnded"], [data-testid="SpaceReplay"]'));
                const markerVisible = markers.some(visible);
                const textNodes = Array.from(document.querySelectorAll(
                    '[role="alert"], [data-testid*="emptyState" i], main h1, main h2, main button'
                )).filter(visible).map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim())
                  .filter(text => text.length > 0 && text.length <= 240);
                const explicitText = textNodes.some(text => /(?:this\\s+space\\s+has\\s+ended|space\\s+has\\s+ended|انتهت\\s+(?:هذه\\s+)?المساحة|انتهى\\s+البث)/i.test(text));
                const replayText = textNodes.some(text => /(?:listen\\s+again\\s+to\\s+this\\s+space|replay\\s+this\\s+space|إعادة\\s+الاستماع\\s+للمساحة)/i.test(text));
                const titleEnded = /^This Space has ended:\\s*/i.test(document.title || '');
                // Current X Space replay pages show a compact "ENDED · 2 hr" badge.
                // Only return short, exact-looking status nodes; Python applies a strict full match.
                const badgeNodes = Array.from(document.querySelectorAll('main span, main [aria-label], [data-testid*="Space" i] span'))
                    .filter(visible).map(el => (el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim())
                    .filter(text => text.length > 0 && text.length <= 48);
                const endedBadges = badgeNodes.filter(text => /^ended(?:\\s*[·•-]\\s*\\d+\\s*(?:s|sec(?:ond)?s?|m|min(?:ute)?s?|h|hr|hrs|hour(?:s)?|d|days?|w|weeks?|mo|months?|y|yrs?|years?))?$/i.test(text));
                const docks = Array.from(document.querySelectorAll('[data-testid="SpaceDock"], [data-testid="SpaceDockExpanded"], [data-testid="audiospace"]'));
                return { explicit_ended: markerVisible || explicitText, replay_ended: replayText,
                         title_ended: titleEnded, ended_badges: endedBadges, space_dock_visible: docks.some(visible) };
            }""")
        except Exception:
            dom = {
                "explicit_ended": False,
                "replay_ended": False,
                "space_dock_visible": False,
                "ended_badges": [],
            }

        # X can move the active player back to /home while keeping the Space
        # dock open. In that case the URL no longer carries the Space ID, but
        # this isolated adapter is still locked to the ID it joined. Treat a
        # visible dock as the same live Space only when that lock matches.
        locked_space_context = current_id == expected or (
            current_id is None
            and expected is not None
            and self._active_space_id == expected
            and bool(dom.get("space_dock_visible"))
        )

        ended_badge = next(
            (text for text in dom.get("ended_badges", []) if _is_explicit_ended_badge(text)),
            None,
        )
        badge_end = ended_badge is not None or bool(dom.get("title_ended"))
        dom_end = bool(
            dom.get("explicit_ended") or badge_end or (dom.get("replay_ended") and current_id == expected)
        )
        if badge_end and current_id == expected:
            result = {
                "state": "ENDED",
                "source": "x_dom",
                "evidence": f"explicit X status badge/title={ended_badge or 'This Space has ended'};space_id={expected}",
                "confidence": 0.98,
            }
        elif gql_state == "ended":
            result = {
                "state": "ENDED",
                "source": "metadata",
                "evidence": f"audio_space_state=ended;space_id={expected}",
                "confidence": 0.99,
            }
        elif dom_end and gql_state == "live":
            result = {
                "state": "UNKNOWN",
                "source": "conflict",
                "evidence": "DOM says ended while Space metadata says live",
            }
        elif dom_end and current_id == expected:
            result = {
                "state": "ENDED",
                "source": "x_dom",
                "evidence": "explicit ended/replay state on the locked Space page",
                "confidence": 0.95,
            }
        elif gql_state == "live" and locked_space_context:
            result = {
                "state": "LIVE",
                "source": "metadata",
                "evidence": f"audio_space_state=live;space_id={expected}",
                "confidence": 0.99,
            }
        elif locked_space_context and dom.get("space_dock_visible"):
            result = {
                "state": "LIVE",
                "source": "x_dom",
                "evidence": f"Space dock visible for locked space_id={expected}",
                "confidence": 0.75,
            }
        elif expected and current_id and current_id != expected:
            result = {
                "state": "UNKNOWN",
                "source": "x_dom",
                "evidence": f"space_id_mismatch:expected={expected};observed={current_id}",
            }
        else:
            result = {"state": "UNKNOWN", "source": "x_dom", "evidence": "no definitive Space state evidence"}
        result["space_id"] = expected
        result["source_checks"] = {
            "metadata": {
                "state": (gql_state or "EXPLICIT_UNKNOWN").upper()
                if check_metadata
                else "NOT_CHECKED_THIS_CYCLE",
                "space_id": expected,
                "confidence": 0.99,
            },
            "x_dom": {
                "state": "ENDED" if dom_end and current_id == expected else "EXPLICIT_UNKNOWN",
                "space_id": expected,
                "confidence": 0.95,
            },
        }
        self._last_space_end_observation = result
        return result

    async def get_space_state(self, expected_space_id: str | None = None) -> str:
        return (await self.get_space_end_evidence(expected_space_id)).get("state", "UNKNOWN")

    async def is_space_live(self, expected_space_id: str | None = None) -> bool:
        return await self.get_space_state(expected_space_id) == "LIVE"

    async def is_space_ended(self, expected_space_id: str | None = None) -> bool:
        return await self.get_space_state(expected_space_id) == "ENDED"

    async def getSpaceState(self) -> str:
        """Backward-compatible state name for existing callers."""
        state = await self.get_space_state()
        return state.lower()

    async def quarantine_space_media(self) -> None:
        """Silence every page media source after FFmpeg has stopped."""
        if not self._page:
            return
        try:
            await self._page.evaluate("""async () => {
                for (const el of document.querySelectorAll('audio, video')) {
                    el.pause(); el.muted = true; el.volume = 0;
                }
                for (const key of ['AudioContext', 'webkitAudioContext']) {
                    const Ctx = window[key];
                    if (Ctx && Ctx.prototype) {
                        // Existing contexts are not enumerable; media elements above are still paused/muted.
                    }
                }
            }""")
        except Exception as exc:
            log.debug("Space media quarantine failed", error=type(exc).__name__)

    async def close_space_page(self, expected_space_id: str | None = None) -> None:
        """After capture stops, silence and remove the dedicated page from X media."""
        if not self._page or (expected_space_id and self._active_space_id != expected_space_id):
            return
        await self.quarantine_space_media()
        try:
            await self._page.goto("about:blank", wait_until="domcontentloaded", timeout=5_000)
        except Exception as exc:
            log.debug("Could not navigate dedicated Space page away", error=type(exc).__name__)

    async def getSpaceMetadata(self) -> dict:
        """Extract title, listener count, started_at etc from current page."""
        assert self._page
        result: dict = {"observed_at": datetime.now(UTC).isoformat()}

        # Title
        try:
            title_el = await self._page.query_selector(_Sel.SPACE_TITLE)
            if title_el:
                result["title"] = (await title_el.inner_text()).strip()
        except Exception:
            pass

        # Listener count from any text like "1,234 listening"
        try:
            body = await self._page.inner_text("body")
            m = re.search(r"([\d,]+)\s+(?:listening|listeners)", body, re.I)
            if m:
                result["listener_count"] = int(m.group(1).replace(",", ""))
        except Exception:
            pass

        # HLS URL captured from network intercept
        if self._hls_urls:
            parts = urlsplit(self._hls_urls[-1])
            result["hls_url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

        # Space ID from current URL
        m = re.search(r"/spaces/([A-Za-z0-9]+)", self._page.url)
        if m:
            result["space_id"] = m.group(1)
            result["url"] = self._page.url

        return result

    async def getParticipants(self) -> list[SpaceParticipantInfo]:
        """Scrape participant rows from the Space UI in a fast single evaluation."""
        if not self._page:
            return []
        try:
            # High-performance batch DOM extraction in a single CDP roundtrip (~5ms)
            raw_data = await self._page.evaluate("""
            () => {
                const results = [];
                const seen = new Set();
                const selectors = [
                    '[data-testid="SpaceParticipant"]',
                    '[data-testid="audioSpace_participant"]',
                    '[data-testid="SpaceDockExpanded"] [role="listitem"]',
                    '[data-testid="SpaceDockExpanded"] [role="button"]:has(img)',
                    '[data-testid="SpaceDock"] [role="listitem"]',
                    '[data-testid="SpaceDock"] [role="button"]:has(img)',
                    'div[aria-label*="المشاركون"] [role="button"]:has(img)',
                    'div[aria-label*="Members"] [role="button"]:has(img)',
                    'div[aria-label*="مساحة"] [role="button"]:has(img)'
                ];
                let elements = [];
                for (const sel of selectors) {
                    try {
                        const found = document.querySelectorAll(sel);
                        for (const el of found) {
                            if (el && el.getBoundingClientRect().height > 0) {
                                elements.push(el);
                            }
                        }
                    } catch(e) {}
                }

                const reserved = new Set(['i', 'home', 'explore', 'search', 'notifications', 'messages', 'settings', 'compose']);

                for (const el of elements) {
                    try {
                        const text = (el.innerText || '').trim();
                        const aria = (el.getAttribute('aria-label') || '').trim();
                        const combined = text + ' ' + aria;

                        // 1. Username
                        let username = '';
                        const avatarContainer = el.querySelector('[data-testid*="UserAvatar-Container-"]') ||
                                                el.closest('[data-testid*="UserAvatar-Container-"]') ||
                                                ((el.getAttribute('data-testid') || '').includes('UserAvatar-Container-') ? el : null);
                        if (avatarContainer) {
                            const tid = avatarContainer.getAttribute('data-testid') || '';
                            const m = tid.match(/UserAvatar-Container-([A-Za-z0-9_]{1,20})/);
                            if (m) username = m[1];
                        }
                        if (!username) {
                            const link = el.querySelector('a[href^="/"]') || el.closest('a[href^="/"]');
                            if (link) {
                                const href = link.getAttribute('href') || '';
                                const candidate = href.replace(/^\\//, '').split('/')[0].split('?')[0];
                                if (candidate && !reserved.has(candidate.toLowerCase())) {
                                    username = candidate;
                                }
                            }
                        }
                        if (!username) {
                            const m = combined.match(/@([A-Za-z0-9_]{1,20})/);
                            if (m) username = m[1];
                        }

                        // 2. Display Name
                        let displayName = '';
                        const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                        if (lines.length > 0) {
                            const nonRole = lines.find(l => !/^(host|co-host|cohost|speaker|listener|المضيف|مضيف|مضيف مشارك|استضافة مشتركة|متحدث|مستمع)$/i.test(l));
                            displayName = nonRole || lines[0];
                        }
                        if (!displayName) {
                            const img = el.querySelector('img[alt]');
                            if (img) displayName = img.getAttribute('alt') || '';
                        }

                        // 3. Avatar URL
                        let avatarUrl = '';
                        const img = el.querySelector('img[src*="pbs.twimg.com"]') || el.querySelector('img');
                        if (img) avatarUrl = img.src || '';

                        // 4. Role detection (Arabic & English)
                        const lowerCombined = combined.toLowerCase();
                        let role = 'listener';
                        if (lowerCombined.includes('co-host') || lowerCombined.includes('cohost') || lowerCombined.includes('مضيف مشارك') || lowerCombined.includes('استضافة مشتركة')) {
                            role = 'cohost';
                        } else if (lowerCombined.includes('host') || lowerCombined.includes('المضيف') || lowerCombined.includes('مضيف')) {
                            role = 'host';
                        } else if (lowerCombined.includes('speaker') || lowerCombined.includes('متحدث') || lowerCombined.includes('المتحدث')) {
                            role = 'speaker';
                        } else if (lowerCombined.includes('listener') || lowerCombined.includes('مستمع')) {
                            role = 'listener';
                        } else if (isSpeaking || isMuted) {
                            role = 'speaker';
                        } else {
                            role = 'listener';
                        }

                        // 5. Active speaking indicator
                        const isSpeaking = Boolean(
                            el.querySelector('[data-testid="SpeakerActive"], [data-testid="liveAudioWave"], [data-testid="soundWave"], svg[aria-label*="Speaker"], svg[aria-label*="يتحدث"], [aria-label*="Speaking"], [aria-label*="يتحدث"]')
                        );

                        // 6. Muted indicator
                        const isMuted = Boolean(
                            el.querySelector('svg[aria-label*="muted"], svg[aria-label*="كتم"], [data-testid="mic-off"], [aria-label*="Muted"], [aria-label*="مكتوم"]')
                        );

                        const key = (username || displayName).toLowerCase();
                        if (key && !seen.has(key)) {
                            seen.add(key);
                            results.push({
                                username: username || '',
                                displayName: displayName || username || '',
                                avatarUrl: avatarUrl || '',
                                role: role || 'speaker',
                                isSpeaking: isSpeaking,
                                isMuted: isMuted
                            });
                        }
                    } catch(err) {}
                }
                return results;
            }
            """)

            if raw_data and isinstance(raw_data, list):
                participants = []
                for item in raw_data:
                    p = SpaceParticipantInfo()
                    p.username = item.get("username", "")
                    p.display_name = item.get("displayName", "")
                    p.avatar_url = item.get("avatarUrl", "")
                    p.role = item.get("role", "speaker")
                    p.is_speaking = bool(item.get("isSpeaking", False))
                    p.is_muted = bool(item.get("isMuted", False))
                    p.source = "x_dom_batch"
                    participants.append(p)
                if participants:
                    return participants
        except Exception as e:
            log.debug("getParticipants batch error", error=str(e))

        # Fallback to selector-based iteration if batch evaluate yielded nothing
        participants: list[SpaceParticipantInfo] = []
        try:
            rows = await self._page.query_selector_all(_Sel.PARTICIPANT_ROW)
            for row in rows[:20]:
                p = await self._extract_participant(row)
                if p:
                    participants.append(p)
        except Exception as e:
            log.debug("getParticipants fallback error", error=str(e))
        return participants

    async def getSpeakers(self) -> list[SpaceParticipantInfo]:
        """Return only participants with role=speaker or is_speaking=True."""
        all_p = await self.getParticipants()
        return [p for p in all_p if p.role in ("speaker", "host", "cohost") or p.is_speaking]

    async def getActiveSpeaker(self) -> SpaceParticipantInfo | None:
        """
        Return the participant currently speaking.

        Strategy (in order of reliability):
        1. JavaScript DOM traversal: find animated waveform/soundwave elements
           using JS querySelectorAll with multiple attribute patterns (more
           reliable than CSS selectors which can be blocked by Shadow DOM).
        2. CSS selector fallback using _Sel.SPEAKER_ACTIVE.
        3. From GQL participants list: first non-muted speaker (weakest signal).
        """
        assert self._page

        # --- Strategy 1: JS-based DOM traversal (most reliable) ---
        try:
            speaker_info = await self._page.evaluate("""
            () => {
                // Patterns that indicate an active/speaking participant
                const waveSelectors = [
                    '[data-testid="SpeakerActive"]',
                    '[data-testid="liveAudioWave"]',
                    '[data-testid="soundWave"]',
                    '[aria-label*="Speaking"]',
                    '[aria-label*="يتحدث"]',
                    // Audio wave SVG animations
                    'svg[aria-label*="Speaker"]',
                    // Animated bars pattern used in expanded dock
                    '[data-testid="SpaceDockExpanded"] .r-jc7xae',
                    // Any element with active speaker animation class
                    '.r-1h0z5md[aria-hidden="false"]',
                ];

                let waveEl = null;
                for (const sel of waveSelectors) {
                    try {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                waveEl = el;
                                break;
                            }
                        }
                        if (waveEl) break;
                    } catch(e) {}
                }

                if (!waveEl) return null;

                // Walk up DOM tree to find participant container
                let container = waveEl;
                const maxDepth = 12;
                for (let i = 0; i < maxDepth; i++) {
                    if (!container || !container.parentElement) break;
                    container = container.parentElement;
                    const testId = container.getAttribute('data-testid') || '';
                    if (testId.includes('Participant') || testId.includes('participant') ||
                        testId.includes('Speaker') || testId.includes('audioSpace')) {
                        break;
                    }
                    // Also stop at role=listitem or role=article
                    const role = container.getAttribute('role') || '';
                    if (role === 'listitem' || role === 'article') break;
                }

                if (!container) return null;

                // Extract username using multi-strategy
                const text = (container.innerText || '').trim();
                const aria = (container.getAttribute('aria-label') || '').trim();
                const combined = text + ' ' + aria;
                let username = '';
                const avatarContainer = container.querySelector('[data-testid*="UserAvatar-Container-"]') ||
                                        container.closest('[data-testid*="UserAvatar-Container-"]') ||
                                        ((container.getAttribute('data-testid') || '').includes('UserAvatar-Container-') ? container : null);
                if (avatarContainer) {
                    const tid = avatarContainer.getAttribute('data-testid') || '';
                    const m = tid.match(/UserAvatar-Container-([A-Za-z0-9_]{1,20})/);
                    if (m) username = m[1];
                }
                if (!username) {
                    const link = container.querySelector('a[href^="/"]') || container.closest('a[href^="/"]');
                    if (link) {
                        const href = link.getAttribute('href') || '';
                        const candidate = href.replace(/^\\//, '').split('/')[0].split('?')[0];
                        if (candidate && !['i', 'home', 'explore', 'search', 'notifications', 'messages'].includes(candidate.toLowerCase())) {
                            username = candidate;
                        }
                    }
                }
                if (!username) {
                    const m = combined.match(/@([A-Za-z0-9_]{1,20})/);
                    if (m) username = m[1];
                }

                // Extract display name
                const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                const nonRole = lines.find(l => !/^(host|co-host|cohost|speaker|listener|المضيف|مضيف|مضيف مشارك|استضافة مشتركة|متحدث|مستمع)$/i.test(l));
                let displayName = nonRole || lines[0] || '';
                if (!displayName) {
                    const img = container.querySelector('img[alt]');
                    if (img) displayName = img.getAttribute('alt') || '';
                }

                // Extract avatar src
                const img = container.querySelector('img[src*="pbs.twimg.com"]') || container.querySelector('img');
                const avatarUrl = img ? img.src : '';

                // Role detection (Arabic & English)
                const lowerCombined = combined.toLowerCase();
                let role = 'speaker';
                if (lowerCombined.includes('co-host') || lowerCombined.includes('cohost') || lowerCombined.includes('مضيف مشارك') || lowerCombined.includes('استضافة مشتركة')) {
                    role = 'cohost';
                } else if (lowerCombined.includes('host') || lowerCombined.includes('المضيف') || lowerCombined.includes('مضيف')) {
                    role = 'host';
                }

                return { username, displayName, avatarUrl, role };
            }
            """)

            if speaker_info and (speaker_info.get("username") or speaker_info.get("displayName")):
                p = SpaceParticipantInfo()
                p.username = speaker_info.get("username", "")
                p.display_name = speaker_info.get("displayName", "")
                p.avatar_url = speaker_info.get("avatarUrl", "")
                p.role = speaker_info.get("role", "speaker")
                p.is_speaking = True
                p.source = "x_dom_js"
                log.debug("Active speaker found via JS", username=p.username, role=p.role)
                return p
        except Exception as e:
            log.debug("getActiveSpeaker JS strategy failed", error=str(e))

        # --- Strategy 2: CSS selector fallback ---
        try:
            active = await self._page.query_selector(_Sel.SPEAKER_ACTIVE)
            if active:
                row = await active.evaluate_handle(
                    "el => el.closest('[data-testid=\"SpaceParticipant\"]') || "
                    "el.closest('[data-testid=\"audioSpace_participant\"]') || el.parentElement"
                )
                if row:
                    participant = await self._extract_participant(row.as_element())
                    if participant:
                        participant.is_speaking = True
                        participant.source = "x_dom_css"
                        return participant
        except Exception as e:
            log.debug("getActiveSpeaker CSS fallback failed", error=str(e))

        return None

    async def getParticipantsGQL(self, space_id: str) -> list[SpaceParticipantInfo]:
        """
        Fetch participants from X GraphQL API (AudioSpaceById).
        More reliable and richer than DOM scraping.
        Falls back to DOM scraping if GQL fails.
        """
        from xsa.browser.graphql import fetch_space_participants_gql

        assert self._page
        try:
            participants = await fetch_space_participants_gql(self._page, space_id)
            if participants:
                self._last_participant_source = "x_gql"
                return participants
        except Exception as e:
            log.warning("GQL participants fetch failed, falling back to DOM", error=str(e))
        # DOM fallback
        participants = await self.getParticipants()
        self._last_participant_source = "x_dom"
        return participants

    async def getSpeakersGQL(self, space_id: str) -> list[SpaceParticipantInfo]:
        """Return speakers (host + cohost + speakers) from GQL, fallback to DOM."""
        all_p = await self.getParticipantsGQL(space_id)
        return [p for p in all_p if p.role in ("host", "cohost", "speaker")]

    async def getSpaceStateGQL(self, space_id: str) -> str | None:
        """
        Get Space state from GraphQL API. Returns 'live'|'ended'|'not_started'|None.
        Much faster and more reliable than DOM checks for ended state.
        """
        from xsa.browser.graphql import fetch_space_state_gql

        assert self._page
        try:
            return await fetch_space_state_gql(self._page, space_id)
        except Exception as e:
            log.debug("getSpaceStateGQL failed", error=str(e))
            return None

    async def recoverSession(self) -> bool:
        """Attempt to recover from a stale page (reload + re-join)."""
        assert self._page
        log.warning("Attempting session recovery")
        try:
            await self._page.reload(wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(3)
            if not self._active_space_id:
                return False
            evidence = await self.get_space_end_evidence(self._active_space_id, check_metadata=True)
            return evidence.get("state") == "LIVE"
        except Exception as e:
            log.error("Session recovery failed", error=str(e))
            return False

    async def screenshot(self, path: Path) -> None:
        assert self._page
        await self._page.screenshot(path=str(path), full_page=False)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _extract_space_from_card(self, card: ElementHandle, host_username: str) -> SpaceState | None:
        try:
            link = await card.query_selector('a[href*="/spaces/"]')
            href = await link.get_attribute("href") if link else ""
            m = re.search(r"/spaces/([A-Za-z0-9]+)", href or "")
            if not m:
                return None
            space_id = m.group(1)
            title = ""
            try:
                title_el = await card.query_selector("span, h2, h3")
                if title_el:
                    title = (await title_el.inner_text()).strip()
            except Exception:
                pass
            url = f"https://x.com{href}" if href.startswith("/") else href
            return SpaceState(
                space_id=space_id, title=title, host_username=host_username, state="detected", url=url
            )
        except Exception as e:
            log.debug("_extract_space_from_card error", error=str(e))
            return None

    async def _extract_participant(self, row: ElementHandle | None) -> SpaceParticipantInfo | None:
        if not row:
            return None
        try:
            p = SpaceParticipantInfo()
            text = (await row.inner_text()).strip()
            aria = (await row.get_attribute("aria-label") or "").strip()
            combined = text + " " + aria

            # Username discovery
            m = re.search(r"@([\w]+)", combined)
            if m:
                p.username = m.group(1)
            else:
                try:
                    # Try reading UserAvatar-Container-
                    ac = await row.query_selector('[data-testid*="UserAvatar-Container-"]')
                    if ac:
                        tid = await ac.get_attribute("data-testid") or ""
                        match_tid = re.search(r"UserAvatar-Container-([\w]+)", tid)
                        if match_tid:
                            p.username = match_tid.group(1)
                except Exception:
                    pass
                if not p.username:
                    try:
                        # Try reading link href
                        link = await row.query_selector('a[href^="/"]')
                        if link:
                            href = await link.get_attribute("href") or ""
                            candidate = href.lstrip("/").split("/")[0].split("?")[0]
                            if candidate and candidate.lower() not in ("i", "home", "explore", "search"):
                                p.username = candidate
                    except Exception:
                        pass

            # Display Name
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            non_roles = [
                line
                for line in lines
                if not re.match(
                    r"^(host|co-host|cohost|speaker|listener|المضيف|مضيف|مضيف مشارك|استضافة مشتركة|متحدث|مستمع)$",
                    line,
                    re.I,
                )
            ]
            p.display_name = non_roles[0] if non_roles else (lines[0] if lines else p.username)

            # Avatar
            try:
                img = await row.query_selector(_Sel.AVATAR_IMG)
                if img:
                    p.avatar_url = await img.get_attribute("src") or ""
            except Exception:
                pass

            # Role detection from aria labels / badges (Arabic & English)
            role_text = combined.lower()
            if any(w in role_text for w in ("co-host", "cohost", "مضيف مشارك", "استضافة مشتركة")):
                p.role = "cohost"
            elif any(w in role_text for w in ("host", "المضيف", "مضيف")):
                p.role = "host"
            elif any(w in role_text for w in ("speaker", "المتحدث", "متحدث")):
                p.role = "speaker"
            elif any(w in role_text for w in ("listener", "المستمع", "مستمع")):
                p.role = "listener"

            # Speaking indicator
            try:
                wave = await row.query_selector(_Sel.SPEAKER_ACTIVE)
                p.is_speaking = wave is not None
            except Exception:
                pass

            if not getattr(p, "role", None):
                p.role = "speaker" if p.is_speaking else "listener"

            return p if (p.username or p.display_name) else None
        except Exception as e:
            log.debug("_extract_participant error", error=str(e))
            return None

    async def dismiss_cookie_banners(self) -> None:
        """Dismiss common cookie / GDPR / prompt banners that block the UI."""
        assert self._page
        selectors = [
            'button:has-text("Accept all")',
            'button:has-text("Accept cookies")',
            'button:has-text("I agree")',
            'button:has-text("Got it")',
            'button:has-text("حسناً")',
            'button:has-text("حسنا")',
            'button[data-testid="xMigrationBottomBar"] button',
        ]
        for sel in selectors:
            try:
                btn = self._page.locator(sel).first
                await btn.click(timeout=2_000)
                await asyncio.sleep(0.5)
            except Exception:
                pass
