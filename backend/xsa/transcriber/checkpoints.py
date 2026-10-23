"""Durable, source-bound Whisper window results.

Only a complete, fsynced window is reusable. A different source or recognition
configuration gets a different directory, so retries cannot mix generations.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from xsa.transcriber.engine import SegmentItem, TranscriptionResult, WordItem

SCHEMA_VERSION = 1
WINDOW_SECONDS = 600


def source_identity(source: Path, *, model: str, language: str | None, compute_type: str) -> dict:
    stat = source.stat()
    fingerprint = hashlib.sha256()
    with source.open("rb") as handle:
        fingerprint.update(handle.read(65536))
        if stat.st_size > 65536:
            handle.seek(max(0, stat.st_size - 65536))
            fingerprint.update(handle.read(65536))
    return {
        "schema": SCHEMA_VERSION,
        "source_name": source.name,
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_sample_sha256": fingerprint.hexdigest(),
        "model": model,
        "language": language,
        "compute_type": compute_type,
        "window_seconds": WINDOW_SECONDS,
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        if os.name == "posix":
            try:
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                # Some Docker Desktop bind mounts do not support directory fsync.
                pass
    finally:
        temporary.unlink(missing_ok=True)


class WindowCheckpoints:
    def __init__(self, root: Path, identity: dict, total_windows: int) -> None:
        self.root = root
        self.identity = identity
        self.key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.folder = root / self.key
        self.total_windows = total_windows

    def _path(self, index: int) -> Path:
        return self.folder / f"window_{index:06d}.json"

    def load(self, index: int, offset: float, length: float) -> TranscriptionResult | None:
        try:
            value = json.loads(self._path(index).read_text(encoding="utf-8"))
            if (
                value.get("identity") != self.identity
                or value.get("index") != index
                or value.get("offset") != offset
                or value.get("length") != length
            ):
                return None
            segments = []
            for item in value["segments"]:
                words = [WordItem(**word) for word in item.get("words", [])]
                segment = SegmentItem(**dict(item, words=words))
                if not 0 <= segment.start <= segment.end <= length + 0.1:
                    return None
                segments.append(segment)
            return TranscriptionResult(
                language=value["language"],
                duration=length,
                segments=segments,
                text=" ".join(item.text for item in segments),
            )
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, index: int, offset: float, length: float, result: TranscriptionResult) -> None:
        if any(not 0 <= item.start <= item.end <= length + 0.1 for item in result.segments):
            raise ValueError("Whisper returned timings outside the current audio window")
        _atomic_json(
            self._path(index),
            {
                "identity": self.identity,
                "index": index,
                "offset": offset,
                "length": length,
                "language": result.language,
                "segments": [asdict(item) for item in result.segments],
            },
        )
        _atomic_json(
            self.root / "current.json",
            {"key": self.key, "identity": self.identity, "total_windows": self.total_windows},
        )

    def completed_count(self) -> int:
        count = 0
        for index in range(self.total_windows):
            try:
                value = json.loads(self._path(index).read_text(encoding="utf-8"))
                offset, length = value["offset"], value["length"]
                if (
                    offset == index * WINDOW_SECONDS
                    and isinstance(length, (int, float))
                    and 0 < length <= WINDOW_SECONDS
                    and self.load(index, offset, length) is not None
                ):
                    count += 1
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return count


def read_partial_transcript(space_root: Path) -> dict:
    """Return verified, timestamped partial text without marking it final."""
    root = space_root / "transcript" / "checkpoints"
    empty = {"recognition_state": "NOT_STARTED", "segments": [], "completed_windows": 0, "total_windows": 0}
    try:
        pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
        identity = pointer["identity"]
        name = identity["source_name"]
        total = pointer["total_windows"]
        if Path(name).name != name or not isinstance(total, int) or not 0 < total < 100000:
            return empty
        source = space_root / name
        current = source_identity(
            source,
            model=identity["model"],
            language=identity["language"],
            compute_type=identity["compute_type"],
        )
        if current != identity:
            return empty
        store = WindowCheckpoints(root, identity, total)
        if pointer["key"] != store.key:
            return empty
        segments = []
        completed = 0
        for index in range(total):
            try:
                metadata = json.loads(store._path(index).read_text(encoding="utf-8"))
                offset, length = metadata["offset"], metadata["length"]
                if offset != index * WINDOW_SECONDS or not 0 < length <= WINDOW_SECONDS:
                    continue
                result = store.load(index, offset, length)
                if result is None:
                    continue
                completed += 1
                for item in result.segments:
                    segment = asdict(item)
                    segment["id"] = len(segments)
                    segment["start"] += offset
                    segment["end"] += offset
                    for word in segment["words"]:
                        word["start"] += offset
                        word["end"] += offset
                    segments.append(segment)
            except (OSError, ValueError, TypeError, KeyError):
                continue
        if completed == 0:
            return empty
        return {
            "recognition_state": "PARTIAL",
            "segments": segments,
            "completed_windows": completed,
            "total_windows": total,
            "progress": completed / total,
            "source_name": name,
        }
    except (OSError, ValueError, TypeError, KeyError):
        return empty
