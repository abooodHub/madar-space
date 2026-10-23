"""The initial 10-account selection never deletes historical accounts or Spaces."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts import bootstrap_accounts
from xsa.config import Settings
from xsa.db import MonitoredAccount, Space, database, get_session, init_db
from xsa.maintenance_gate import arm_maintenance
from xsa.monitor.configuration import load_monitoring_config, save_monitoring_config


def test_bootstrap_chooses_ten_by_recorded_activity_and_preserves_all_rows(tmp_path, monkeypatch):
    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "db.sqlite3")
    cfg.max_active_accounts = 10
    init_db(cfg.db_path)
    with get_session() as db:
        for index in range(59):
            account = MonitoredAccount(
                username=f"account_{index:02}", active=True, added_at=datetime.now(UTC)
            )
            db.add(account)
            db.flush()
            for number in range(index % 15):
                db.add(Space(space_id=f"space_{index}_{number}", host_account_id=account.id))
        db.commit()
        before_spaces = db.query(Space).count()
    monkeypatch.setattr(bootstrap_accounts, "get_settings", lambda: cfg)
    try:
        with pytest.raises(RuntimeError, match="maintenance gate"):
            bootstrap_accounts.bootstrap(apply=True, expected_count=59)
        arm_maintenance(cfg.db_path.parent)
        result = bootstrap_accounts.bootstrap(apply=True, expected_count=59)
        assert result["before"] == {"total": 59, "active": 59}
        assert result["after"] == {"total": 59, "active": 10}
        assert len(result["selected"]) == 10
        with get_session() as db:
            assert db.query(MonitoredAccount).count() == 59
            assert db.query(Space).count() == before_spaces
            active = db.query(MonitoredAccount).filter_by(active=True).all()
            assert len(active) == 10 and all(account.priority == "P1" for account in active)
            assert db.query(MonitoredAccount).filter_by(priority="P2", active=False).count() > 0
            assert db.query(MonitoredAccount).filter_by(priority="P3", active=False).count() > 0
    finally:
        database._engine.dispose()
        database._engine = None
        database._SessionLocal = None


def test_monitoring_limits_persist_and_reject_invalid_values(tmp_path):
    state = tmp_path / "state"
    values = {
        "max_active_accounts": 10,
        "poll_interval_seconds": 300,
        "max_graphql_requests_per_cycle": 60,
    }
    save_monitoring_config(state, values)
    assert load_monitoring_config(state) == values
    (state / "monitoring-config.json").write_text(
        '{"max_active_accounts":true,"poll_interval_seconds":1,"max_graphql_requests_per_cycle":60}'
    )
    assert load_monitoring_config(state) == {"max_graphql_requests_per_cycle": 60}


def test_account_controls_enforce_cap_and_archive_without_deleting(tmp_path, monkeypatch):
    import importlib

    from fastapi import HTTPException

    api = importlib.import_module("xsa.api.app")

    cfg = Settings(data_dir=tmp_path / "data", db_path=tmp_path / "state" / "db.sqlite3")
    cfg.max_active_accounts = 10
    init_db(cfg.db_path)
    with get_session() as db:
        for index in range(11):
            db.add(
                MonitoredAccount(
                    username=f"user{index}", active=index < 10, priority="P1" if index < 10 else "P2"
                )
            )
        db.commit()
    monkeypatch.setattr(api, "_cfg", cfg)
    try:
        with pytest.raises(HTTPException) as blocked:
            api.toggle_account("user10")
        assert blocked.value.status_code == 409
        assert (
            api.update_accounts_bulk(api.AccountBulkIn(usernames=["user0"], active=False))["active_accounts"]
            == 9
        )
        assert api.toggle_account("user10").active is True
        api.remove_account("user0")
        with get_session() as db:
            assert db.query(MonitoredAccount).count() == 11
            archived = db.query(MonitoredAccount).filter_by(username="user0").one()
            assert archived.priority == "P3" and archived.active is False
    finally:
        database._engine.dispose()
        database._engine = None
        database._SessionLocal = None
