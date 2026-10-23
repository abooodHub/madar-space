"""Deterministic on-disk layout for a Space's artifacts.

data/spaces/YYYY/MM/DD/HHMMSS - SPACE_TITLE - @HOST - SPACE_ID/
    recording_001.mkv ...   raw segments (never modified)
    recording.mkv           merged logical recording (when segments are compatible)
    playback.mp4            browser-friendly remux (no re-encode)
    audio.wav               normalized 16 kHz mono analysis audio
    space.json participants.json speaker_events.json transcript.json analysis.json
    avatars/ screenshots/ clips/
"""

from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9_\-]")
_LOCAL_TZ = timezone(timedelta(hours=3), "Asia/Riyadh")


def safe_id(space_id: str) -> str:
    cleaned = _SAFE.sub("", space_id)
    if not cleaned:
        raise ValueError(f"invalid space id: {space_id!r}")
    return cleaned


def _find_space_folder(data_dir: Path, space_id: str) -> Path | None:
    """Resolve an existing Space by stable ID, independent of its folder name/depth."""
    suffix = f" - {space_id}"
    spaces_root = data_dir / "spaces"
    # The normal date hierarchy is cheap to check first. Fall back to recursive
    # ID matching for older or reorganized layouts, then to the identity in a
    # sidecar so descriptive directory names are never authoritative.
    candidates: list[Path] = []
    if spaces_root.exists():
        candidates.extend(path for path in spaces_root.glob("*/*/*") if path.is_dir())
        candidates.extend(path for path in spaces_root.glob("*/*/*/*") if path.is_dir())
    matches = [path for path in candidates if path.name == space_id or path.name.endswith(suffix)]
    if not matches:
        search_roots = [spaces_root]
        if data_dir.exists():
            excluded = {"logs", "cache", "_maintenance", "tmp", "temp"}
            search_roots.extend(
                child
                for child in data_dir.iterdir()
                if child.is_dir() and child.name.casefold() not in excluded and child != spaces_root
            )
        for search_root in search_roots:
            if not search_root.exists():
                continue
            asset_dirs = {"audio", "avatars", "clips", "screenshots", "transcript", "analysis"}
            for current, directories, _files in os.walk(search_root):
                path = Path(current)
                relative_depth = len(path.relative_to(search_root).parts)
                is_match = path.name == space_id or path.name.endswith(suffix)
                if is_match:
                    matches.append(path)
                    directories[:] = []
                    continue
                sidecar = path / "space.json"
                if sidecar.is_file():
                    try:
                        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        metadata = None
                    if _metadata_space_id(metadata) == space_id:
                        matches.append(path)
                        directories[:] = []
                        continue
                if relative_depth >= 7:
                    directories[:] = []
                else:
                    directories[:] = [name for name in directories if name.casefold() not in asset_dirs]
            if matches:
                break
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def _find_space_folders(data_dir: Path, space_ids: set[str]) -> dict[str, Path]:
    """Resolve many Space IDs with at most one recursive filesystem walk."""
    if not space_ids:
        return {}

    found: dict[str, list[Path]] = {space_id: [] for space_id in space_ids}
    unresolved = set(space_ids)
    spaces_root = data_dir / "spaces"

    def record(path: Path, *, unresolved_only: bool = False) -> bool:
        name = path.name
        candidates = unresolved if unresolved_only else space_ids
        matched = [space_id for space_id in candidates if name == space_id or name.endswith(f" - {space_id}")]
        if not matched:
            return False
        for space_id in matched:
            found[space_id].append(path)
            unresolved.discard(space_id)
        return True

    # The canonical date hierarchy is cheap to index once and covers nearly all
    # current recordings. Keep multiple paths so duplicate IDs resolve by mtime,
    # matching the single-ID resolver's behavior.
    if spaces_root.exists():
        candidates = [*spaces_root.glob("*/*/*"), *spaces_root.glob("*/*/*/*")]
        for path in candidates:
            if path.is_dir():
                record(path)

    if unresolved:
        excluded = {"logs", "cache", "_maintenance", "tmp", "temp"}
        asset_dirs = {"audio", "avatars", "clips", "screenshots", "transcript", "analysis"}
        if data_dir.exists():
            for current, directories, _files in os.walk(data_dir):
                path = Path(current)
                relative_depth = len(path.relative_to(data_dir).parts)
                if relative_depth == 0:
                    directories[:] = [name for name in directories if name.casefold() not in excluded]
                if record(path, unresolved_only=True):
                    directories[:] = []
                    continue
                sidecar = path / "space.json"
                if sidecar.is_file():
                    try:
                        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        metadata = None
                    sidecar_id = _metadata_space_id(metadata)
                    if sidecar_id in unresolved:
                        found[sidecar_id].append(path)
                        unresolved.discard(sidecar_id)
                        directories[:] = []
                        continue
                if relative_depth >= 8 or not unresolved:
                    directories[:] = []
                    if not unresolved:
                        break
                else:
                    directories[:] = [name for name in directories if name.casefold() not in asset_dirs]

    return {
        space_id: max(paths, key=lambda path: path.stat().st_mtime)
        for space_id, paths in found.items()
        if paths
    }


def _metadata_space_id(metadata: object) -> str | None:
    """Read common Space ID fields from sidecar metadata without trusting names."""
    if not isinstance(metadata, dict):
        return None
    for key in ("space_id", "spaceId", "audio_space_id", "id"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("space", "metadata", "audio_space"):
        nested = _metadata_space_id(metadata.get(key))
        if nested:
            return nested
    return None


def sanitize_title(title: str | None, max_length: int = 70) -> str:
    """Sanitize title for safe cross-platform folder naming (Windows + Linux)."""
    if not title:
        return ""
    # Strip characters invalid on Windows / Linux: \ / : * ? " < > |
    cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]', "", str(title))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(". ")
    # Keep the folder's " - " separators unambiguous for parsing and search.
    cleaned = cleaned.replace(" - ", " — ")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip()
    return cleaned


def _folder_name(start: datetime, space_id: str, title: str | None, host: str | None) -> str:
    parts = [f"{start:%H%M%S}"]
    for value in (title, host):
        cleaned = sanitize_title(value, max_length=48)
        if cleaned:
            parts.append(cleaned)
    parts.append(space_id)
    return " - ".join(parts)


@dataclass(frozen=True, init=False)
class SpacePaths:
    root: Path

    def __init__(
        self, root: Path, space_id: str | None = None, title: str | None = None, host: str | None = None
    ):
        if space_id is not None:
            # Called as SpacePaths(data_dir, space_id, title=...)
            clean_sid = safe_id(space_id)
            match = _find_space_folder(root, clean_sid)
            if match:
                object.__setattr__(self, "root", match)
            else:
                now = datetime.now(UTC).astimezone(_LOCAL_TZ)
                folder_name = _folder_name(now, clean_sid, title, host)
                object.__setattr__(
                    self, "root", root / "spaces" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}" / folder_name
                )
        else:
            object.__setattr__(self, "root", root)

    @classmethod
    def for_space(
        cls,
        data_dir: Path,
        space_id: str,
        started_at: datetime,
        title: str | None = None,
        host: str | None = None,
    ) -> SpacePaths:
        clean_sid = safe_id(space_id)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        local_start = started_at.astimezone(_LOCAL_TZ)
        # Reuse legacy folders, but upgrade their name and hierarchy once
        # verified title/host metadata is available. Files are moved intact.
        match = _find_space_folder(data_dir, clean_sid)
        if match:
            wanted_title = sanitize_title(title, max_length=48)
            wanted_host = sanitize_title(host, max_length=48)
            if wanted_title or wanted_host:
                suffix = f" - {clean_sid}"
                middle = match.name[: -len(suffix)] if match.name.endswith(suffix) else ""
                time_match = re.match(r"^(\d{6})(?: - (.*))?$", middle)
                old_time = time_match.group(1) if time_match else ""
                old_parts = (
                    (time_match.group(2) or "").split(" - ") if time_match else ([middle] if middle else [])
                )
                old_parts = [part for part in old_parts if part]
                old_title = old_parts[0] if old_parts else ""
                old_host = old_parts[1] if len(old_parts) > 1 else ""
                final_title = wanted_title or old_title
                final_host = wanted_host or old_host
                clock = old_time or f"{local_start:%H%M%S}"
                desired_name = " - ".join(
                    part for part in (clock, final_title, final_host, clean_sid) if part
                )
                desired = (
                    data_dir
                    / "spaces"
                    / f"{local_start:%Y}"
                    / f"{local_start:%m}"
                    / f"{local_start:%d}"
                    / desired_name
                )
                if match != desired and not desired.exists():
                    try:
                        desired.parent.mkdir(parents=True, exist_ok=True)
                        match.rename(desired)
                        match = desired
                    except OSError:
                        # Keep the legacy path if another process has a file open.
                        pass
            return cls(match)
        return cls.planned_path(data_dir, space_id, started_at, title=title, host=host)

    @classmethod
    def planned_path(
        cls,
        data_dir: Path,
        space_id: str,
        started_at: datetime,
        title: str | None = None,
        host: str | None = None,
    ) -> SpacePaths:
        """Return the canonical path for a new/missing Space without searching or writing."""
        clean_sid = safe_id(space_id)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        local_start = started_at.astimezone(_LOCAL_TZ)
        folder_name = _folder_name(local_start, clean_sid, title, host)
        return cls(
            data_dir
            / "spaces"
            / f"{local_start:%Y}"
            / f"{local_start:%m}"
            / f"{local_start:%d}"
            / folder_name
        )

    @classmethod
    def find_space(cls, data_dir: Path, space_id: str, title: str | None = None) -> SpacePaths:
        return cls(data_dir, space_id, title=title)

    @classmethod
    def find_spaces(cls, data_dir: Path, space_ids: list[str]) -> dict[str, SpacePaths]:
        """Resolve a batch of IDs without repeating recursive storage scans."""
        cleaned_ids = {safe_id(space_id) for space_id in space_ids}
        return {space_id: cls(path) for space_id, path in _find_space_folders(data_dir, cleaned_ids).items()}

    @classmethod
    def resolve_existing(cls, data_dir: Path, space_id: str) -> SpacePaths | None:
        """Return an existing on-disk Space by ID; never creates or renames files."""
        match = _find_space_folder(data_dir, safe_id(space_id))
        return cls(match) if match else None

    def update_title(self, title: str) -> SpacePaths:
        """Rename the folder to include the title once it becomes known."""
        clean_title = sanitize_title(title)
        if not clean_title or clean_title in self.root.name:
            return self
        current_prefix, separator, current_sid = self.root.name.rpartition(" - ")
        clean_sid = safe_id(current_sid if separator else self.root.name)
        time_prefix = re.match(r"^(\d{6}) - ", self.root.name)
        host = ""
        if time_prefix:
            middle = (
                self.root.name[9 : self.root.name.rfind(" - ")] if self.root.name.rfind(" - ") > 9 else ""
            )
            if " - " in middle:
                host = middle.split(" - ", 1)[1]
        new_name = " - ".join(
            p for p in (time_prefix.group(1) if time_prefix else "", clean_title, host, clean_sid) if p
        )
        new_path = self.root.parent / new_name
        try:
            if new_path == self.root or new_path.exists():
                return self
            if self.root.exists():
                self.root.rename(new_path)
            # Planned (not-yet-created) folders should still pick up the
            # verified Space title before FFmpeg creates the directory.
            return SpacePaths(new_path)
        except Exception:
            pass
        return self

    def update_host(self, host: str) -> SpacePaths:
        """Add or update the host display name in this Space's folder name."""
        clean_host = sanitize_title(host, max_length=48)
        if not clean_host or clean_host in self.root.name:
            return self
        match = re.match(r"^(\d{6}) - (.*) - ([A-Za-z0-9_-]+)$", self.root.name)
        if not match:
            return self
        prefix, middle, sid = match.groups()
        title = middle.split(" - ", 1)[0] if middle else ""
        new_name = " - ".join(p for p in (prefix, title, clean_host, sid) if p)
        new_path = self.root.parent / new_name
        try:
            if self.root.exists() and not new_path.exists():
                self.root.rename(new_path)
                return SpacePaths(new_path)
        except OSError:
            pass
        return self

    def ensure(self) -> SpacePaths:
        """Create the Space root; artifact subfolders are created by their writers.

        Pre-creating every future artifact directory made successful recordings
        look incomplete and left empty folders after failed joins.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    def segment(self, index: int) -> Path:
        return self.root / f"recording_{index:03d}.mkv"

    @property
    def recording(self) -> Path:
        return self.root / "recording.mkv"

    @property
    def playback(self) -> Path:
        return self.root / "playback.mp4"

    @property
    def thumbnail(self) -> Path:
        return self.root / "thumbnail.jpg"

    @property
    def audio_original(self) -> Path:
        return self.audio_dir / "original.wav"

    @property
    def audio_analysis(self) -> Path:
        return self.audio_dir / "analysis.wav"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def transcript_dir(self) -> Path:
        return self.root / "transcript"

    def _transcript_artifact(self, name: str, legacy: Path) -> Path:
        from xsa.db import Space, get_session
        from xsa.db.database import _SessionLocal

        if _SessionLocal is not None:
            space_id = self.root.name.rsplit(" - ", 1)[-1]
            metadata = self.root / "space.json"
            if metadata.is_file():
                with suppress(OSError, ValueError):
                    space_id = json.loads(metadata.read_text(encoding="utf-8")).get("space_id", space_id)
            with get_session() as db:
                row = db.query(Space).filter_by(space_id=space_id).first()
                generation = getattr(row, "transcript_generation_id", None) if row else None
                if generation and re.fullmatch(r"[a-f0-9]{32}", generation):
                    return self.transcript_dir / "generations" / generation / name
        return legacy

    @property
    def analysis_dir(self) -> Path:
        return self.root / "analysis"

    @property
    def transcript_raw(self) -> Path:
        return self._transcript_artifact("raw.json", self.transcript_dir / "raw.json")

    @property
    def transcript_normalized(self) -> Path:
        return self._transcript_artifact("normalized.json", self.transcript_dir / "normalized.json")

    @property
    def transcript_words(self) -> Path:
        return self._transcript_artifact("words.json", self.transcript_dir / "words.json")

    @property
    def transcript_segments(self) -> Path:
        return self._transcript_artifact("segments.json", self.transcript_dir / "segments.json")

    @property
    def transcript_text(self) -> Path:
        return self._transcript_artifact("transcript.txt", self.transcript_dir / "transcript.txt")

    @property
    def space_json(self) -> Path:
        return self.root / "space.json"

    @property
    def participants_json(self) -> Path:
        return self.root / "participants.json"

    @property
    def speaker_events_json(self) -> Path:
        return self.root / "speaker_events.json"

    @property
    def speaker_events_live(self) -> Path:
        """Append-only JSONL written during the live session (crash-safe)."""
        return self.root / "speaker_events.jsonl"

    @property
    def space_end_events(self) -> Path:
        """Append-only evidence for Space termination and recorder shutdown."""
        return self.root / "space_end_events.jsonl"

    @property
    def speaker_timeline_json(self) -> Path:
        """Completed, portable report of every detected speaker turn."""
        return self.root / "speaker_timeline.json"

    @property
    def transcript_json(self) -> Path:
        return self._transcript_artifact("transcript.json", self.root / "transcript.json")

    @property
    def transcript_srt(self) -> Path:
        return self._transcript_artifact("transcript.srt", self.root / "transcript.srt")

    @property
    def transcript_vtt(self) -> Path:
        return self._transcript_artifact("transcript.vtt", self.root / "transcript.vtt")

    @property
    def analysis_json(self) -> Path:
        return self.root / "analysis.json"

    @property
    def summary_json(self) -> Path:
        return self.root / "summary.json"

    @property
    def avatars(self) -> Path:
        return self.root / "avatars"

    @property
    def screenshots(self) -> Path:
        return self.root / "screenshots"

    @property
    def clips(self) -> Path:
        return self.root / "clips"
