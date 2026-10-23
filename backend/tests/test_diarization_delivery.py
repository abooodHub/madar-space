from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import diarization_worker as worker
from xsa.diarization import run_offline_diarization


def test_worker_image_copy_set_can_start_cli_without_workspace_imports(tmp_path):
    root = Path(__file__).resolve().parents[2]
    for line in (root / "docker" / "diarization" / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            _copy, source, target = shlex.split(line)
            destination = tmp_path / target.removeprefix("/app/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / source, destination)
    result = subprocess.run(
        [sys.executable, str(tmp_path / "backend" / "scripts" / "diarization_worker.py"), "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "backend")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "doctor" in result.stdout


def test_blocked_job_retains_forced_rerun_and_committed_generation(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite3"
    generation = "a" * 32
    with sqlite3.connect(db_path) as db:
        db.executescript("""CREATE TABLE spaces(id INTEGER PRIMARY KEY,space_id TEXT,transcript_generation_id TEXT);
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,job_type TEXT,state TEXT,result TEXT,error TEXT,progress REAL,completed_at TEXT);
            INSERT INTO jobs VALUES(1,'diarize','queued','{"force_model":true}',NULL,0,NULL);""")
        db.execute("INSERT INTO spaces VALUES(1,'test123',?)", (generation,))
    monkeypatch.setattr(worker, "DB_PATH", db_path)
    worker._block_jobs_without_token()
    worker._set_job(1, "waiting_for_resources", result={"status": "WAITING_FOR_RESOURCES"})
    with sqlite3.connect(db_path) as db:
        payload = json.loads(db.execute("SELECT result FROM jobs WHERE id=1").fetchone()[0])
    assert payload["_options"]["force_model"] is True
    _audio, transcript = worker._input_paths("test123", tmp_path)
    assert transcript == tmp_path / "transcript" / "generations" / generation / "segments.json"


@pytest.mark.parametrize("change", ["audio", "transcript", "generation"])
def test_changed_source_never_publishes_new_diarization(tmp_path, change):
    audio, transcript, analysis = tmp_path / "audio.wav", tmp_path / "segments.json", tmp_path / "analysis"
    audio.write_bytes(b"original audio")
    transcript.write_text("[]")

    class Diarizer:
        model_id = "test"

        def diarize(self, _audio):
            if change == "audio":
                audio.write_bytes(b"changed audio")
            if change == "transcript":
                transcript.write_text('[{"text":"changed"}]')
            return []

    with pytest.raises(RuntimeError, match="inputs changed"):
        run_offline_diarization(
            space_id="test123",
            audio_path=audio,
            transcript_path=transcript,
            analysis_dir=analysis,
            diarizer=Diarizer(),
            duration=2,
            source_is_current=lambda: change != "generation",
        )
    assert not (analysis / "speaker_timeline.json").exists()
    assert not (analysis / "diarization_raw.json").exists()


def test_worker_refuses_inference_during_core_postprocessing(monkeypatch):
    monkeypatch.setattr(
        worker,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(
            json.dumps(
                {"active": [], "recording_ffmpeg_count": 0, "active_post_processing": ["busy123"]}
            ).encode()
        ),
    )
    with pytest.raises(worker.ActiveRecordingError, match="post-processing is active"):
        worker._api_status()


def test_merge_threshold_rebuilds_timeline_without_repeating_model(tmp_path):
    audio, transcript = tmp_path / "audio.wav", tmp_path / "segments.json"
    audio.write_bytes(b"source")
    transcript.write_text("[]")

    class Diarizer:
        model_id = "fixture"
        calls = 0

        def diarize(self, _audio):
            self.calls += 1
            return [{"start": 0, "end": 1, "speaker": "a"}, {"start": 1.2, "end": 2, "speaker": "a"}]

    model = Diarizer()
    options = dict(
        space_id="test123",
        audio_path=audio,
        transcript_path=transcript,
        analysis_dir=tmp_path / "analysis",
        diarizer=model,
        duration=3,
    )
    first = run_offline_diarization(**options, merge_gap_seconds=0.1)
    second = run_offline_diarization(**options, merge_gap_seconds=0.3)
    assert len(first["timeline"]["intervals"]) == 2
    assert len(second["timeline"]["intervals"]) == 1
    assert model.calls == 1
