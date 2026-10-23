"""Speaker matching & transcript alignment (Phase 5).

Matches Whisper transcription segments with live speaker events (from Phase 3)
based on timestamp overlap. Generates aligned JSON, SRT, and WebVTT artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass

from xsa.logging_setup import get_logger
from xsa.transcriber.engine import SegmentItem, TranscriptionResult

log = get_logger("ALIGNER")


@dataclass
class SpeakerInterval:
    username: str
    display_name: str
    role: str
    start_s: float
    end_s: float


def build_speaker_intervals(events: list[dict]) -> list[SpeakerInterval]:
    """
    Convert a list of raw speaker events into continuous time intervals.
    Events format from Phase 3:
      {"timestamp": 12.3, "event": "speaker_started", "username": "alice", ...}
      {"timestamp": 25.1, "event": "speaker_stopped", "username": "alice", ...}
    """
    intervals: list[SpeakerInterval] = []
    current_speakers: dict[str, dict] = {}  # username -> {start_s, display_name, role}

    for ev in events:
        ev_type = ev.get("event")
        ts = ev.get("timestamp")
        username = ev.get("username")
        display_name = ev.get("display_name", "")
        role = ev.get("role", "speaker")

        if ts is None or not username:
            continue

        if ev_type == "speaker_started":
            # If someone was already speaking, close them out
            for cur_user, cur_data in list(current_speakers.items()):
                if cur_user != username:
                    intervals.append(
                        SpeakerInterval(
                            username=cur_user,
                            display_name=cur_data.get("display_name", ""),
                            role=cur_data.get("role", "speaker"),
                            start_s=cur_data["start_s"],
                            end_s=ts,
                        )
                    )
                    del current_speakers[cur_user]

            current_speakers[username] = {
                "start_s": ts,
                "display_name": display_name,
                "role": role,
            }

        elif ev_type == "speaker_stopped":
            if username in current_speakers:
                start_s = current_speakers[username]["start_s"]
                intervals.append(
                    SpeakerInterval(
                        username=username,
                        display_name=current_speakers[username].get("display_name", ""),
                        role=current_speakers[username].get("role", "speaker"),
                        start_s=start_s,
                        end_s=max(start_s, ts),
                    )
                )
                del current_speakers[username]

    # Close any still-open intervals with a large end time
    for cur_user, cur_data in current_speakers.items():
        intervals.append(
            SpeakerInterval(
                username=cur_user,
                display_name=cur_data.get("display_name", ""),
                role=cur_data.get("role", "speaker"),
                start_s=cur_data["start_s"],
                end_s=cur_data["start_s"] + 3600.0,
            )
        )

    return intervals


def align_segments_with_speakers(
    segments: list[SegmentItem],
    intervals: list[SpeakerInterval],
) -> list[SegmentItem]:
    """
    For each Whisper segment, determine the most likely active speaker
    based on the time overlap with speaker intervals.
    """
    if not intervals:
        log.info("No speaker intervals provided, leaving speaker names empty")
        return segments

    aligned: list[SegmentItem] = []

    for seg in segments:
        best_speaker = None
        best_overlap = 0.0

        for interval in intervals:
            # Overlap between [seg.start, seg.end] and [interval.start_s, interval.end_s]
            overlap_start = max(seg.start, interval.start_s)
            overlap_end = min(seg.end, interval.end_s)
            overlap = max(0.0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = interval.username

        # If overlap is at least 20% of segment duration, or > 0.5 seconds
        seg_dur = max(0.1, seg.end - seg.start)
        if best_speaker and (best_overlap / seg_dur >= 0.2 or best_overlap >= 0.5):
            seg.speaker = best_speaker
        else:
            # Check nearest interval within 2 seconds
            nearest = None
            min_dist = 2.0
            for interval in intervals:
                dist = min(abs(seg.start - interval.end_s), abs(seg.end - interval.start_s))
                if dist < min_dist:
                    min_dist = dist
                    nearest = interval.username
            seg.speaker = nearest or "unknown"

        aligned.append(seg)

    return aligned


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds into SRT timestamp HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_vtt_timestamp(seconds: float) -> str:
    """Format seconds into WebVTT timestamp HH:MM:SS.mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def export_to_srt(segments: list[SegmentItem]) -> str:
    """Generate SubRip (.srt) subtitle string."""
    lines = []
    for i, seg in enumerate(segments, 1):
        start_ts = format_srt_timestamp(seg.start)
        end_ts = format_srt_timestamp(seg.end)
        speaker_tag = f"[@{seg.speaker}]: " if seg.speaker and seg.speaker != "unknown" else ""
        lines.append(str(i))
        lines.append(f"{start_ts} --> {end_ts}")
        lines.append(f"{speaker_tag}{seg.text}")
        lines.append("")
    return "\n".join(lines)


def export_to_vtt(segments: list[SegmentItem]) -> str:
    """Generate WebVTT (.vtt) subtitle string."""
    lines = ["WEBVTT", ""]
    for i, seg in enumerate(segments, 1):
        start_ts = format_vtt_timestamp(seg.start)
        end_ts = format_vtt_timestamp(seg.end)
        speaker_tag = f"<v @{seg.speaker}>" if seg.speaker and seg.speaker != "unknown" else ""
        end_tag = "</v>" if speaker_tag else ""
        lines.append(str(i))
        lines.append(f"{start_ts} --> {end_ts}")
        lines.append(f"{speaker_tag}{seg.text}{end_tag}")
        lines.append("")
    return "\n".join(lines)


def export_to_json(
    result: TranscriptionResult,
    participants_map: dict[str, dict] | None = None,
) -> dict:
    """Serialize transcription and aligned speakers into structured JSON."""
    parts = participants_map or {}
    return {
        "language": result.language,
        "duration_seconds": result.duration,
        "full_text": result.text,
        "segments": [
            {
                "id": seg.id,
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "start_ms": int(seg.start * 1000),
                "end_ms": int(seg.end * 1000),
                "speaker": seg.speaker,
                "display_name": parts.get(seg.speaker or "", {}).get("display_name", ""),
                "text": seg.text,
                "words": [
                    {
                        "word": w.word,
                        "start": round(w.start, 2),
                        "end": round(w.end, 2),
                        "start_ms": int(w.start * 1000),
                        "end_ms": int(w.end * 1000),
                        "probability": round(w.probability, 3),
                    }
                    for w in seg.words
                ],
            }
            for seg in result.segments
        ],
    }
