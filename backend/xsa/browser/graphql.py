"""X (Twitter) GraphQL / REST API helpers for Space data.

Fetches structured participant and speaker data directly from the X private
API using the browser's authenticated cookies — no external credential storage
required. All requests are made *through the Playwright page context* so they
share the same session cookies automatically.

Key endpoints used:
  - AudioSpaceById (GraphQL): full Space metadata + participants
  - /i/api/1.1/live_video_stream/status/<media_key>: HLS stream info

Design: all methods receive the Playwright Page object and extract cookies +
CSRF token from it, then issue fetch() calls inside the browser context so
they are sent with the same auth. No raw network calls leave Python.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import Page

from xsa.browser.adapter import SpaceParticipantInfo
from xsa.logging_setup import get_logger

log = get_logger("GQL")

# ---------------------------------------------------------------------------
# GraphQL operation IDs (these rotate; we use the current known values and
# fall back to a feature-discovery approach when they fail)
# ---------------------------------------------------------------------------

# Known queryId for AudioSpaceById (as tracked by open-source tools, 2025-2026)
# If this fails we try to extract it from the page bundle JS
_AUDIO_SPACE_BY_ID_QUERY_ID = "nAljmV5ydSUKBJCWmDWfqg"
_DISCOVERED_AUDIO_SPACE_BY_ID_QUERY_ID: str | None = None


def _extract_audio_space_query_id(bundle_text: str) -> str | None:
    """Extract the current AudioSpaceById operation ID from X's audio bundle."""
    match = re.search(
        r'queryId\s*:\s*["\']([^"\']+)["\']\s*,\s*operationName\s*:\s*["\']AudioSpaceById["\']',
        bundle_text,
    )
    return match.group(1) if match else None


async def _discover_audio_space_query_id(page: Page) -> str | None:
    """Read the operation ID from the audio module already loaded by x.com."""
    script = r"""
    () => {
      const operation = /queryId\s*:\s*["']([^"']+)["']\s*,\s*operationName\s*:\s*["']AudioSpaceById["']/;
      const chunks = self.webpackChunk_twitter_responsive_web || [];
      let moduleCount = 0;
      for (const chunk of chunks) {
        const modules = chunk && chunk[1] && typeof chunk[1] === 'object' ? chunk[1] : {};
        for (const factory of Object.values(modules)) {
          moduleCount += 1;
          const match = Function.prototype.toString.call(factory).match(operation);
          if (match) return {query_id: match[1], urls: [], module_count: moduleCount};
        }
      }
      const urls = Array.from(new Set([
        ...performance.getEntriesByType('resource').map(entry => entry.name),
        ...Array.from(document.scripts, script => script.src).filter(Boolean),
      ].filter(url => /(?:modules\.audio\.|(?:audio|space)[^/]*\.[^/]+\.js(?:\?|$))/i.test(url)))).slice(-8).reverse();
      return {query_id: null, urls, module_count: moduleCount};
    }
    """
    try:
        discovery = await asyncio.wait_for(page.evaluate(script), timeout=5)
    except Exception as exc:
        log.warning(
            "[GQL] operation_id_discovery_failed", category="bundle_url_lookup", exception=type(exc).__name__
        )
        return None
    if not isinstance(discovery, dict) or not isinstance(discovery.get("urls"), list):
        log.warning("[GQL] operation_id_discovery_failed", category="bundle_url_shape")
        return None
    discovered = discovery.get("query_id")
    if isinstance(discovered, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,80}", discovered):
        return discovered
    urls = discovery["urls"]
    for url in urls:
        if not isinstance(url, str) or not url.startswith(("https://abs.twimg.com/", "https://x.com/")):
            continue
        try:
            response = await asyncio.wait_for(page.request.get(url, timeout=5_000), timeout=6)
            if not response.ok:
                continue
            query_id = _extract_audio_space_query_id(await response.text())
            if query_id:
                return query_id
        except Exception as exc:
            log.debug("[GQL] audio_bundle_read_failed", exception=type(exc).__name__)
    log.warning(
        "[GQL] operation_id_discovery_failed",
        category="query_id_not_found",
        bundle_count=len(urls),
        module_count=discovery.get("module_count", 0),
    )
    return None


_AUDIO_SPACE_FEATURES = {
    "spaces_2022_h2_clipping": True,
    "spaces_2022_h2_spaces_communities": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rvk_interstitial_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


# ---------------------------------------------------------------------------
# Internal: issue authenticated fetch via browser page context
# ---------------------------------------------------------------------------


async def _page_fetch(
    page: Page,
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    timeout_ms: int = 15_000,
) -> Any | None:
    """
    Execute a fetch() inside the browser page so it uses the page's cookies
    and returns the parsed JSON. Returns None on any failure.
    """
    extra_headers = headers or {}
    body_js = f", body: {repr(body)}" if body else ""
    method_js = method.upper()

    script = f"""
    async () => {{
        try {{
            const resp = await fetch({repr(url)}, {{
                method: {repr(method_js)},
                credentials: "include",
                headers: {{
                    "Content-Type": "application/json",
                    "X-Twitter-Active-User": "yes",
                    "X-Twitter-Auth-Type": "OAuth2Session",
                    "X-Twitter-Client-Language": "en",
                    "Authorization": window.__INITIAL_STATE__?.authorizationHeader ||
                                     document.cookie.match(/ct0=([^;]+)/)?.[1] &&
                                     "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I6xeuF0HKI%3DzfBFYmvHNjXDhxhQnSbGBbEJCsujAMhm1zzUm34mMa",
                    "X-Csrf-Token": document.cookie.match(/ct0=([^;]+)/)?.[1] || "",
                    ...{repr(extra_headers)}
                }}{body_js}
            }});
            let payload = null;
            try {{ payload = await resp.json(); }}
            catch(e) {{ return {{ __fetch_failure: "invalid_json", status: resp.status, retry_after: resp.headers.get("retry-after") }}; }}
            if (!resp.ok) {{
            const errs = Array.isArray(payload?.errors) ? payload.errors : [];
            return {{ __fetch_failure: "http", status: resp.status,
                          retry_after: resp.headers.get("retry-after"),
                          graphql_error_codes: errs.map(e => e?.extensions?.code || e?.code || "unknown").slice(0, 8),
                          graphql_error_messages: errs.map(e => String(e?.message || "").slice(0, 160)).filter(Boolean).slice(0, 4),
                          response_keys: Object.keys(payload || {{}}).slice(0, 12) }};
            }}
            return payload;
        }} catch(e) {{
            return {{ __fetch_failure: "transport", exception: e?.name || "Error" }};
        }}
    }}
    """
    try:
        # Playwright's Page.evaluate has no `timeout` keyword. Passing one
        # raises TypeError on every request, which previously got swallowed
        # below and made valid Space hosts appear unknown. Bound the await at
        # the asyncio layer instead.
        result = await asyncio.wait_for(page.evaluate(script), timeout=timeout_ms / 1000)
        if isinstance(result, dict) and "__fetch_failure" in result:
            status = result.get("status")
            if status == 429:
                from xsa.browser.adapter import observe_viewer_response

                retry = result.get("retry_after")
                observe_viewer_response(
                    SimpleNamespace(url=url, status=429, headers={"retry-after": retry} if retry else {})
                )
            if status == 401:
                category = "authorization"
            elif status == 403:
                category = "forbidden_or_access_policy"
            elif status == 429:
                category = "rate_limited"
            elif status in (400, 404):
                category = "operation_or_schema_changed"
            elif result.get("__fetch_failure") == "transport":
                category = "transport"
            elif result.get("__fetch_failure") == "invalid_json":
                category = "invalid_json"
            else:
                category = "http_error"
            log.warning(
                "[GQL] request_failed",
                category=category,
                status=status,
                error_codes=result.get("graphql_error_codes", []),
                error_messages=result.get("graphql_error_messages", []),
                response_keys=result.get("response_keys", []),
                exception=result.get("exception"),
            )
            return None
        return result
    except TimeoutError:
        log.debug("[GQL] request_failed", category="page_evaluation_timeout", exception="TimeoutError")
        return None
    except Exception as e:
        log.debug("[GQL] request_failed", category="page_evaluation_exception", exception=type(e).__name__)
        return None


async def _get_bearer_token(page: Page) -> str:
    """Extract the Bearer token from the page's JS bundle if possible.

    Priority:
    1. Token captured live from authenticated GraphQL requests in the browser
       (set by _capture_x_bearer_from_requests on each page).
    2. Token provided via X_API_BEARER_TOKEN env / .env config (operator-supplied).
    3. Authorization header embedded in the page's initial state bundle.
    4. Static fallback bearer used by the x.com web client (shared — rate-limited quickly).
    """
    # 1. Live-captured from authenticated browser requests (most reliable)
    captured = getattr(page, "_xsa_bearer_token", None)
    if isinstance(captured, str) and captured.startswith("Bearer "):
        return captured

    # 2. Operator-supplied token from X_API_BEARER_TOKEN env var
    try:
        from xsa.config import get_settings

        cfg = get_settings()
        if cfg.x_api_bearer_token:
            val = cfg.x_api_bearer_token.get_secret_value().strip()
            if val:
                return val if val.startswith("Bearer ") else f"Bearer {val}"
    except Exception:
        pass

    # 3. Token embedded in the page JS bundle
    try:
        token = await page.evaluate("() => window.__INITIAL_STATE__?.authorizationHeader || ''")
        if token and token.startswith("Bearer "):
            return token
    except Exception:
        pass

    # 4. Fallback: known static bearer token used by x.com web client
    return "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I6xeuF0HKI%3DzfBFYmvHNjXDhxhQnSbGBbEJCsujAMhm1zzUm34mMa"


async def _get_csrf_token(page: Page) -> str:
    """Extract ct0 CSRF token from cookies."""
    try:
        token = await page.evaluate("() => document.cookie.match(/ct0=([^;]+)/)?.[1] || ''")
        return token or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_audio_space_by_id(page: Page, space_id: str) -> dict | None:
    """
    Fetch full Space metadata via AudioSpaceById GraphQL.

    Returns the raw `data.audioSpace` dict or None on failure.

    Fields of interest in the response:
      metadata.rest_id, metadata.state (Running|Ended|NotStarted),
      metadata.title, metadata.media_key, metadata.total_live_listeners,
      metadata.created_at, metadata.started_at, metadata.ended_at,
      participants.admins[], participants.speakers[], participants.listeners[]
      Each participant: twitter_screen_name, display_name, avatar_url,
                        is_muted_by_admin, is_muted_by_guest, periscope_user_id
    """
    import json as _json

    from xsa.browser.adapter import viewer_retry_remaining

    if viewer_retry_remaining() > 0:
        return None

    global _DISCOVERED_AUDIO_SPACE_BY_ID_QUERY_ID

    if not _DISCOVERED_AUDIO_SPACE_BY_ID_QUERY_ID:
        _DISCOVERED_AUDIO_SPACE_BY_ID_QUERY_ID = await _discover_audio_space_query_id(page)
        if _DISCOVERED_AUDIO_SPACE_BY_ID_QUERY_ID:
            log.info("[GQL] discovered AudioSpaceById operation ID from X audio bundle")
    query_id = _DISCOVERED_AUDIO_SPACE_BY_ID_QUERY_ID or _AUDIO_SPACE_BY_ID_QUERY_ID

    bearer = await _get_bearer_token(page)
    csrf = await _get_csrf_token(page)

    variables = _json.dumps(
        {
            "id": space_id,
            "isMetatagsQuery": False,
            "withReplays": True,
            "withListeners": True,
        }
    )
    features = _json.dumps(_AUDIO_SPACE_FEATURES)

    query = urlencode({"variables": variables, "features": features})
    url = f"https://x.com/i/api/graphql/{query_id}/AudioSpaceById?{query}"

    headers = {
        "Authorization": bearer,
        "X-Csrf-Token": csrf,
    }

    data = await _page_fetch(page, url, headers=headers)
    if not data:
        return None

    try:
        if isinstance(data, dict) and data.get("errors"):
            codes = [
                e.get("extensions", {}).get("code") or e.get("code") or "unknown"
                for e in data["errors"]
                if isinstance(e, dict)
            ]
            normalized_codes = " ".join(str(code).upper() for code in codes)
            if "AUTH" in normalized_codes or "UNAUTH" in normalized_codes:
                category = "authorization"
            elif "RATE" in normalized_codes or "THROTTL" in normalized_codes:
                category = "rate_limited"
            elif "FORBIDDEN" in normalized_codes:
                category = "forbidden_or_access_policy"
            elif "NOT_FOUND" in normalized_codes:
                category = "space_or_operation_not_found"
            else:
                category = "graphql_error"
            log.warning(
                "[GQL] response_contains_errors",
                category=category,
                error_codes=codes[:8],
                response_has_data=bool(data.get("data")),
            )
        audio_space = data["data"]["audioSpace"]
        if not isinstance(audio_space, dict):
            log.info("[GQL] space_data_empty", category="audio_space_null")
            return None
        if audio_space.get("rest_id") and str(audio_space["rest_id"]) != space_id:
            log.warning("[GQL] returned different Space ID; discarding response", expected_space_id=space_id)
            return None
        return audio_space
    except (KeyError, TypeError):
        keys = sorted(str(key) for key in data)[:20] if isinstance(data, dict) else []
        log.warning(
            "[GQL] response_schema_unexpected", category="missing_data_or_audio_space", response_keys=keys
        )
        return None


async def fetch_space_participants_gql(page: Page, space_id: str) -> list[SpaceParticipantInfo]:
    """
    Return a list of SpaceParticipantInfo objects from the GraphQL response.
    Covers hosts (admins), speakers, and (if available) listeners.
    """
    audio_space = await fetch_audio_space_by_id(page, space_id)
    if not audio_space:
        log.info(
            "[GQL] participant_query_unavailable",
            category="space_absent_or_request_failed",
            space_id=space_id,
        )
        return []

    participants: list[SpaceParticipantInfo] = []
    creator = (audio_space.get("metadata") or {}).get("creator_results", {}).get("result", {}) or {}
    creator_id = str(creator.get("rest_id") or "")
    creator_username = str((creator.get("legacy") or {}).get("screen_name") or "").lower()
    if "participants" not in audio_space:
        log.warning("[GQL] participant_field_missing", category="schema_changed")
        return participants
    participant_section = audio_space.get("participants")
    if not isinstance(participant_section, dict):
        log.warning(
            "[GQL] participant_field_invalid",
            category="schema_changed",
            field_type=type(participant_section).__name__,
        )
        return participants
    group_counts: dict[str, int] = {}
    expected_groups = ("admins", "speakers", "listeners")
    missing_groups = [group for group in expected_groups if group not in participant_section]
    if missing_groups:
        log.warning(
            "[GQL] participant_groups_missing",
            category="partial_or_schema_changed",
            missing_groups=missing_groups,
        )

    def _parse_group(items: list, role: str) -> None:
        if not isinstance(items, list):
            log.debug("[GQL] participant_group_invalid", role=role, field_type=type(items).__name__)
            return
        group_counts[role] = len(items)
        for item in items or []:
            try:
                p = SpaceParticipantInfo()
                p.role = role
                p.username = item.get("twitter_screen_name", "")
                p.display_name = item.get("display_name", "")
                p.periscope_id = item.get("periscope_user_id", "")
                p.avatar_url = item.get("avatar_url", "")
                p.is_muted = item.get("is_muted_by_admin", False) or item.get("is_muted_by_guest", False)
                p.source = "gql"
                # is_speaking: speakers who are not muted may be speaking
                p.is_speaking = False  # Role/mute state is not acoustic or active-speaker evidence.
                # Try to get x_user_id from nested user_results
                with contextlib.suppress(Exception):
                    p.x_user_id = item.get("user_results", {}).get("result", {}).get("rest_id", "")
                if role == "admin":
                    is_creator = bool(
                        (creator_id and str(p.x_user_id) == creator_id)
                        or (creator_username and p.username.lower() == creator_username)
                    )
                    p.role = "host" if is_creator else "cohost" if item.get("is_cohost") is True else "admin"
                if p.username or p.display_name:
                    participants.append(p)
            except Exception as e:
                log.debug("_parse_group item error", role=role, error=str(e))

    for group, role in (("admins", "admin"), ("speakers", "speaker"), ("listeners", "listener")):
        if group in participant_section:
            _parse_group(participant_section[group], role)

    if not any(group_counts.values()):
        log.debug("[GQL] participants_empty", category="valid_empty_groups", groups=group_counts)
    elif not participants:
        log.warning(
            "[GQL] participant_records_unusable",
            category="missing_identity_fields",
            input_group_counts=group_counts,
        )

    log.info(
        "GraphQL participants fetched",
        space_id=space_id,
        hosts=len([p for p in participants if p.role == "host"]),
        speakers=len([p for p in participants if p.role == "speaker"]),
        listeners=len([p for p in participants if p.role == "listener"]),
    )
    return participants


async def fetch_space_state_gql(page: Page, space_id: str) -> str | None:
    """
    Return the Space state string from GraphQL:
      'Running' → 'live'
      'Ended'   → 'ended'
      'NotStarted' → 'not_started'
      None on failure
    """
    audio_space = await fetch_audio_space_by_id(page, space_id)
    if not audio_space:
        return None
    try:
        raw = audio_space["metadata"]["state"]
        mapping = {"Running": "live", "Ended": "ended", "NotStarted": "not_started"}
        return mapping.get(raw, raw.lower())
    except (KeyError, TypeError, AttributeError):
        return None


async def fetch_hls_url_from_media_key(page: Page, media_key: str) -> str | None:
    """
    Fetch the HLS master playlist URL from:
    /i/api/1.1/live_video_stream/status/<media_key>
    Returns the m3u8 URL or None.
    """
    url = f"https://x.com/i/api/1.1/live_video_stream/status/{media_key}"
    bearer = await _get_bearer_token(page)
    csrf = await _get_csrf_token(page)
    headers = {"Authorization": bearer, "X-Csrf-Token": csrf}

    data = await _page_fetch(page, url, headers=headers)
    if not data:
        return None
    try:
        return data.get("source", {}).get("location") or data.get("source", {}).get("noRedirectPlaybackUrl")
    except Exception:
        return None


async def extract_space_id_from_page_url(page: Page) -> str | None:
    """Extract the space_id from the current page URL."""
    m = re.search(r"/spaces/([A-Za-z0-9]+)", page.url)
    return m.group(1) if m else None
