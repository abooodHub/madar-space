"""Read-only inventory and media health audit for archived X Spaces."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func

from xsa.db import Job, MonitoredAccount, Space, SpaceParticipant, TranscriptSegment, get_session
from xsa.paths import safe_id

MEDIA_SUFFIXES = {".mkv", ".mp4", ".webm", ".mov"}
ASSET_DIRS = {"audio", "avatars", "clips", "screenshots", "transcript", "analysis"}
SPACE_ID_RE = re.compile(r"([A-Za-z0-9]{10,})$")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _count_jsonl(path: Path) -> int:
    if not path.is_file():
        return 0
    count = 0
    try:
        with path.open("rb") as stream:
            for line in stream:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def _media_files(folder: Path) -> list[Path]:
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES),
        key=lambda p: (p.name != "recording.mkv", p.name),
    )


def probe_media(path: Path, *, timeout_seconds: int = 30) -> dict[str, Any]:
    """Probe one file with ffprobe without changing it."""
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "duration_seconds": None,
        "audio_valid": False,
        "video_valid": False,
        "audio_duration_seconds": None,
        "video_duration_seconds": None,
        "streams": [],
        "valid": False,
        "error": None,
    }
    if not result["exists"]:
        result["error"] = "missing"
        return result
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,size,start_time:stream=codec_type,codec_name,duration,sample_rate,channels,width,height,nb_frames",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            result["error"] = (completed.stderr or "ffprobe failed")[-500:]
            return result
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams") or []
        fmt = payload.get("format") or {}
        durations: dict[str, list[float]] = {"audio": [], "video": []}
        for stream in streams:
            kind = stream.get("codec_type")
            entry = {
                key: stream.get(key)
                for key in (
                    "codec_type",
                    "codec_name",
                    "duration",
                    "sample_rate",
                    "channels",
                    "width",
                    "height",
                    "nb_frames",
                )
            }
            result["streams"].append(entry)
            try:
                if kind in durations and stream.get("duration") not in (None, "N/A"):
                    durations[kind].append(float(stream["duration"]))
            except (TypeError, ValueError):
                pass
        result["audio_valid"] = any(s.get("codec_type") == "audio" for s in streams)
        result["video_valid"] = any(s.get("codec_type") == "video" for s in streams)
        result["audio_duration_seconds"] = max(durations["audio"], default=None)
        result["video_duration_seconds"] = max(durations["video"], default=None)
        try:
            result["duration_seconds"] = float(fmt["duration"])
        except (KeyError, TypeError, ValueError):
            result["duration_seconds"] = max(durations["audio"] + durations["video"], default=None)
        result["valid"] = bool(streams and (result["audio_valid"] or result["video_valid"]))
        result["audio_duration_source"] = "stream" if result["audio_duration_seconds"] is not None else None
        result["video_duration_source"] = "stream" if result["video_duration_seconds"] is not None else None
        if result["audio_duration_seconds"] is None and result["audio_valid"]:
            result["audio_duration_seconds"] = result["duration_seconds"]
            result["audio_duration_source"] = "container_estimate"
        if result["video_duration_seconds"] is None and result["video_valid"]:
            result["video_duration_seconds"] = result["duration_seconds"]
            result["video_duration_source"] = "container_estimate"
        if not result["valid"]:
            result["error"] = "no decodable audio/video streams"
    except FileNotFoundError:
        result["error"] = "ffprobe unavailable"
    except subprocess.TimeoutExpired:
        result["error"] = "ffprobe timeout"
    except (OSError, json.JSONDecodeError) as exc:
        result["error"] = type(exc).__name__
    return result


def _space_id(folder: Path, metadata: dict[str, Any] | None) -> str | None:
    if metadata and metadata.get("space_id"):
        return safe_id(str(metadata["space_id"]))
    match = SPACE_ID_RE.search(folder.name)
    return match.group(1) if match else None


def discover_space_folders(spaces_root: Path) -> list[Path]:
    """Find space roots; never treat nested asset directories as separate Spaces."""
    found: set[Path] = set()
    if not spaces_root.is_dir():
        return []
    for current, directories, files in __import__("os").walk(spaces_root):
        directories[:] = [name for name in directories if name.lower() not in ASSET_DIRS]
        folder = Path(current)
        has_media = any(Path(name).suffix.lower() in MEDIA_SUFFIXES for name in files)
        has_space_artifact = any(
            name in files
            for name in (
                "space.json",
                "transcript.json",
                "speaker_events.jsonl",
                "speaker_observations.jsonl",
                "speaker_timeline.json",
                "participants.json",
                "analysis.json",
                "summary.json",
            )
        )
        if has_media or has_space_artifact or SPACE_ID_RE.search(folder.name):
            found.add(folder)
    return sorted(found, key=lambda path: str(path).casefold())


def audit_spaces(data_dir: Path) -> dict[str, Any]:
    """Build a persistent-friendly inventory from files, ffprobe, and SQLite."""
    spaces_root = data_dir / "spaces"
    folders = discover_space_folders(spaces_root)
    folder_data: list[dict[str, Any]] = []
    for folder in folders:
        metadata = _read_json(folder / "space.json")
        sid = _space_id(folder, metadata)
        if not sid:
            # Some imported recordings have their ID only in their media parent path.
            sid = _space_id(folder.parent, None)
        folder_data.append({"folder": folder, "metadata": metadata or {}, "space_id": sid})

    db_rows: dict[str, dict[str, Any]] = {}
    participant_counts: Counter[str] = Counter()
    transcript_counts: Counter[str] = Counter()
    job_rows: dict[str, dict[str, Any]] = {}
    with get_session() as db:
        spaces = (
            db.query(Space, MonitoredAccount.username, MonitoredAccount.display_name)
            .outerjoin(MonitoredAccount, Space.host_account_id == MonitoredAccount.id)
            .all()
        )
        for space, host_username, host_display_name in spaces:
            db_rows[space.space_id] = {
                "id": space.id,
                "state": space.state,
                "title": space.title,
                "url": space.url,
                "host_username": host_username,
                "host_display_name": host_display_name,
                "detected_at": space.detected_at.isoformat() if space.detected_at else None,
                "started_at": space.started_at.isoformat() if space.started_at else None,
                "ended_at": space.ended_at.isoformat() if space.ended_at else None,
                "recording_started_at": space.recording_started_at.isoformat()
                if space.recording_started_at
                else None,
                "recording_ended_at": space.recording_ended_at.isoformat()
                if space.recording_ended_at
                else None,
                "metadata": space.space_metadata or {},
                "transcript_generation_id": space.transcript_generation_id,
            }
        counts = (
            db.query(Space.space_id, func.count(SpaceParticipant.id))
            .join(SpaceParticipant, SpaceParticipant.space_id == Space.id)
            .group_by(Space.space_id)
            .all()
        )
        participant_counts.update({sid: count for sid, count in counts})
        counts = (
            db.query(Space.space_id, func.count(TranscriptSegment.id))
            .join(TranscriptSegment, TranscriptSegment.space_id == Space.id)
            .group_by(Space.space_id)
            .all()
        )
        transcript_counts.update({sid: count for sid, count in counts})
        jobs = (
            db.query(Space.space_id, Job.job_type, Job.state, Job.error, Job.result)
            .join(Job, Job.space_id == Space.id)
            .order_by(Job.id)
            .all()
        )
        for sid, job_type, state, error, result in jobs:
            job_rows.setdefault(sid, {})[job_type] = {
                "state": state,
                "failure_reason": error,
                "result": result,
            }

    records: list[dict[str, Any]] = []
    for item in folder_data:
        folder: Path = item["folder"]
        sid: str | None = item["space_id"]
        metadata: dict[str, Any] = item["metadata"]
        db_info = db_rows.get(sid or "", {})
        media_paths = _media_files(folder)
        media = [probe_media(path) for path in media_paths]
        mkvs = [m for m in media if Path(m["path"]).suffix.lower() == ".mkv"]
        merged = next((m for m in mkvs if Path(m["path"]).name == "recording.mkv" and m.get("valid")), None)
        source_media = merged or next((m for m in mkvs if m.get("valid")), None) or next(iter(mkvs), None)
        if not merged:
            valid_durations = [m.get("duration_seconds") or 0 for m in mkvs if m.get("valid")]
            if source_media and len(valid_durations) > 1:
                source_media = dict(source_media, duration_seconds=sum(valid_durations))
        playback_media = next((m for m in media if Path(m["path"]).name == "playback.mp4"), None)
        if source_media is None and media:
            source_media = media[0]
        media_duration = (source_media or playback_media or {}).get("duration_seconds")

        generation_id = db_info.get("transcript_generation_id")
        transcript_folder = (
            folder / "transcript" / "generations" / generation_id if generation_id else folder / "transcript"
        )
        transcript_data = (
            _read_json(transcript_folder / "transcript.json")
            if generation_id
            else _read_json(folder / "transcript.json")
        )
        if transcript_data is None:
            transcript_data = _read_json(transcript_folder / "normalized.json")
        segments = (transcript_data or {}).get("segments") or []
        segments_count = transcript_counts.get(sid, 0) if not segments and sid else len(segments)
        word_file = _read_json(transcript_folder / "words.json")
        if isinstance(word_file, list):
            words_count = len(word_file)
        else:
            words_count = sum(len(s.get("words") or []) for s in segments if isinstance(s, dict))
        if words_count == 0 and segments:
            words_count = sum(len(str(s.get("text") or "").split()) for s in segments if isinstance(s, dict))
        full_text = (transcript_data or {}).get("full_text") or ""
        if not words_count and full_text:
            words_count = len(str(full_text).split())

        speaker_observations = _count_jsonl(folder / "speaker_observations.jsonl")
        speaker_events = _count_jsonl(folder / "speaker_events.jsonl") + _count_jsonl(
            folder / "speaker_events_live.jsonl"
        )
        participants_file = _read_json(folder / "participants.json")
        participants = participant_counts.get(sid or "", 0)
        if participants_file:
            participants = max(participants, len(participants_file.get("participants") or []))
        # speaker_timeline.json is generated from X's participant UI events. It is
        # useful identity evidence, but it is not acoustic diarization.
        diarization = _read_json(folder / "analysis" / "diarization_raw.json")
        if not diarization:
            speakers_candidate = _read_json(folder / "analysis" / "speakers.json")
            if (
                speakers_candidate
                and speakers_candidate.get("acoustic_diarization", {}).get("status") == "DONE"
            ):
                diarization = speakers_candidate.get("acoustic_diarization")
        summary = (
            _read_json(folder / "analysis" / "overview.json")
            or _read_json(folder / "summary.json")
            or _read_json(folder / "analysis.json")
        )
        # A failed preflight/join never produced a recording and must not be
        # counted as an interrupted capture. Require evidence capture began.
        known_interrupted = bool(
            db_info.get("state") in {"development_interrupted", "interrupted", "recovered_stale"}
            or metadata.get("recording_interruption")
            or (
                db_info.get("state") == "error"
                and db_info.get("recording_started_at")
                and not db_info.get("recording_ended_at")
            )
        )
        capture_active = db_info.get("state") in {"recording", "finalizing"}
        joining = db_info.get("state") == "joining"
        active = capture_active or joining
        growing = bool(
            capture_active
            or any((datetime.now().timestamp() - path.stat().st_mtime) < 120 for path in media_paths)
        )
        source_valid = bool(source_media and source_media.get("valid"))
        has_audio = bool(source_media and source_media.get("audio_valid"))
        has_video = bool(source_media and source_media.get("video_valid"))
        duration = media_duration or 0.0
        completeness = _read_json(folder / "recording_manifest.json") or {}
        invalid_raw = [
            m
            for m in mkvs
            if re.fullmatch(r"recording_\d{3}\.mkv", Path(m["path"]).name) and not m.get("valid")
        ]
        if invalid_raw:
            completeness = {**completeness, "status": "PARTIAL"}
        cleanly_finalized = bool(
            source_valid
            and not active
            and not growing
            and not known_interrupted
            and completeness.get("status") != "PARTIAL"
        )
        flags: list[str] = []
        if completeness.get("status") == "PARTIAL":
            flags.append("PARTIAL_RECORDING")
        if not media_paths:
            flags.append("NO_MEDIA")
        elif not source_valid:
            flags.append("CORRUPT_MEDIA")
        elif not has_audio:
            flags.append("NO_AUDIO")
        if source_valid and not has_video:
            flags.append("NO_VIDEO")
        if known_interrupted:
            flags.append("INTERRUPTED_RECORDING")
        if capture_active:
            flags.append("ACTIVE_RECORDING")
        if joining:
            flags.append("JOINING")
        if (
            source_valid
            and has_audio
            and cleanly_finalized
            and not (transcript_data is not None and not segments and not full_text)
        ):
            flags.append("GOOD_RECORDING")
        elif source_valid and has_audio and cleanly_finalized and transcript_data is not None:
            flags.append("VALID_CONTAINER_NO_SPEECH")
        if (
            playback_media
            and playback_media.get("valid")
            and source_media
            and source_media["path"] != playback_media["path"]
        ):
            flags.append("PLAYBACK_READY")
        if segments_count:
            flags.append("READY_FOR_ANALYSIS")
            if not media_paths:
                flags.append("TRANSCRIPT_ONLY")
        elif transcript_data is not None and not segments and not full_text:
            flags.append("NO_SPEECH")
        elif source_valid and has_audio and not active:
            flags.append("NEEDS_TRANSCRIPTION")
        elif media_paths and (active or not source_valid):
            flags.append("MEDIA_NOT_FINAL")
        if diarization:
            flags.append("DIARIZED")
        elif source_valid and has_audio:
            flags.append("NEEDS_DIARIZATION")
        if summary and segments_count:
            flags.append("ANALYZED")
        if not has_audio and source_valid:
            flags.append("NO_AUDIO")

        stage_jobs = job_rows.get(sid or "", {})
        transcript_state = (
            "DONE" if segments_count else "NO_SPEECH" if "NO_SPEECH" in flags else "NOT_TRANSCRIBED"
        )
        analysis_state = "DONE" if summary else ("NOT_STARTED" if segments_count else "BLOCKED")
        diarization_state = "DONE" if diarization else "NOT_STARTED"
        for key, stage in (
            ("transcribe", "TRANSCRIPT"),
            ("analyze", "SEMANTIC_ANALYSIS"),
            ("diarize", "DIARIZATION"),
            ("post_process", "POST_PROCESSING"),
        ):
            job = stage_jobs.get(key)
            if job and job.get("state") in {"queued", "running"}:
                if stage == "TRANSCRIPT":
                    transcript_state = job["state"].upper()
                elif stage == "DIARIZATION":
                    diarization_state = job["state"].upper()
                elif stage == "SEMANTIC_ANALYSIS":
                    analysis_state = job["state"].upper()
                else:
                    post_processing_state = job["state"].upper()
            elif job and job.get("state") == "failed":
                if stage == "TRANSCRIPT":
                    transcript_state = "FAILED"
                elif stage == "DIARIZATION":
                    diarization_state = "FAILED"
                elif stage == "SEMANTIC_ANALYSIS":
                    analysis_state = "FAILED"
                else:
                    post_processing_state = "FAILED"
            elif stage == "POST_PROCESSING" and job and job.get("state") == "completed":
                post_processing_state = "DONE"
            elif stage == "POST_PROCESSING" and job and job.get("state") == "partial":
                post_processing_state = "PARTIAL"
            elif stage == "POST_PROCESSING":
                post_processing_state = "NOT_STARTED"
        if diarization_state == "NOT_STARTED" and source_valid and has_audio:
            diarization_state = "BLOCKED_HF_TOKEN" if not _hf_token_configured() else "NOT_STARTED"
        if "NO_SPEECH" in flags:
            analysis_state = "NO_SPEECH"

        folder_start = _folder_time(folder)
        title = metadata.get("title") or db_info.get("title") or ""
        host = metadata.get("host") or metadata.get("host_username") or db_info.get("host_username")
        if isinstance(host, dict):
            host = host.get("username") or host.get("screen_name")
        observed_host = _observed_host(folder, participants_file)
        stale_tail = None
        if (folder / "space_end_events.jsonl").is_file():
            for line in (
                (folder / "space_end_events.jsonl").read_text(encoding="utf-8", errors="ignore").splitlines()
            ):
                try:
                    event = json.loads(line)
                    if event.get("event") == "recording_stopped":
                        stale_tail = event.get("end_tail_duration")
                except json.JSONDecodeError:
                    continue
        try:
            stale_tail_suspected = stale_tail is not None and float(stale_tail) > 30
        except (TypeError, ValueError):
            stale_tail_suspected = False
        if stale_tail_suspected:
            flags.append("STALE_TAIL_SUSPECTED")
        stage_status = {
            "MEDIA": "RUNNING"
            if growing
            else ("DONE" if source_valid else ("FAILED" if media_paths else "NOT_STARTED")),
            "AUDIO": "DONE" if has_audio else ("BLOCKED" if source_valid else "NOT_STARTED"),
            "TRANSCRIPT": transcript_state,
            "DIARIZATION": diarization_state if has_audio else "NOT_APPLICABLE",
            "SPEAKER_MAPPING": "DONE"
            if any(seg.get("speaker") for seg in segments if isinstance(seg, dict))
            else "BLOCKED"
            if segments
            else "NOT_APPLICABLE",
            "SEMANTIC_ANALYSIS": analysis_state,
            "REPORT": "DONE" if (folder / "analysis" / "full_analysis.json").is_file() else "NOT_STARTED",
            "POST_PROCESSING": post_processing_state,
        }
        error_reasons = {
            stage: job.get("failure_reason")
            for stage, job_type in (
                ("TRANSCRIPT", "transcribe"),
                ("DIARIZATION", "diarize"),
                ("SEMANTIC_ANALYSIS", "analyze"),
                ("POST_PROCESSING", "post_process"),
            )
            if (job := stage_jobs.get(job_type)) and job.get("failure_reason")
        }
        records.append(
            {
                "space_id": sid,
                "folder": str(folder),
                "folder_relative": folder.relative_to(data_dir).as_posix(),
                "title": title,
                "expected_host": host,
                "observed_host": observed_host,
                "created_at": folder_start,
                "started_at": db_info.get("started_at") or metadata.get("started_at"),
                "db": db_info,
                "media": {
                    "files": media,
                    "source": source_media,
                    "playback": playback_media,
                    "still_growing": growing,
                    "cleanly_finalized": cleanly_finalized,
                    "duration_seconds": duration,
                    "size_bytes": sum(m["size_bytes"] for m in media),
                },
                "transcript": {
                    "present": bool(segments_count or full_text),
                    "segment_count": segments_count,
                    "word_count": words_count,
                    "language": (transcript_data or {}).get("language"),
                },
                "derived": {
                    "speaker_observations": speaker_observations,
                    "speaker_events": speaker_events,
                    "participants": participants,
                    "diarization": bool(diarization),
                    "x_speaker_timeline": (folder / "speaker_timeline.json").is_file()
                    or (folder / "analysis" / "speaker_timeline.json").is_file(),
                    "semantic_analysis": bool(summary),
                    "transcript_segments_db": transcript_counts.get(sid or "", 0),
                },
                "recording_health": {
                    "video_valid": has_video,
                    "audio_valid": has_audio,
                    "container_finalized": cleanly_finalized,
                    "known_interrupted": known_interrupted,
                    "stale_tail_suspected": stale_tail_suspected,
                    "stale_tail_seconds": stale_tail,
                    "transcription_status": transcript_state,
                    "quality_scope": "container_and_stream_integrity_only",
                    "overall_status": "INTERRUPTED"
                    if known_interrupted
                    else "JOINING"
                    if joining
                    else "ACTIVE"
                    if capture_active
                    else "CORRUPT"
                    if media_paths and not source_valid
                    else "PARTIAL"
                    if completeness.get("status") == "PARTIAL"
                    else "NO_AUDIO"
                    if source_valid and not has_audio
                    else "NO_SPEECH"
                    if "NO_SPEECH" in flags
                    else "GOOD"
                    if source_valid and cleanly_finalized
                    else "INCOMPLETE",
                },
                "stages": stage_status,
                "stage_errors": error_reasons,
                "flags": list(dict.fromkeys(flags)),
                "candidate_score": _candidate_score(
                    source_valid,
                    cleanly_finalized,
                    duration,
                    has_audio,
                    has_video,
                    segments_count,
                    speaker_events,
                    participants,
                    stale_tail_suspected,
                ),
            }
        )
    records.sort(key=lambda row: (row["candidate_score"], row.get("space_id") or ""), reverse=True)
    counts = Counter(flag for row in records for flag in row["flags"])
    best_completed = next(
        (
            row
            for row in records
            if row["recording_health"]["overall_status"] == "GOOD"
            and row["media"]["duration_seconds"] >= 300
            and row["transcript"]["present"]
        ),
        None,
    )
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_root": str(data_dir),
        "space_folder_count": len(records),
        "db_space_count": len(db_rows),
        "db_spaces_without_folder": sorted(
            set(db_rows) - {row["space_id"] for row in records if row["space_id"]}
        ),
        "recommended_validation_space_id": best_completed["space_id"] if best_completed else None,
        "flag_counts": dict(counts),
        "spaces": records,
    }


def _hf_token_configured() -> bool:
    import os

    return bool(os.environ.get("HF_TOKEN"))


def _folder_time(folder: Path) -> str | None:
    # YYYY/MM/DD/HHMMSS - ...
    for part in reversed(folder.parts):
        match = re.match(r"^(\d{6})\s+-", part)
        if match:
            try:
                day = folder.parent.parent.name
                month = folder.parent.name
                year = folder.parent.parent.parent.name
                value = datetime.strptime(f"{year}/{month}/{day}/{match.group(1)}", "%Y/%m/%d/%H%M%S")
                return value.isoformat()
            except ValueError:
                return None
    return None


def _observed_host(folder: Path, participants_file: dict[str, Any] | None) -> str | None:
    candidates = (participants_file or {}).get("participants") or []
    if isinstance(candidates, dict):
        candidates = list(candidates.values())
    host = next(
        (
            item
            for item in candidates
            if isinstance(item, dict) and str(item.get("role", "")).lower() == "host"
        ),
        None,
    )
    if host:
        return host.get("username") or host.get("screen_name")
    path = folder / "speaker_observations.jsonl"
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                data = json.loads(line)
                participants = data.get("participants") or []
                host = next((p for p in participants if str(p.get("role", "")).lower() == "host"), None)
                if host:
                    return host.get("username") or host.get("screen_name")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return None


def _candidate_score(
    source_valid: bool,
    finalized: bool,
    duration: float,
    audio: bool,
    video: bool,
    segments: int,
    events: int,
    participants: int,
    stale_tail: bool,
) -> float:
    score = 0.0
    score += 1000 if finalized else 0
    score += 300 if source_valid else 0
    score += 100 if audio else 0
    score += 80 if video else 0
    score += min(max(duration, 0.0), 10_800) / 60
    score += 500 if segments else 0
    score += min(segments, 1000) * 0.3
    score += min(events, 500) * 0.1
    score += min(participants, 100) * 0.2
    score -= 1000 if stale_tail else 0
    return round(score, 2)


def write_inventory(data_dir: Path, output_path: Path | None = None) -> dict[str, Any]:
    inventory = audit_spaces(data_dir)
    target = output_path or (data_dir / "space_inventory.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)
    return inventory
