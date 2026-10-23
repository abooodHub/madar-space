from __future__ import annotations

import builtins
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts import diarization_worker as worker


def test_missing_hf_token_blocks_job_without_leaking_secrets(tmp_path, monkeypatch):
    db_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE spaces (id INTEGER PRIMARY KEY, space_id TEXT NOT NULL);
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY, space_id INTEGER, job_type TEXT, state TEXT,
              attempt INTEGER DEFAULT 0, started_at TEXT, worker_id TEXT, error TEXT,
              result TEXT, progress REAL, completed_at TEXT
            );
            CREATE TABLE transcript_segments (text TEXT);
            INSERT INTO spaces VALUES (1, 'space-1');
            INSERT INTO jobs VALUES (7, 1, 'diarize', 'running', 1, NULL, NULL, NULL, NULL, 0, NULL);
            INSERT INTO transcript_segments VALUES ('preserve me');
        """)
    monkeypatch.setattr(worker, "DB_PATH", db_path)
    monkeypatch.setattr(worker, "DATA_DIR", tmp_path / "data")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with worker._connect() as db:
        row = db.execute(
            "SELECT jobs.id, jobs.space_id, jobs.result, spaces.space_id AS external_space_id "
            "FROM jobs JOIN spaces ON spaces.id=jobs.space_id WHERE jobs.id=7"
        ).fetchone()
    worker._process_job(row)
    with sqlite3.connect(db_path) as db:
        state, error, result = db.execute("SELECT state,error,result FROM jobs WHERE id=7").fetchone()
        transcript = db.execute("SELECT text FROM transcript_segments").fetchone()[0]
    assert state == "waiting_for_configuration"
    assert "BLOCKED_CONFIGURATION" in error
    assert '"status": "BLOCKED_CONFIGURATION"' in result
    assert "HF_TOKEN" not in result
    assert transcript == "preserve me"


def test_active_state_api_must_include_ffmpeg_guard(monkeypatch):
    monkeypatch.setattr(worker, "urlopen", lambda *_args, **_kwargs: _JsonResponse({"active": []}))
    try:
        worker._api_status()
    except worker.ActiveRecordingError as exc:
        assert "FFmpeg" in str(exc)
    else:
        raise AssertionError("worker must reject APIs that cannot verify FFmpeg idle state")


def test_diarization_waits_while_maintenance_gate_is_armed(monkeypatch):
    monkeypatch.setattr(
        worker,
        "urlopen",
        lambda *_args, **_kwargs: _JsonResponse(
            {"active": [], "recording_ffmpeg_count": 0, "maintenance_mode": True}
        ),
    )
    try:
        worker._api_status()
    except worker.ActiveRecordingError as exc:
        assert "Maintenance gate" in str(exc)
    else:
        raise AssertionError("worker must pause while the deployment gate is armed")


def test_doctor_reports_missing_hf_configuration_even_if_dependencies_are_missing(monkeypatch, capsys):
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("isolated dependencies unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert worker.doctor() == 3
    output = capsys.readouterr().out
    assert "BLOCKED_DEPENDENCIES" in output
    assert "BLOCKED_CONFIGURATION" in output
    assert "HF_TOKEN configured: False" in output


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter([])


# Keep urllib's json.load contract simple without opening a network socket.
def _response_read(self):
    import json

    return json.dumps(self.payload).encode()


_JsonResponse.read = _response_read


def test_postprocessing_diarization_enqueue_is_idempotent(tmp_path, monkeypatch):
    from xsa.db import Job, Space
    from xsa.db.models import Base
    from xsa.monitor import monitor as monitor_module

    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.sqlite3'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        space = Space(space_id="finished-space", state="analyzed")
        db.add(space)
        db.commit()
        space_id = space.id
    monkeypatch.setattr(monitor_module, "get_session", lambda: Session())
    monitor_module.AccountMonitor._enqueue_diarization(space_id)
    monitor_module.AccountMonitor._enqueue_diarization(space_id)
    with Session() as db:
        jobs = db.query(Job).filter_by(space_id=space_id, job_type="diarize").all()
        assert len(jobs) == 1
        assert jobs[0].state == "queued"


def test_requeue_is_diarization_only_and_updates_existing_job(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    space_dir = data_dir / "spaces" / "2026" / "10" / "06" / "12 - title - space-1"
    (space_dir / "audio").mkdir(parents=True)
    (space_dir / "transcript").mkdir()
    (space_dir / "audio" / "analysis.wav").write_bytes(b"existing audio")
    (space_dir / "transcript" / "segments.json").write_text("[]", encoding="utf-8")
    db_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE spaces (id INTEGER PRIMARY KEY, space_id TEXT NOT NULL);
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY, space_id INTEGER, job_type TEXT, state TEXT,
              attempt INTEGER DEFAULT 0, started_at TEXT, completed_at TEXT, progress REAL,
              error TEXT, result TEXT, worker_id TEXT, priority INTEGER, max_attempts INTEGER,
              created_at TEXT
            );
            INSERT INTO spaces VALUES (1, 'space-1');
            INSERT INTO jobs VALUES (4, 1, 'diarize', 'failed', 1, NULL, NULL, 1, 'previous', NULL, NULL, 5, 0, '2026-10-06');
        """)
    monkeypatch.setattr(worker, "DB_PATH", db_path)
    monkeypatch.setattr(worker, "DATA_DIR", data_dir)
    assert worker.requeue("space-1", force_model=True) == 0
    with sqlite3.connect(db_path) as db:
        state, attempt, result = db.execute("SELECT state,attempt,result FROM jobs WHERE id=4").fetchone()
        count = db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='diarize'").fetchone()[0]
    assert state == "queued"
    assert attempt == 0
    assert '"force_model": true' in result
    assert count == 1
