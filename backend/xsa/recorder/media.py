"""Media finalization helpers for recordings with muxed audio and video."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

from xsa.artifact_lock import artifact_lock


def _capture_active(root: Path) -> bool:
    import psutil

    for proc in psutil.process_iter(attrs=["name"]):
        try:
            if "ffmpeg" not in (proc.info.get("name") or "").lower():
                continue
            args = (proc.info["cmdline"] if "cmdline" in proc.info else proc.cmdline()) or []
            if (
                "ffmpeg" in (proc.info.get("name") or "").lower()
                and "x11grab" in args
                and "pulse" in args
                and args
                and Path(args[-1]).resolve().parent == root.resolve()
            ):
                return True
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError):
            # A known FFmpeg whose arguments cannot be inspected could own this
            # capture. Refuse publication rather than assuming it is idle.
            return True
    return False


def _source_generation(source: Path) -> dict:
    stat = source.stat()
    return {"source": source.name, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def playback_readiness_from_metadata(
    sizes: dict[str, int], mtimes: dict[str, int], manifest: dict | None
) -> tuple[bool, str]:
    """Check whether a published MP4 matches its source without FFprobe or FFmpeg."""
    if sizes.get("playback.mp4", 0) <= 0:
        return False, "missing"
    segments = [name for name in sizes if re.fullmatch(r"recording_[0-9]{3}\.mkv", name)]
    raw = [name for name in ("recording.mkv", *segments) if sizes.get(name, 0) > 0]
    if not raw:
        # Keep a surviving MP4 playable when its raw source was removed.
        return True, "source_unavailable"

    source_name = manifest.get("source") if isinstance(manifest, dict) else None
    if source_name:
        if source_name not in raw:
            return False, "source_changed"
        if sizes[source_name] != manifest.get("size_bytes") or mtimes.get(source_name) != manifest.get(
            "mtime_ns"
        ):
            return False, "source_changed"
        if source_name != "recording.mkv" and len(segments) > 1:
            return False, "new_segments"
        if any(mtimes.get(name, 0) > mtimes.get(source_name, 0) for name in raw if name != source_name):
            return False, "new_segments"
        return True, "ready"

    # Older MP4s have no source manifest. Their timestamps provide a cautious fallback.
    if len(segments) > 1 and "recording.mkv" not in raw:
        return False, "unmerged_segments"
    if "recording.mkv" in raw and any(
        mtimes.get(name, 0) > mtimes.get("recording.mkv", 0) for name in segments
    ):
        return False, "new_segments"
    if mtimes.get("playback.mp4", 0) < max(mtimes.get(name, 0) for name in raw):
        return False, "source_changed"
    return True, "ready_legacy"


def read_playback_source_manifest(root: Path) -> dict | None:
    try:
        value = json.loads((root / "playback.mp4.source.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError):
        return None


def playback_readiness_for_paths(paths) -> tuple[bool, str]:
    """Cheap direct-folder inspection for an individual media request."""
    sizes: dict[str, int] = {}
    mtimes: dict[str, int] = {}
    try:
        with os.scandir(paths.root) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False) and (
                    entry.name in {"playback.mp4", "recording.mkv"}
                    or re.fullmatch(r"recording_[0-9]{3}\.mkv", entry.name)
                ):
                    stat = entry.stat(follow_symlinks=False)
                    sizes[entry.name] = stat.st_size
                    mtimes[entry.name] = stat.st_mtime_ns
    except OSError:
        return False, "missing"
    return playback_readiness_from_metadata(sizes, mtimes, read_playback_source_manifest(paths.root))


def recording_completeness(paths, segments) -> dict:
    files = list(segments)
    excluded = [path.name for path in files if not has_audio_video(path)]
    numbers = [int(p.stem.rsplit("_", 1)[-1]) for p in files]
    missing = [
        f"recording_{index:03d}.mkv" for index in range(max(numbers, default=-1) + 1) if index not in numbers
    ]
    excluded.extend(missing)
    value = {
        "status": "PARTIAL" if excluded else "COMPLETE",
        "status_scope": "container_and_stream_integrity_only",
        "quality": {"state": "NOT_ASSESSED"},
        "segments": [p.name for p in files],
        "excluded_segments": excluded,
        "gaps": [
            {"segment": name, "duration": "unknown", "reason": "unreadable_or_missing_stream"}
            for name in excluded
        ],
    }
    target = paths.root / "recording_manifest.json"
    partial = target.with_name(f".{uuid4().hex}.manifest.tmp")
    partial.write_text(json.dumps(value, indent=2), encoding="utf-8")
    partial.replace(target)
    return value


def has_audio_video(path: Path) -> bool:
    """Return whether a media file is readable and contains both streams."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        streams = set(result.stdout.splitlines())
        return "video" in streams and "audio" in streams
    except (OSError, subprocess.SubprocessError):
        return False


def _concat_quote(path: Path) -> str:
    # Syntax required by FFmpeg's concat demuxer for a single quoted path.
    return "'" + str(path.resolve()).replace("'", "'\\''") + "'"


def finalize_recording(paths, segments: Iterable[Path]) -> Path | None:
    with artifact_lock(paths.recording):
        return _finalize_recording(paths, segments)


def _finalize_recording(paths, segments: Iterable[Path]) -> Path | None:
    """Join all completed segments into recording.mkv, preserving both streams."""
    segments = list(segments)
    recording_completeness(paths, segments)
    files = [path for path in segments if has_audio_video(path)]
    if not files:
        return None
    if len(files) == 1:
        return files[0] if has_audio_video(files[0]) else None

    target = paths.recording
    attempt = uuid4().hex
    partial = target.with_name(f"recording.{attempt}.partial.mkv")
    listing = target.with_name(f"recording.{attempt}.concat.txt")
    try:
        listing.write_text(
            "\n".join(f"file {_concat_quote(path)}" for path in files) + "\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                str(partial),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0 or not has_audio_video(partial):
            partial.unlink(missing_ok=True)
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(listing),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "23",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    str(partial),
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0 or not has_audio_video(partial):
                return None
        partial.replace(target)
        return target
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        listing.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


def get_recording_source(paths) -> Path | None:
    with artifact_lock(paths.recording):
        return _get_recording_source(paths)


def _get_recording_source(paths) -> Path | None:
    """Resolve the complete recording, merging older split sessions if needed."""
    segments = sorted(paths.root.glob("recording_[0-9][0-9][0-9].mkv"))
    if _capture_active(paths.root):
        raise RuntimeError("Active recording source cannot be finalized or remuxed")
    if segments:
        recording_completeness(paths, segments)
    valid_segments = [segment for segment in segments if has_audio_video(segment)]
    if has_audio_video(paths.recording):
        # A previous merge can remain after a later attempt failed. Never prefer
        # that stale merge over newer raw segments.
        if (
            not valid_segments
            or max(p.stat().st_mtime for p in valid_segments) <= paths.recording.stat().st_mtime
        ):
            return paths.recording
        return _finalize_recording(paths, segments)
    if len(valid_segments) == 1:
        return valid_segments[0]
    if len(valid_segments) > 1:
        return _finalize_recording(paths, segments)
    return None


def create_playback_mp4(source: Path, target: Path) -> bool:
    """Create an atomically published MP4 and verify it retains audio and video."""
    with artifact_lock(target):
        if _capture_active(source.parent):
            raise RuntimeError("Cannot remux an active capture")
        generation = _source_generation(source)
        manifest = target.with_name(target.name + ".source.json")
        if target.is_file() and manifest.is_file():
            try:
                if json.loads(manifest.read_text(encoding="utf-8")) == generation and has_audio_video(target):
                    return True
            except (OSError, ValueError):
                pass
        success = _create_playback_mp4(source, target)
        if success:
            partial = manifest.with_name(f".{uuid4().hex}.source.tmp")
            partial.write_text(json.dumps(generation), encoding="utf-8")
            partial.replace(manifest)
        return success


def _create_playback_mp4(source: Path, target: Path) -> bool:
    partial = target.with_name(f"playback.{uuid4().hex}.partial.mp4")
    try:
        partial.unlink(missing_ok=True)
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(partial),
            ],
            capture_output=True,
            text=True,
            # Faststart relocates the MP4 index after muxing and can take
            # several minutes for multi-hour recordings on the data drive.
            timeout=1800,
        )
        if result.returncode != 0 or not has_audio_video(partial):
            return False
        partial.replace(target)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        partial.unlink(missing_ok=True)


def create_thumbnail(source: Path, target: Path, *, seek_seconds: int = 10) -> bool:
    """Create and atomically publish a representative frame from a recording."""
    partial = target.with_name(f"{target.stem}.partial{target.suffix}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.unlink(missing_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(max(0, seek_seconds)),
                "-i",
                str(source),
                "-vframes",
                "1",
                "-vf",
                "scale=640:-1",
                str(partial),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=True,
        )
        if not partial.is_file() or partial.stat().st_size == 0:
            return False
        partial.replace(target)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        partial.unlink(missing_ok=True)
