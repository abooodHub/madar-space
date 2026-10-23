"""Analytics derived from verified acoustic results, with explicit uncertainty."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xsa.diarization import file_sha256


def load_verified_timeline(
    analysis_dir: Path, audio: Path, transcript: Path, space_id: str
) -> tuple[dict[str, Any] | None, str]:
    timeline_path = analysis_dir / "speaker_timeline.json"
    if not timeline_path.is_file():
        return None, "INCOMPLETE" if (analysis_dir / "diarization_raw.json").exists() else "NOT_STARTED"
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        manifest = json.loads(
            (analysis_dir / "speaker_diarization_manifest.json").read_text(encoding="utf-8")
        )
        source = timeline["source"]
        if timeline.get("space_id") != space_id or timeline.get("status") != "completed":
            return None, "UNVERIFIED"
        for key in ("diarization_fingerprint", "timeline_fingerprint", "audio_sha256", "transcript_sha256"):
            if not source.get(key) or source[key] != manifest.get(key):
                return None, "UNVERIFIED"
        if (
            file_sha256(audio) != source["audio_sha256"]
            or file_sha256(transcript) != source["transcript_sha256"]
        ):
            return None, "STALE_INPUTS"
        return timeline, "DONE"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, "UNVERIFIED"


def attribute_segments(segments: list[dict], timeline: dict) -> list[dict]:
    associations = {row["transcript_segment_id"]: row for row in timeline.get("transcript_associations", [])}
    result = []
    for index, segment in enumerate(segments):
        row = associations.get(segment.get("id", segment.get("segment_index", index)), {})
        # A report may have loaded a different historical transcript artifact.
        # IDs alone cannot establish that its text belongs to this generation.
        matches = str(row.get("transcript_text", "")).strip() == str(segment.get("text", "")).strip()
        try:
            start = float(segment.get("start", segment.get("start_ms", 0) / 1000))
            end = float(segment.get("end", segment.get("end_ms", 0) / 1000))
            matches = matches and abs(start - row["start"]) < 0.01 and abs(end - row["end"]) < 0.01
        except (KeyError, TypeError, ValueError):
            matches = False
        speaker = row.get("speaker_id") if matches and not row.get("ambiguous") else None
        result.append(
            {
                **segment,
                "speaker": speaker,
                "speaker_label": speaker,
                "speaker_attribution": {
                    "source": "acoustic_temporal_alignment",
                    "verified_inputs": matches,
                    "quality": row.get("quality"),
                    "speaker_id": speaker,
                },
            }
        )
    return result


def speaker_insights(timeline: dict, segments: list[dict]) -> list[dict]:
    result = []
    for speaker in sorted(
        timeline.get("speakers", []), key=lambda row: -row.get("total_speaking_seconds", 0)
    ):
        speaker_id = speaker["speaker_id"]
        attributed = [row for row in segments if row.get("speaker") == speaker_id]
        result.append(
            {
                **speaker,
                "turns": [
                    {
                        key: row.get(key)
                        for key in ("start", "end", "speech_seconds", "confidence", "evidence_source")
                    }
                    for row in timeline.get("intervals", [])
                    if row.get("speaker_id") == speaker_id
                ],
                "text_summary": {
                    "status": "available" if len(attributed) >= 3 else "insufficient_attributed_segments",
                    "method": "extractive_excerpts; no inferred intent or emotion",
                    "excerpts": [
                        {
                            "text": row.get("text"),
                            "start": row.get("start"),
                            "end": row.get("end"),
                            "transcript_segment_id": row.get("id", row.get("segment_index")),
                        }
                        for row in attributed[:3]
                    ]
                    if len(attributed) >= 3
                    else [],
                    "source": "acoustically_attributed_transcript",
                },
            }
        )
    return result
