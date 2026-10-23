"""Immutable transcript generations selected by the Space database pointer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4


def stage_generation(root: Path, artifacts: dict[str, str]) -> tuple[str, Path]:
    generation = uuid4().hex
    parent = root / "transcript" / "generations"
    temporary = parent / f".{generation}.tmp"
    temporary.mkdir(parents=True)
    for name in ("raw.json", "normalized.json", "transcript.json"):
        value = json.loads(artifacts[name])
        value["generation_id"] = generation
        has_speech = bool(value.get("segments") or str(value.get("full_text") or "").strip())
        value["recognition_state"] = "DONE" if has_speech else "NO_SPEECH"
        if not has_speech:
            # Whisper can guess a language from silence/noise even when it found
            # no words. Do not present that guess as a recognized language.
            value["language"] = "unknown"
        artifacts[name] = json.dumps(value, ensure_ascii=False, indent=2)
    for name, contents in artifacts.items():
        if name.endswith(".json"):
            json.loads(contents)
        with (temporary / name).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    (temporary / "manifest.json").write_text(
        json.dumps({"generation_id": generation, "files": sorted(artifacts)}), encoding="utf-8"
    )
    complete = parent / generation
    temporary.replace(complete)
    return generation, complete


def publish_compatibility(root: Path, complete: Path) -> None:
    """Legacy copies are conveniences; DB-selected generation remains authoritative."""
    for name in (
        "raw.json",
        "normalized.json",
        "words.json",
        "segments.json",
        "transcript.txt",
        "transcript.json",
        "transcript.srt",
        "transcript.vtt",
    ):
        destination = (
            root / name
            if name in {"transcript.json", "transcript.srt", "transcript.vtt"}
            else root / "transcript" / name
        )
        partial = destination.with_name(f".{uuid4().hex}.tmp")
        partial.write_bytes((complete / name).read_bytes())
        partial.replace(destination)
