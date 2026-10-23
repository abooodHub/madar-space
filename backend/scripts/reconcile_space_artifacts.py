"""Report or reconstruct missing derived transcript files from persisted DB rows.

This command never runs Whisper, extracts audio, or changes source media. Use
`--dry-run` to inspect the proposed writes before `--apply`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xsa.config import get_settings  # noqa: E402
from xsa.db import Space, TranscriptSegment, TranscriptWord, get_session, init_db  # noqa: E402
from xsa.db.models import MonitoredAccount, RecordingSegment  # noqa: E402
from xsa.paths import SpacePaths  # noqa: E402


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(payload, encoding="utf-8")
    os.replace(temp, path)


def reconcile(space_id: str, *, dry_run: bool, data_dir: Path | None = None) -> dict[str, Any]:
    cfg = get_settings()
    data_dir = data_dir or cfg.data_dir
    init_db(cfg.db_path)
    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if space is None:
            raise LookupError(f"Space {space_id} does not exist in the database")
        existing = SpacePaths.resolve_existing(data_dir, space_id)
        if existing:
            paths = existing
        else:
            started_at = space.recording_started_at or space.started_at or space.detected_at
            host = db.get(MonitoredAccount, space.host_account_id) if space.host_account_id else None
            paths = SpacePaths.planned_path(
                data_dir,
                space_id,
                started_at,
                title=space.title,
                host=host.username if host else None,
            )

        segment_paths = [
            Path(row.file_path)
            for row in db.query(RecordingSegment).filter_by(space_id=space.id).all()
            if row.file_path
        ]
        segments = (
            db.query(TranscriptSegment)
            .filter_by(space_id=space.id)
            .order_by(TranscriptSegment.segment_index)
            .all()
        )
        words = (
            db.query(TranscriptWord)
            .filter_by(space_id=space.id)
            .order_by(TranscriptWord.start_ms, TranscriptWord.id)
            .all()
        )
        words_by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for word in words:
            words_by_segment[word.segment_id].append(
                {
                    "word": word.word,
                    "start": round(word.start_ms / 1000, 3),
                    "end": round(word.end_ms / 1000, 3),
                    "start_ms": word.start_ms,
                    "end_ms": word.end_ms,
                    "probability": round(word.probability, 3) if word.probability is not None else None,
                }
            )
        exported_segments = [
            {
                "id": segment.segment_index,
                "start": round(segment.start_ms / 1000, 3),
                "end": round(segment.end_ms / 1000, 3),
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "speaker": segment.speaker_label,
                "display_name": "",
                "text": segment.text,
                "language": segment.language,
                "confidence": segment.confidence,
                "no_speech_prob": segment.no_speech_prob,
                "words": words_by_segment.get(segment.id, []),
            }
            for segment in segments
        ]
        languages = Counter(segment.language for segment in segments if segment.language)
        language = languages.most_common(1)[0][0] if languages else None
        full_text = " ".join(
            segment.text.strip() for segment in segments if segment.text and segment.text.strip()
        )
        transcript_json = {
            "language": language,
            "duration_seconds": None,
            "full_text": full_text,
            "segments": exported_segments,
            "reconstructed_from": "verified_database_rows",
        }
        all_words = [word for segment in exported_segments for word in segment["words"]]
        outputs = {
            paths.transcript_segments: json.dumps(exported_segments, ensure_ascii=False, indent=2),
            paths.transcript_words: json.dumps(all_words, ensure_ascii=False, indent=2),
            paths.transcript_normalized: json.dumps(transcript_json, ensure_ascii=False, indent=2),
            paths.transcript_text: full_text + "\n",
            paths.transcript_json: json.dumps(transcript_json, ensure_ascii=False, indent=2),
        }
        missing_outputs = [str(path) for path in outputs if not path.exists()]
        mkv_paths = {path for path in segment_paths if path.is_file() and path.suffix.casefold() == ".mkv"}
        if paths.root.exists():
            mkv_paths.update(paths.root.glob("recording*.mkv"))
        source_mkvs = sorted({str(path) for path in mkv_paths if path.is_file()})
        report = {
            "space_id": space_id,
            "resolved_path": str(paths.root),
            "path_was_existing": existing is not None,
            "why_expected_path_was_missing": (
                "no matching directory or space.json sidecar exists under the mounted data root; "
                "the historical date directory is absent"
                if existing is None
                else None
            ),
            "database_recording_segment_paths": [str(path) for path in segment_paths],
            "source_mkv_found": bool(source_mkvs),
            "source_mkvs": source_mkvs,
            "analysis_audio_found": paths.audio_analysis.is_file(),
            "transcript_db_segments": len(segments),
            "transcript_db_words": len(words),
            "transcript_artifacts_missing": missing_outputs,
            "whisper_rerun_required": False,
            "mode": "dry-run" if dry_run else "apply",
        }
        if not dry_run:
            for path, payload in outputs.items():
                if not path.exists():
                    _write_atomic(path, payload)
            report["artifacts_written"] = [path for path in missing_outputs]
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("space_id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()
    try:
        report = reconcile(args.space_id, dry_run=args.dry_run, data_dir=args.data_dir)
    except Exception as exc:
        print(f"ARTIFACT_RECONCILIATION_FAILED: {type(exc).__name__}: {exc}")
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
