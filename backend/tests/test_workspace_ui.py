"""Workspace routes and independently reported optional-service health."""

from __future__ import annotations

from importlib import import_module

import pytest
from fastapi import HTTPException

from xsa.config import Settings
from xsa.db import Job, Space, get_session, init_db
from xsa.db import database as db_module


def test_workspace_routes_are_real_sections_and_reject_unknown_names():
    api = import_module("xsa.api.app")
    response = api.serve_workspace_section("library")
    assert response.status_code == 200
    assert b"/spaces" in response.body
    assert b"/workspace/accounts" in response.body
    assert b"__SECTION__" not in response.body
    assert b"renderOnboarding" in response.body
    assert b"127.0.0.1:6080/vnc.html" in response.body
    with pytest.raises(HTTPException) as error:
        api.serve_workspace_section("not-a-section")
    assert error.value.status_code == 404


def test_cookie_injection_is_not_exposed_as_an_api_route():
    api = import_module("xsa.api.app")
    assert "/auth/cookies" not in {route.path for route in api.app.routes}


@pytest.mark.asyncio
async def test_workspace_config_exposes_no_worker_secret(tmp_path, monkeypatch):
    api = import_module("xsa.api.app")
    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "db.sqlite3")
    monkeypatch.setattr(api, "_cfg", cfg)
    monkeypatch.setenv("HF_TOKEN", "secret-that-must-not-leak")
    payload = await api.workspace_config()
    assert payload["safe_to_deploy"] is False
    assert payload["hf_token_configured"] is None
    assert "secret-that-must-not-leak" not in str(payload)


@pytest.mark.asyncio
async def test_queued_diarization_does_not_mark_api_or_processing_blocked(tmp_path, monkeypatch):
    api = import_module("xsa.api.app")
    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "db.sqlite3")
    cfg.data_dir.mkdir()
    init_db(cfg.db_path)
    with get_session() as db:
        space = Space(space_id="queued-diarization", state="analyzed")
        db.add(space)
        db.flush()
        db.add(Job(space_id=space.id, job_type="diarize", state="queued"))
        db.commit()
    monkeypatch.setattr(api, "_cfg", cfg)
    monkeypatch.setattr(api, "_adapter", None)
    monkeypatch.setattr(api, "_monitor", None)
    monkeypatch.setattr(api, "_monitor_task", None)
    monkeypatch.setattr(api, "_ffmpeg_role_counts", lambda: {"recording": 0, "processing": 0})
    monkeypatch.delenv("HF_TOKEN", raising=False)
    try:
        payload = await api.health()
        assert payload["api_health"]["status"] == "healthy"
        assert payload["processing_health"]["status"] == "ok"
        assert payload["diarization_health"]["status"] == "queued"
        assert payload["diarization_health"]["waiting_jobs"] == 1
        assert payload["diarization_health"]["running_jobs"] == 0
        assert payload["diarization_health"]["worker"]["status"] == "unavailable"
    finally:
        if db_module._engine:
            db_module._engine.dispose()
        db_module._engine = None
        db_module._SessionLocal = None
