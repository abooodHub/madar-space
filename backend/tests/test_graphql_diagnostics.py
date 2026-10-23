from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from xsa.browser import graphql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, "authorization"),
        (403, "forbidden_or_access_policy"),
        (429, "rate_limited"),
        (404, "operation_or_schema_changed"),
    ],
)
async def test_graphql_http_failures_are_classified_without_logging_response_body(status, category, caplog):
    caplog.set_level("DEBUG", logger="xsa.gql")

    class Page:
        async def evaluate(self, script):
            return {
                "__fetch_failure": "http",
                "status": status,
                "graphql_error_codes": [],
                "sensitive_response": "secret-body",
            }

    assert await graphql._page_fetch(Page(), "https://x.com/private?token=do-not-log") is None
    messages = " ".join(
        record.getMessage() + " " + str(getattr(record, "fields", {})) for record in caplog.records
    )
    assert category in messages
    assert "secret-body" not in messages
    assert "do-not-log" not in messages


@pytest.mark.asyncio
async def test_page_fetch_calls_playwright_evaluate_without_unsupported_timeout_keyword():
    class Page:
        async def evaluate(self, script):
            assert "AudioSpaceById" not in script
            return {"ok": True}

    assert await graphql._page_fetch(Page(), "https://x.com/api") == {"ok": True}


@pytest.mark.asyncio
async def test_page_fetch_bounds_evaluate_with_async_timeout(caplog):
    caplog.set_level("DEBUG", logger="xsa.gql")

    class Page:
        async def evaluate(self, script):
            import asyncio

            await asyncio.sleep(0.05)

    assert await graphql._page_fetch(Page(), "https://x.com/api", timeout_ms=1) is None
    assert any(
        "page_evaluation_timeout" in record.getMessage()
        or "page_evaluation_timeout" in str(getattr(record, "fields", {}))
        for record in caplog.records
    )


def test_extract_audio_space_query_id_from_current_web_bundle():
    bundle = 'x={queryId:"rotated_X_id-123",operationName:"AudioSpaceById",operationType:"query"}'
    assert graphql._extract_audio_space_query_id(bundle) == "rotated_X_id-123"
    assert graphql._extract_audio_space_query_id('operationName:"AudioSpaceById"') is None


@pytest.mark.asyncio
async def test_discover_audio_space_query_id_reads_bundles_from_page_context():
    class Response:
        ok = True

        async def text(self):
            return 'queryId:"fresh_query_id_2026",operationName:"AudioSpaceById"'

    class Request:
        async def get(self, url, timeout):
            assert url == "https://abs.twimg.com/responsive-web/client-web/modules.audio.abc123.js"
            assert timeout == 5_000
            return Response()

    class Page:
        async def evaluate(self, script):
            assert "modules\\.audio" in script
            return {
                "query_id": None,
                "urls": ["https://abs.twimg.com/responsive-web/client-web/modules.audio.abc123.js"],
                "module_count": 0,
            }

        request = Request()

    assert await graphql._discover_audio_space_query_id(Page()) == "fresh_query_id_2026"


@pytest.mark.asyncio
async def test_discover_audio_space_query_id_uses_loaded_webpack_modules():
    class Page:
        async def evaluate(self, script):
            assert "webpackChunk_twitter_responsive_web" in script
            return {"query_id": "webpack_query_id_2026", "urls": [], "module_count": 9}

    assert await graphql._discover_audio_space_query_id(Page()) == "webpack_query_id_2026"


@pytest.mark.asyncio
async def test_audio_space_request_url_encodes_graphql_json_parameters(monkeypatch):
    captured = {}
    monkeypatch.setattr(graphql, "_DISCOVERED_AUDIO_SPACE_BY_ID_QUERY_ID", "fresh_query_id_2026")

    async def capture_fetch(page, url, **kwargs):
        captured["url"] = url
        return None

    monkeypatch.setattr(graphql, "_page_fetch", capture_fetch)
    await graphql.fetch_audio_space_by_id(object(), "space-id-123")

    query = urlsplit(captured["url"]).query
    assert "%7B" in query
    params = parse_qs(query)
    assert '"id": "space-id-123"' in params["variables"][0]
    assert "spaces_2022_h2_clipping" in params["features"][0]


@pytest.mark.asyncio
async def test_bearer_token_prefers_token_captured_from_x_session_request():
    class Page:
        _xsa_bearer_token = "Bearer captured-current-token"

        async def evaluate(self, script):
            raise AssertionError("Captured session token should be preferred")

    assert await graphql._get_bearer_token(Page()) == "Bearer captured-current-token"


@pytest.mark.asyncio
async def test_graphql_distinguishes_valid_empty_groups_from_missing_fields(monkeypatch):
    async def empty_response(page, space_id):
        return {"participants": {"admins": [], "speakers": [], "listeners": []}}

    monkeypatch.setattr(graphql, "fetch_audio_space_by_id", empty_response)
    assert await graphql.fetch_space_participants_gql(object(), "space") == []

    async def missing_listeners(page, space_id):
        return {"participants": {"admins": [], "speakers": []}}

    monkeypatch.setattr(graphql, "fetch_audio_space_by_id", missing_listeners)
    assert await graphql.fetch_space_participants_gql(object(), "space") == []


@pytest.mark.asyncio
async def test_graphql_classifies_missing_participant_shape_as_unavailable(monkeypatch):
    async def missing_participants(page, space_id):
        return {"metadata": {"state": "Running"}}

    monkeypatch.setattr(graphql, "fetch_audio_space_by_id", missing_participants)
    assert await graphql.fetch_space_participants_gql(object(), "space") == []
