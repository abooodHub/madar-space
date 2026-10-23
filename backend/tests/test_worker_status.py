"""Liveness cannot be inferred from queued work or from stale/invalid reports."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts import diarization_worker as worker
from xsa.worker_status import WorkerHeartbeat, read_worker_status


@pytest.mark.parametrize("contents", [None, "{", "[]", '{"phase":"polling","updated_at":"bad"}'])
def test_missing_or_invalid_worker_report_is_unavailable(tmp_path, contents):
    if contents is not None:
        (tmp_path / "diarization-worker.json").write_text(contents)
    assert read_worker_status(tmp_path) == {"status": "unavailable", "reason": "no_recent_heartbeat"}


@pytest.mark.parametrize("age", [-10, 46, 3600])
def test_old_or_future_worker_report_cannot_imply_readiness(tmp_path, age):
    now = datetime.now(UTC)
    (tmp_path / "diarization-worker.json").write_text(
        json.dumps({"phase": "polling", "updated_at": (now - timedelta(seconds=age)).isoformat()})
    )
    assert read_worker_status(tmp_path, now=now)["status"] == "unavailable"


def test_blocked_heartbeat_is_current_and_stop_is_explicit(tmp_path):
    with WorkerHeartbeat(tmp_path) as heartbeat:
        heartbeat.set_phase("blocked", "model_access_not_configured")
        report = read_worker_status(tmp_path)
        assert report["status"] == "online"
        assert report["phase"] == "blocked"
        assert report["reason"] == "model_access_not_configured"
        assert "HF_TOKEN" not in (tmp_path / "diarization-worker.json").read_text()
    assert read_worker_status(tmp_path)["status"] == "stopped"


def test_worker_reports_missing_access_before_claiming_work(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "DB_PATH", tmp_path / "jobs.sqlite3")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    reports = []
    monkeypatch.setattr(
        worker, "_block_jobs_without_token", lambda: reports.append(read_worker_status(tmp_path))
    )
    monkeypatch.setattr(worker.sys, "argv", ["worker", "run"])

    def stop_after_iteration(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.time, "sleep", stop_after_iteration)
    monkeypatch.setattr(worker, "_claim", lambda: pytest.fail("Unconfigured worker must not claim jobs"))
    assert worker.main() == 0
    assert reports[0]["reason"] == "model_access_not_configured"
    assert read_worker_status(tmp_path)["status"] == "stopped"


def test_unwritable_report_does_not_interrupt_worker(tmp_path):
    state_dir = tmp_path / "file-not-directory"
    state_dir.write_text("preserve")
    with WorkerHeartbeat(state_dir) as heartbeat:
        heartbeat.set_phase("processing")
    assert state_dir.read_text() == "preserve"
