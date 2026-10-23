"""Disposable polling worker for isolated offline diarization.

The worker touches only analysis artifacts and the `jobs` row. It never opens
recordings for write and never changes Space/transcription status.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
import threading
import time
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xsa.diarization import (  # noqa: E402
    ActiveRecordingError,
    DiarizationUnavailable,
    GuardedPyannoteDiarizer,
    run_offline_diarization,
)
from xsa.paths import SpacePaths  # noqa: E402
from xsa.worker_status import WorkerHeartbeat  # noqa: E402

DB_PATH = Path(os.getenv("XSA_DB_PATH", "/state/xsa.sqlite3"))
DATA_DIR = Path(os.getenv("XSA_DATA_DIR", "/data"))
API_BASE = os.getenv("XSA_API_BASE", "http://app:8765")
POLL_SECONDS = max(5, int(os.getenv("XSA_DIARIZATION_POLL_SECONDS", "30")))
WAITING_STATES = ("queued", "waiting_for_resources", "waiting_for_configuration", "retrying")
OWNER_ID = f"diarization-{os.getpid()}-{uuid4().hex}"


def _api_status() -> dict:
    try:
        with urlopen(f"{API_BASE.rstrip('/')}/live", timeout=5) as response:
            value = json.load(response)
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ActiveRecordingError("Cannot verify recorder/API status; refusing diarization.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("active"), list):
        raise ActiveRecordingError("Recorder/API returned no verifiable active-session list.")
    if "recording_ffmpeg_count" not in value:
        raise ActiveRecordingError("Recorder/API lacks the FFmpeg safety check; refusing diarization.")
    if value.get("maintenance_mode"):
        raise ActiveRecordingError("Maintenance gate is armed; offline model jobs are paused.")
    if not isinstance(value.get("active_post_processing"), list):
        raise ActiveRecordingError("Recorder/API lacks post-processing safety status; refusing inference.")
    if value["active_post_processing"]:
        raise ActiveRecordingError("Core post-processing is active; offline model jobs are paused.")
    return value


def _active_ids() -> list[str]:
    status = _api_status()
    return [
        str(row.get("space_id"))
        for row in status["active"]
        if isinstance(row, dict) and row.get("state") == "recording" and row.get("space_id")
    ]


def _recording_processes() -> list[tuple[str, str]]:
    status = _api_status()
    count = int(status.get("recording_ffmpeg_count", -1))
    if count < 0:
        raise ActiveRecordingError("Recorder/API returned an invalid FFmpeg safety status.")
    return [(f"app-{index}", "ffmpeg recording_000.mkv") for index in range(count)]


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    for name, kind in {
        "owner_id": "TEXT",
        "claimed_at": "TEXT",
        "heartbeat_at": "TEXT",
        "lease_expires_at": "TEXT",
        "attempt_id": "TEXT",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
    connection.commit()
    return connection


def _claim() -> sqlite3.Row | None:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC).replace(tzinfo=None)
        db.execute(
            "UPDATE jobs SET state='queued', owner_id=NULL, worker_id=NULL, error='Expired worker lease recovered' "
            "WHERE job_type='diarize' AND state IN ('running','cancelling') "
            "AND (lease_expires_at IS NULL OR julianday(lease_expires_at)<julianday(?))",
            (now.isoformat(),),
        )
        row = db.execute(
            "SELECT jobs.id, jobs.space_id, jobs.result, spaces.space_id AS external_space_id "
            "FROM jobs JOIN spaces ON spaces.id=jobs.space_id "
            f"WHERE jobs.job_type='diarize' AND jobs.state IN ({','.join('?' for _ in WAITING_STATES)}) "
            "ORDER BY jobs.priority DESC, jobs.id LIMIT 1",
            WAITING_STATES,
        ).fetchone()
        if row:
            db.execute(
                "UPDATE jobs SET state='running', attempt=attempt+1, started_at=?, "
                "worker_id=?, error=NULL, owner_id=?, claimed_at=?, heartbeat_at=?, lease_expires_at=?, attempt_id=? WHERE id=?",
                (
                    now.isoformat(),
                    f"diarization-{os.getpid()}",
                    OWNER_ID,
                    now.isoformat(),
                    now.isoformat(),
                    (now + timedelta(seconds=120)).isoformat(),
                    uuid4().hex,
                    row["id"],
                ),
            )
        db.commit()
        return row


def _block_jobs_without_token() -> None:
    """Park pending work until credentials change; never spin on gated access."""
    _block_pending_jobs("Hugging Face model access is not configured.")


def _job_options(value: str | None) -> dict:
    try:
        payload = json.loads(value or "{}")
        options = payload.get("_options", payload)
        return {"force_model": bool(options["force_model"])} if "force_model" in options else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def _block_pending_jobs(reason: str) -> None:
    with _connect() as db:
        rows = db.execute(
            "SELECT id,result FROM jobs WHERE job_type='diarize' "
            "AND state IN ('queued','retrying','waiting_for_resources')"
        ).fetchall()
        for row in rows:
            payload = {
                "status": "BLOCKED_CONFIGURATION",
                "reason": reason,
                "_options": _job_options(row["result"]),
            }
            db.execute(
                "UPDATE jobs SET state='waiting_for_configuration', error=?, result=? WHERE id=?",
                (f"BLOCKED_CONFIGURATION: {reason}", json.dumps(payload), row["id"]),
            )


def _missing_dependencies() -> list[str]:
    missing = []
    for module in ("torch", "torchaudio", "torchcodec", "pyannote.audio"):
        try:
            available = importlib.util.find_spec(module) is not None
        except (ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing.append(module)
    return missing


def _set_job(
    job_id: int, state: str, *, error: str | None = None, result: dict | None = None, completed: bool = False
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    with _connect() as db:
        row = db.execute(
            "SELECT result FROM jobs WHERE id=? AND (owner_id IS NULL OR owner_id=?)",
            (job_id, OWNER_ID),
        ).fetchone()
        if row is None:
            return
        result = {**(result or {}), "_options": _job_options(row["result"])}
        db.execute(
            "UPDATE jobs SET state=?, error=?, result=?, progress=?, completed_at=? WHERE id=? AND (owner_id IS NULL OR owner_id=?)",
            (
                state,
                error,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                1.0 if completed else 0.0,
                now if completed else None,
                job_id,
                OWNER_ID,
            ),
        )


def _find_space(space_id: str) -> Path:
    paths = SpacePaths.resolve_existing(DATA_DIR, space_id)
    if paths is None:
        raise FileNotFoundError(f"Space directory not found: {space_id}")
    return paths.root


def _input_paths(space_id: str, root: Path) -> tuple[Path, Path]:
    """Read the committed transcript generation without loading the app ORM."""
    generation = None
    with _connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(spaces)")}
        if "transcript_generation_id" in columns:
            row = db.execute(
                "SELECT transcript_generation_id FROM spaces WHERE space_id=?", (space_id,)
            ).fetchone()
            generation = row[0] if row else None
    if generation:
        if not re.fullmatch(r"[a-f0-9]{32}", generation):
            raise ValueError("Committed transcript generation is invalid")
        transcript = root / "transcript" / "generations" / generation / "segments.json"
    else:
        transcript = root / "transcript" / "segments.json"
    return root / "audio" / "analysis.wav", transcript


def _audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
    if duration <= 0:
        raise ValueError("Analysis audio has no readable samples.")
    return duration


def _process_job(job: sqlite3.Row) -> None:
    from xsa.artifact_lock import artifact_lock

    stop = threading.Event()

    def heartbeat():
        while not stop.wait(10):
            now = datetime.now(UTC).replace(tzinfo=None)
            with _connect() as db:
                db.execute(
                    "UPDATE jobs SET heartbeat_at=?, lease_expires_at=? WHERE id=? AND owner_id=? AND state='running'",
                    (now.isoformat(), (now + timedelta(seconds=120)).isoformat(), job["id"], OWNER_ID),
                )

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with artifact_lock(DATA_DIR / "_maintenance" / f"diarize-{job['external_space_id']}"):
            _process_owned_job(job)
    finally:
        stop.set()
        thread.join(timeout=35)


def _process_owned_job(job: sqlite3.Row) -> None:
    job_id, space_id = int(job["id"]), str(job["external_space_id"])
    if not os.getenv("HF_TOKEN"):
        _set_job(
            job_id,
            "waiting_for_configuration",
            error="BLOCKED_CONFIGURATION: HF model access is not configured.",
            result={
                "status": "BLOCKED_CONFIGURATION",
                "reason": "Hugging Face model access is not configured.",
            },
        )
        return
    try:
        paths = _find_space(space_id)
        audio, transcript = _input_paths(space_id, paths)
        if not audio.is_file() or not transcript.is_file():
            raise FileNotFoundError("Existing analysis.wav and transcript/segments.json are required.")
        duration = _audio_duration(audio)
        _api_status()  # Fail closed if the app is unavailable or too old to check FFmpeg.
        diarizer = GuardedPyannoteDiarizer(
            token=os.environ["HF_TOKEN"],
            device=os.getenv("XSA_DIARIZATION_DEVICE", "auto"),
            active_space_ids=_active_ids,
            process_rows=_recording_processes,
            poll_seconds=3.0,
        )
        queued_options = _job_options(job["result"])
        mapping_path = paths / "analysis" / "speaker_identity_mappings.json"
        mappings = json.loads(mapping_path.read_text(encoding="utf-8")) if mapping_path.is_file() else {}
        if not isinstance(mappings, dict):
            raise ValueError("speaker_identity_mappings.json must contain an object.")
        result = run_offline_diarization(
            space_id=space_id,
            audio_path=audio,
            transcript_path=transcript,
            analysis_dir=paths / "analysis",
            diarizer=diarizer,
            duration=duration,
            active_space_ids=_active_ids,
            process_rows=_recording_processes,
            identity_mappings=mappings,
            force_model=bool(queued_options.get("force_model")),
            merge_gap_seconds=float(os.getenv("XSA_DIARIZATION_MERGE_GAP_SECONDS", "0.1")),
            source_is_current=lambda: _input_paths(space_id, paths)[1] == transcript,
        )
        timeline = result["timeline"]
        summary = {
            "status": result["status"],
            "speaker_count": len(timeline["speakers"]),
            "interval_count": len(timeline["intervals"]),
            "speech_seconds_unique": timeline["coverage"]["speech_seconds_unique"],
            "overlap_seconds": timeline["coverage"]["overlap_seconds"],
            "device": diarizer.actual_device,
            "execution_metrics": diarizer.execution_metrics,
            "processing_seconds": diarizer.execution_metrics.get("processing_seconds"),
        }
        _set_job(job_id, "completed", result=summary, completed=True)
    except ActiveRecordingError as exc:
        _set_job(job_id, "waiting_for_resources", error=str(exc), result={"status": "WAITING_FOR_RESOURCES"})
    except DiarizationUnavailable as exc:
        _set_job(
            job_id, "waiting_for_configuration", error=str(exc), result={"status": "BLOCKED_CONFIGURATION"}
        )
    except Exception as exc:
        # Keep failure contained to this job; never mutate the Space or transcript.
        _set_job(
            job_id,
            "failed",
            error=f"{type(exc).__name__}: {str(exc)[:500]}",
            result={"status": "failed", "error_type": type(exc).__name__},
            completed=True,
        )


def doctor(*, load_model: bool = False, space_id: str | None = None) -> int:
    print("XSpaceAnalyzer isolated diarization preflight")
    print(f"Python: {sys.version.split()[0]}")
    try:
        import pyannote.audio
        import torch
        import torchaudio
        import torchcodec
    except Exception as exc:
        print(f"BLOCKED_DEPENDENCIES: {type(exc).__name__}; build/use the isolated diarization image.")
        print(f"HF_TOKEN configured: {bool(os.getenv('HF_TOKEN'))}")
        if not os.getenv("HF_TOKEN"):
            print("BLOCKED_CONFIGURATION: Hugging Face model access is not configured.")
            return 3
        return 2
    cuda = bool(torch.cuda.is_available())
    print(f"torch: {torch.__version__}")
    print(f"torchaudio: {torchaudio.__version__}")
    print(f"torchcodec: {torchcodec.__version__}")
    print(f"pyannote.audio: {pyannote.audio.__version__}")
    print(f"CUDA available: {cuda}")
    print(f"CUDA runtime: {torch.version.cuda or 'none'}")
    print(f"GPU: {torch.cuda.get_device_name(0) if cuda else 'none'}")
    print("CPU backend: available")
    if cuda:
        free, total = torch.cuda.mem_get_info(0)
        print(
            f"VRAM before model load: used={((total - free) / (1024 * 1024)):.1f} MiB, "
            f"free={(free / (1024 * 1024)):.1f} MiB, total={(total / (1024 * 1024)):.1f} MiB"
        )
    print(f"HF_TOKEN configured: {bool(os.getenv('HF_TOKEN'))}")
    cache = Path(os.getenv("HF_HOME", "/model-cache"))
    try:
        cache.mkdir(parents=True, exist_ok=True)
        probe = cache / ".xsa-write-check"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        print(f"Model cache writable: yes ({cache})")
    except OSError:
        print(f"Model cache writable: no ({cache})")
        return 2
    print(f"Data directory accessible: {DATA_DIR.is_dir()} ({DATA_DIR})")
    if space_id:
        try:
            paths = _find_space(space_id)
            audio, transcript = _input_paths(space_id, paths)
            print(f"Analysis audio readable: {audio.is_file()} ({audio})")
            print(f"Transcript readable: {transcript.is_file()} ({transcript})")
            if audio.is_file():
                print(f"Audio duration seconds: {_audio_duration(audio):.4f}")
            if not audio.is_file() or not transcript.is_file():
                return 2
            output_dir = paths / "analysis"
            output_dir.mkdir(parents=True, exist_ok=True)
            probe = output_dir / ".xsa-write-check"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            print(f"Analysis output writable: yes ({output_dir})")
        except Exception as exc:
            print(f"Analysis input check failed: {type(exc).__name__}")
            return 2
    try:
        active = _api_status()
        idle = not active["active"] and int(active["recording_ffmpeg_count"]) == 0
        print(f"Recorder safety: {'idle' if idle else 'active; inference blocked'}")
    except ActiveRecordingError:
        idle = False
        print("Recorder safety: unavailable; inference blocked")
    print("Model loaded: no (preflight does not download/load model)")
    if load_model:
        if not os.getenv("HF_TOKEN"):
            print("BLOCKED_CONFIGURATION: accept model conditions and configure HF_TOKEN locally.")
            return 3
        if not idle:
            print("BLOCKED_RESOURCES: wait until all recordings and recording FFmpeg processes stop.")
            return 4
        from xsa.diarization import PyannoteDiarizer

        try:
            model = PyannoteDiarizer(token=os.environ["HF_TOKEN"])
            model._load()
            print(f"Model loaded: yes; device={model.actual_device}")
            if cuda:
                free, total = torch.cuda.mem_get_info(0)
                print(
                    f"VRAM after model load: used={((total - free) / (1024 * 1024)):.1f} MiB, "
                    f"free={(free / (1024 * 1024)):.1f} MiB, total={(total / (1024 * 1024)):.1f} MiB"
                )
            print(f"Model execution diagnostics: {json.dumps(model.execution_metrics, ensure_ascii=False)}")
        except Exception as exc:
            print(
                f"BLOCKED_MODEL_ACCESS: {type(exc).__name__}; accept the Community-1 model conditions "
                "and verify that HF_TOKEN has read access. Token value was not displayed."
            )
            return 5
    if not os.getenv("HF_TOKEN"):
        print("BLOCKED_CONFIGURATION: Hugging Face model access is not configured.")
        return 3
    return 0 if idle else 4


def requeue(space_id: str, *, force_model: bool = False) -> int:
    """Re-run only the diarization stage; preserve all source/transcript data."""
    try:
        paths = _find_space(space_id)
        audio, transcript = _input_paths(space_id, paths)
        if not audio.is_file() or not transcript.is_file():
            print("Cannot queue: existing analysis audio and transcript segments are required.")
            return 2
        with _connect() as db:
            row = db.execute("SELECT id FROM spaces WHERE space_id=?", (space_id,)).fetchone()
            if not row:
                print("Cannot queue: Space is missing from the application database.")
                return 2
            job = db.execute(
                "SELECT id,state FROM jobs WHERE space_id=? AND job_type='diarize' ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            options = json.dumps({"force_model": force_model})
            if job:
                if job["state"] in ("queued", "running"):
                    print(f"Job {job['id']} is already {job['state']}.")
                    return 0
                db.execute(
                    "UPDATE jobs SET state='queued', attempt=0, started_at=NULL, completed_at=NULL, "
                    "progress=0, error=NULL, result=?, worker_id=NULL WHERE id=?",
                    (options, job["id"]),
                )
                job_id = int(job["id"])
            else:
                cursor = db.execute(
                    "INSERT INTO jobs(space_id,job_type,state,priority,attempt,max_attempts,created_at,result) "
                    "VALUES(?,'diarize','queued',5,0,0,?,?)",
                    (row["id"], datetime.now(UTC).replace(tzinfo=None).isoformat(), options),
                )
                job_id = int(cursor.lastrowid)
        print(f"Queued diarization-only job {job_id} for {space_id}; force_model={force_model}.")
        return 0
    except Exception as exc:
        print(f"Could not queue diarization: {type(exc).__name__}.")
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("doctor", help="Check isolated runtime, GPU, cache and recorder safety")
    preflight.add_argument(
        "--load-model", action="store_true", help="Download/load the model after all safety checks"
    )
    preflight.add_argument("--space-id")
    sub.add_parser("run", help="Poll the shared database job queue")
    rerun = sub.add_parser("requeue", help="Queue diarization only for a completed Space")
    rerun.add_argument("space_id")
    rerun.add_argument(
        "--force-model",
        action="store_true",
        help="Run the model again; archive the prior raw output and invalidate old fingerprinted mappings",
    )
    args = parser.parse_args()
    if args.command == "doctor":
        return doctor(load_model=args.load_model, space_id=args.space_id)
    if args.command == "requeue":
        return requeue(args.space_id, force_model=args.force_model)
    print(f"Worker polling {DB_PATH}; cache is {os.getenv('HF_HOME', '/model-cache')}", flush=True)
    with WorkerHeartbeat(DB_PATH.parent) as heartbeat:
        while True:
            try:
                if not os.getenv("HF_TOKEN"):
                    heartbeat.set_phase("blocked", "model_access_not_configured")
                    _block_jobs_without_token()
                    time.sleep(max(POLL_SECONDS, 300))
                    continue
                missing = _missing_dependencies()
                if missing:
                    heartbeat.set_phase("blocked", "dependencies_missing")
                    _block_pending_jobs("Isolated worker dependencies are unavailable: " + ", ".join(missing))
                    time.sleep(max(POLL_SECONDS, 300))
                    continue
                heartbeat.set_phase("polling")
                job = _claim()
                if job:
                    heartbeat.set_phase("processing")
                    _process_job(job)
                else:
                    time.sleep(POLL_SECONDS)
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                heartbeat.set_phase("error", "worker_loop_error")
                print(f"Worker loop error: {type(exc).__name__}; retrying in {POLL_SECONDS}s", flush=True)
                time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
