"""Evidence-first, rerunnable analysis artifacts for a single recorded Space."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from xsa.db import Participant, Space, SpaceParticipant, SpeakerEvent, get_session
from xsa.paths import SpacePaths
from xsa.speech_analytics import attribute_segments, load_verified_timeline, speaker_insights

STOPWORDS = {
    "في",
    "من",
    "على",
    "الى",
    "إلى",
    "عن",
    "مع",
    "هذا",
    "هذه",
    "ذلك",
    "كان",
    "كانت",
    "ان",
    "أن",
    "لا",
    "ما",
    "هو",
    "هي",
    "التي",
    "الذي",
    "كل",
    "او",
    "أو",
    "ثم",
    "بعد",
    "قبل",
    "عند",
    "كما",
    "لكن",
    "حتى",
    "اذا",
    "إذا",
    "يعني",
    "طيب",
    "والله",
    "انا",
    "أنا",
    "انت",
    "أنت",
    "احنا",
    "نحن",
    "ايه",
    "إيه",
    "اه",
    "آه",
    "بس",
    "انه",
    "إنه",
    "there",
    "their",
    "about",
    "from",
    "with",
    "that",
}
QUESTION_START = re.compile(r"^(?:هل|لماذا|كيف|متى|أين|اين|ماذا|من|وش|ليش|وين|ليه)\b")
HANDLE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{1,15})")
POTENTIAL_PROFANITY = ("يلعن", "اللعنة", "كلب", "حمار", "غبي", "حقير")


def _read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _write_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _seconds(item: dict, key: str) -> float | None:
    value = item.get(key)
    if value is None and item.get(f"{key}_ms") is not None:
        value = float(item[f"{key}_ms"]) / 1000
    try:
        parsed = float(value)
        return max(0.0, parsed) if parsed == parsed else None
    except (TypeError, ValueError):
        return None


def _display(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _speech_timing_metrics(regions: list[dict], duration: float | None) -> dict[str, Any]:
    """Report measured word-timed speech occupancy without implying ASR recall."""
    speech_seconds = round(sum(max(0.0, row["end"] - row["start"]) for row in regions), 2)
    ratio = round(min(100.0, speech_seconds / duration * 100), 2) if duration and duration > 0 else None
    return {
        "speech_time_seconds": speech_seconds,
        "speech_time_ratio_percent": ratio,
        "transcript_coverage_percent": None,
        "transcript_coverage_status": "UNKNOWN_WITHOUT_REFERENCE_TRANSCRIPT",
    }


def _load_transcript(paths: SpacePaths) -> dict:
    value = _read(paths.transcript_json)
    if isinstance(value, dict) and isinstance(value.get("segments"), list):
        return value
    value = _read(paths.transcript_normalized)
    return value if isinstance(value, dict) else {"segments": [], "full_text": "", "language": None}


def _load_speaker_events(paths: SpacePaths) -> list[dict]:
    events: list[dict] = []
    for candidate in (paths.speaker_events_live, paths.root / "speaker_events_live.jsonl"):
        if not candidate.is_file():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        events.append(value)
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        if events:
            break
    if not events:
        value = _read(paths.speaker_events_json)
        if isinstance(value, list):
            events = [item for item in value if isinstance(item, dict)]
    return events


def _word_regions(segments: list[dict]) -> list[dict]:
    words: list[dict] = []
    for index, segment in enumerate(segments):
        segment_id = segment.get("id", index)
        speaker = segment.get("speaker") or segment.get("speaker_label")
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            start, end = _seconds(word, "start"), _seconds(word, "end")
            text = str(word.get("word") or word.get("text") or "").strip()
            if start is None or end is None or not text:
                continue
            words.append(
                {
                    "start": start,
                    "end": max(start, end),
                    "text": text,
                    "segment_id": segment_id,
                    "speaker_id": speaker,
                    "confidence": word.get("probability"),
                }
            )
    words.sort(key=lambda row: (row["start"], row["end"]))
    if not words:
        for index, segment in enumerate(segments):
            start, end = _seconds(segment, "start"), _seconds(segment, "end")
            text = str(segment.get("text") or "").strip()
            if start is not None and end is not None and text:
                words.append(
                    {
                        "start": start,
                        "end": max(start, end),
                        "text": text,
                        "segment_id": segment.get("id", index),
                        "speaker_id": segment.get("speaker") or segment.get("speaker_label"),
                        "confidence": segment.get("confidence"),
                    }
                )
    regions: list[dict] = []
    for word in words:
        if (
            regions
            and word["start"] - regions[-1]["end"] <= 1.25
            and word["speaker_id"] == regions[-1]["speaker_id"]
        ):
            region = regions[-1]
            region["end"] = max(region["end"], word["end"])
            region["text"] = (region["text"] + " " + word["text"]).strip()
            region["transcript_segment_ids"] = list(
                dict.fromkeys(region["transcript_segment_ids"] + [word["segment_id"]])
            )
            confidences = [
                x for x in (region.get("confidence"), word.get("confidence")) if isinstance(x, (int, float))
            ]
            region["confidence"] = sum(confidences) / len(confidences) if confidences else None
        else:
            regions.append(
                {
                    "start": word["start"],
                    "end": word["end"],
                    "text": word["text"],
                    "speaker_id": word["speaker_id"],
                    "participant_id": None,
                    "transcript_segment_ids": [word["segment_id"]],
                    "confidence": word.get("confidence"),
                }
            )
    for item in regions:
        item.update(
            start_display=_display(item["start"]),
            end_display=_display(item["end"]),
            playback_time=item["start"],
            topic_id=None,
            discussion_id=None,
        )
    return regions


def _lexical_topics(regions: list[dict], window_seconds: float = 300) -> list[dict]:
    buckets: dict[int, list[dict]] = {}
    for region in regions:
        bucket = int(region["start"] // window_seconds)
        buckets.setdefault(bucket, []).append(region)
    topics = []
    for bucket, rows in sorted(buckets.items()):
        text = " ".join(row["text"] for row in rows)
        tokens = re.findall(r"[\w\u0600-\u06ff]{3,}", text.casefold())
        common = [
            (word, count)
            for word, count in Counter(tokens).most_common(80)
            if word not in STOPWORDS and not word.isdigit()
        ]
        if not rows:
            continue
        topics.append(
            {
                "topic_id": f"LEX-{bucket + 1:03d}",
                "title": "، ".join(word for word, _count in common[:3]) or "مقاطع حديث غير معنونة",
                "summary": "مؤشرات كلمات من النص داخل نافذة زمنية؛ تحتاج مراجعة بشرية ولا تمثل فهماً دلالياً مؤكداً.",
                "start": rows[0]["start"],
                "end": rows[-1]["end"],
                "start_display": _display(rows[0]["start"]),
                "end_display": _display(rows[-1]["end"]),
                "keywords": [word for word, _count in common[:8]],
                "transcript_segment_ids": list(
                    dict.fromkeys(sid for row in rows for sid in row["transcript_segment_ids"])
                ),
                "confidence": None,
                "method": "lexical_window_5m",
            }
        )
    return topics


def _questions(segments: list[dict]) -> list[dict]:
    results = []
    for index, item in enumerate(segments):
        text = str(item.get("text") or "").strip()
        if not text or not ("?" in text or "؟" in text or QUESTION_START.search(text)):
            continue
        start, end = _seconds(item, "start"), _seconds(item, "end")
        results.append(
            {
                "id": item.get("id", index),
                "start": start,
                "end": end,
                "start_display": _display(start),
                "text": text,
                "speaker_id": item.get("speaker") or item.get("speaker_label"),
                "transcript_segment_ids": [item.get("id", index)],
                "method": "question_marker",
            }
        )
    return results


def _mentions(segments: list[dict]) -> list[dict]:
    found: dict[str, dict] = {}
    for index, item in enumerate(segments):
        text = str(item.get("text") or "")
        for match in HANDLE.finditer(text):
            handle = "@" + match.group(1)
            row = found.setdefault(
                handle.casefold(),
                {
                    "entity": handle,
                    "count": 0,
                    "timestamps": [],
                    "speaker_ids": [],
                    "transcript_segment_ids": [],
                },
            )
            row["count"] += 1
            start = _seconds(item, "start")
            if start is not None:
                row["timestamps"].append(start)
            speaker = item.get("speaker") or item.get("speaker_label")
            if speaker:
                row["speaker_ids"].append(speaker)
            row["transcript_segment_ids"].append(item.get("id", index))
    for row in found.values():
        row["speaker_ids"] = list(dict.fromkeys(row["speaker_ids"]))
        row["transcript_segment_ids"] = list(dict.fromkeys(row["transcript_segment_ids"]))
    return list(found.values())


def _conflict_markers(segments: list[dict]) -> list[dict]:
    # Conservative lexical flags only. No target or interpersonal accusation is inferred.
    results = []
    for index, item in enumerate(segments):
        text = str(item.get("text") or "")
        hits = [term for term in POTENTIAL_PROFANITY if term in text]
        if not hits:
            continue
        start, end = _seconds(item, "start"), _seconds(item, "end")
        results.append(
            {
                "start": start,
                "end": end,
                "start_display": _display(start),
                "speaker_id": item.get("speaker") or item.get("speaker_label"),
                "possible_target": None,
                "classification": "possible_profanity_marker",
                "severity": "low",
                "confidence": 0.25,
                "reason": "مطابقة معجمية أولية؛ يلزم الاستماع للسياق قبل أي استنتاج.",
                "excerpt": text[:300],
                "matched_terms": hits,
                "transcript_segment_ids": [item.get("id", index)],
            }
        )
    return results


def _database_evidence(space_id: str) -> tuple[list[dict], list[dict]]:
    participants: list[dict] = []
    events: list[dict] = []
    with get_session() as db:
        space_row = db.query(Space).filter_by(space_id=space_id).first()
        if not space_row:
            return participants, events
        joined = (
            db.query(SpaceParticipant, Participant)
            .join(Participant, SpaceParticipant.participant_id == Participant.id)
            .filter(SpaceParticipant.space_id == space_row.id)
            .all()
        )
        participants = [
            {
                "participant_id": participant.id,
                "username": participant.username,
                "display_name": participant.display_name,
                "x_user_id": participant.x_user_id,
                "avatar_url": participant.avatar_url,
                "role": link.role,
                "first_seen_at": link.first_seen_at.isoformat() if link.first_seen_at else None,
                "last_seen_at": link.last_seen_at.isoformat() if link.last_seen_at else None,
                "total_speaking_ms": link.total_speaking_ms,
                "speaking_turns": link.speaking_turns,
                "is_muted": link.is_muted,
                "provenance": "space_participants database row",
            }
            for link, participant in joined
        ]
        rows = (
            db.query(SpeakerEvent, Participant)
            .outerjoin(Participant, SpeakerEvent.participant_id == Participant.id)
            .filter(SpeakerEvent.space_id == space_row.id)
            .order_by(SpeakerEvent.recording_offset_ms, SpeakerEvent.id)
            .all()
        )
        events = [
            {
                "event_id": event.id,
                "timestamp": (event.recording_offset_ms / 1000)
                if event.recording_offset_ms is not None
                else None,
                "event": event.event_type,
                "username": event.username or (participant.username if participant else None),
                "display_name": participant.display_name if participant else None,
                "participant_id": event.participant_id,
                "source": event.source,
                "confidence": event.confidence,
                "wall_time": event.wall_time.isoformat() if event.wall_time else None,
            }
            for event, participant in rows
        ]
    return participants, events


def _observed_roles(events: list[dict], participants: list[dict]) -> list[dict]:
    indexed: dict[str, dict] = {}
    for participant in participants:
        username = participant.get("username")
        if username:
            key = str(username).casefold()
            indexed[key] = {
                "username": username,
                "display_name": participant.get("display_name"),
                "roles": [participant.get("role")] if participant.get("role") else [],
                "role_events": [],
                "x_user_id": participant.get("x_user_id"),
                "avatar_url": participant.get("avatar_url"),
                "evidence_sources": ["space_participants database row"],
                "acoustic_speech_confirmed": False,
            }
    for event in events:
        username = event.get("username")
        if not username:
            continue
        key = str(username).casefold()
        row = indexed.setdefault(
            key,
            {
                "username": username,
                "display_name": event.get("display_name"),
                "roles": [],
                "role_events": [],
                "x_user_id": None,
                "avatar_url": None,
                "evidence_sources": [],
                "acoustic_speech_confirmed": False,
            },
        )
        if event.get("display_name") and not row.get("display_name"):
            row["display_name"] = event["display_name"]
        role = event.get("role")
        kind = event.get("event") or event.get("event_type")
        if role and role not in row["roles"]:
            row["roles"].append(role)
        if kind in {
            "became_speaker",
            "became_listener",
            "host_detected",
            "cohost_detected",
            "speaker_started",
            "speaker_stopped",
        }:
            row["role_events"].append(
                {
                    "event": kind,
                    "timestamp": event.get("timestamp"),
                    "start_display": _display(_seconds(event, "timestamp")),
                    "role": role,
                    "source": event.get("source"),
                    "confidence": event.get("confidence"),
                }
            )
        source = event.get("source")
        if source and source not in row["evidence_sources"]:
            row["evidence_sources"].append(source)
    return list(indexed.values())


def media_inventory_record(paths: SpacePaths) -> dict:
    """Probe finalized source media before assigning report health fields."""
    from xsa.inventory import probe_media

    source = paths.recording
    if not source.is_file():
        source = paths.segment(0)
    if not source.is_file() and paths.playback.is_file():
        source = paths.playback
    media = probe_media(source) if source.is_file() else None
    good = bool(media and media.get("valid") and media.get("audio_valid"))
    return {
        "media": {"source": media, "duration_seconds": media.get("duration_seconds") if media else None},
        "recording_health": {
            "overall_status": "GOOD" if good else "CORRUPT" if media else "NO_MEDIA",
            "video_valid": media.get("video_valid") if media else False,
            "audio_valid": media.get("audio_valid") if media else False,
            "container_finalized": good,
            "known_interrupted": False,
            "stale_tail_suspected": False,
        },
    }


def build_space_analysis(paths: SpacePaths, *, space: dict, inventory_record: dict | None = None) -> dict:
    """Build idempotent derived artifacts; source media and raw transcript stay untouched."""
    transcript = _load_transcript(paths)
    segments = [item for item in transcript.get("segments", []) if isinstance(item, dict)]
    transcription_status = transcript.get("recognition_state") or (
        "DONE" if segments else "NO_SPEECH" if paths.transcript_json.is_file() else "NOT_TRANSCRIBED"
    )
    regions = _word_regions(segments)
    duration = (inventory_record or {}).get("media", {}).get("duration_seconds") or transcript.get(
        "duration_seconds"
    )
    health = (inventory_record or {}).get("recording_health") or {
        "overall_status": "UNKNOWN",
        "video_valid": None,
        "audio_valid": None,
        "container_finalized": None,
        "known_interrupted": None,
    }
    media = (inventory_record or {}).get("media", {})
    health_report = {
        "schema_version": 1,
        "space_id": space["space_id"],
        "overall_status": health.get("overall_status", "UNKNOWN"),
        "video_valid": health.get("video_valid"),
        "audio_valid": health.get("audio_valid"),
        "container_finalized": health.get("container_finalized"),
        "duration_seconds": duration,
        "audio_duration_seconds": (media.get("source") or {}).get("audio_duration_seconds"),
        "video_duration_seconds": (media.get("source") or {}).get("video_duration_seconds"),
        "audio_duration_source": (media.get("source") or {}).get("audio_duration_source"),
        "video_duration_source": (media.get("source") or {}).get("video_duration_source"),
        "size_bytes": (media.get("source") or {}).get("size_bytes"),
        "known_interrupted": health.get("known_interrupted", False),
        "stale_tail_suspected": health.get("stale_tail_suspected", False),
        "transcript_segment_count": len(segments),
        "transcription_status": transcription_status,
        # Without a reference transcript we cannot know how much speech ASR omitted.
        **_speech_timing_metrics(regions, duration),
        "warning": (
            "لم ينتج التفريغ أي مقاطع كلام؛ الملف قد يكون صامتاً أو تعذّر التقاط صوت المساحة."
            if transcription_status == "NO_SPEECH"
            else "لم يبدأ التفريغ بعد؛ لا يمكن الحكم على وجود كلام في التسجيل."
            if transcription_status == "NOT_TRANSCRIBED"
            else "سلامة الحاوية لا تثبت جودة الصوت أو خلوه من الضجيج؛ افحص الاستماع عند الشك."
        ),
        "checked_at": datetime.now(UTC).isoformat(),
    }
    x_timeline = _read(paths.speaker_timeline_json, {}) or {}
    acoustic, acoustic_status = load_verified_timeline(
        paths.analysis_dir,
        paths.root / "audio" / "analysis.wav",
        paths.transcript_segments,
        space["space_id"],
    )
    if acoustic:
        segments = attribute_segments(segments, acoustic)
        regions = _word_regions(segments)
    local_events = x_timeline.get("events", []) if isinstance(x_timeline, dict) else []
    file_participants = _read(paths.participants_json, {}) or {}
    if isinstance(file_participants, dict):
        file_participants = file_participants.get("participants", [])
    if isinstance(file_participants, dict):
        file_participants = list(file_participants.values())
    db_participants, db_events = _database_evidence(space["space_id"])
    if db_participants:
        participant_list = db_participants
    else:
        participant_list = (
            [p for p in file_participants if isinstance(p, dict)]
            if isinstance(file_participants, list)
            else []
        )
    all_events = db_events or [e for e in local_events if isinstance(e, dict)] or _load_speaker_events(paths)
    observed_speakers = _observed_roles(all_events, participant_list)
    speaker_report = {
        "schema_version": 1,
        "acoustic_diarization": {
            # The credential is configured only in the isolated worker. This
            # process cannot infer its presence from its own environment.
            "status": acoustic_status,
            "speaker_count": len(acoustic.get("speakers", [])) if isinstance(acoustic, dict) else 0,
            "speakers": speaker_insights(acoustic, segments) if acoustic else [],
            "coverage": acoustic.get("coverage") if acoustic else None,
            "intervals": acoustic.get("intervals", []) if acoustic else [],
            "identity_policy": "anonymous acoustic IDs are not mapped to X accounts without timestamped evidence",
        },
        "x_observed_participants": observed_speakers,
        "x_observation_method": "x_gql_or_x_dom" if observed_speakers else None,
        "transcript_speaker_attribution": "unavailable"
        if any(not r.get("speaker_id") for r in regions)
        else "source_labeled",
        "speaking_durations_source": "Unavailable: X role changes do not measure acoustic speech duration"
        if not acoustic
        else "acoustic diarization",
    }
    topics = _lexical_topics(regions)
    questions = _questions(segments)
    mentions = _mentions(segments)
    conflicts = _conflict_markers(segments)
    events = all_events
    prior_summary = _read(paths.summary_json, {}) or {}
    overview = {
        "schema_version": 1,
        "space_id": space["space_id"],
        "title": space.get("title"),
        "host": space.get("host"),
        "duration_seconds": duration,
        "recording_health": health_report,
        "participant_count": len(participant_list),
        "acoustic_speaker_count": speaker_report["acoustic_diarization"]["speaker_count"],
        "x_observed_speakers": len(observed_speakers),
        "transcript_segment_count": len(segments),
        "executive_summary": prior_summary.get("executive_summary"),
        "timeline_region_count": len(regions),
        "main_topics": topics[:10],
        "questions": questions,
        "mentions": mentions,
        "conflict_markers": conflicts,
        "analysis_limitations": [
            "إسناد الكلام مبني على فصل صوتي متحقق منه؛ تبقى المقاطع المتداخلة أو غير الكافية بلا إسناد."
            if acoustic
            else "لا توجد نتيجة فصل صوتي متحققة من مصادر التسجيل الحالية؛ هويات الأصوات غير مثبتة.",
            "الموضوعات الحالية مؤشرات كلمات ضمن نوافذ زمنية وليست فهماً دلالياً مؤكداً.",
            "لا تُعرض القصص أو مسارات الجدل كحقائق دون دليل حواري كافٍ.",
        ],
    }
    diarization_stage = speaker_report["acoustic_diarization"]["status"]
    stage_status = {
        "MEDIA": "DONE" if health_report["video_valid"] or health_report["audio_valid"] else "FAILED",
        "AUDIO": "DONE" if health_report["audio_valid"] else "BLOCKED",
        "TRANSCRIPT": transcription_status,
        "DIARIZATION": diarization_stage,
        "SPEAKER_MAPPING": "PARTIAL" if acoustic else "BLOCKED",
        "LEXICAL_ANALYSIS": "DONE" if segments else "BLOCKED",
        "SEMANTIC_ANALYSIS": "NOT_STARTED",
        "REPORT": "DONE",
    }
    full = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "space": space,
        "recording_health": health_report,
        "overview": overview,
        "timeline": regions,
        "transcript_segments": segments,
        "speakers": speaker_report,
        "topics": topics,
        "stories": [],
        "discussions": [],
        "questions": questions,
        "claims": [],
        "mentions": mentions,
        "conflicts": conflicts,
        "speaker_events": events,
        "participants": participant_list,
        "processing": {
            "stages": stage_status,
            "HF_TOKEN_configured": None,
            "warnings": overview["analysis_limitations"],
        },
        "source_artifacts": {
            "media": "recording_000.mkv or playback.mp4",
            "transcript": "transcript.json",
            "speaker_events": "speaker_events.jsonl",
            "x_speaker_timeline": "speaker_timeline.json",
        },
    }
    _write_atomic(
        paths.analysis_dir / "timeline.json",
        {"space_id": space["space_id"], "duration_seconds": duration, "items": regions},
    )
    _write_atomic(paths.analysis_dir / "recording_health.json", health_report)
    _write_atomic(paths.analysis_dir / "speakers.json", speaker_report)
    _write_atomic(paths.analysis_dir / "topics.json", {"topics": topics})
    _write_atomic(paths.analysis_dir / "overview.json", overview)
    _write_atomic(paths.analysis_dir / "full_analysis.json", full)
    _write_atomic(
        paths.analysis_dir / "processing.json",
        {"stages": stage_status, "HF_TOKEN_configured": None},
    )
    return full


def search_space_analysis(paths: SpacePaths, query: str, *, limit: int = 100) -> list[dict]:
    value = _read(paths.analysis_dir / "timeline.json", {}) or {}
    needle = query.casefold().strip()
    if not needle:
        return []
    results = []
    for item in value.get("items", []):
        if needle in str(item.get("text") or "").casefold():
            results.append(
                {
                    key: item.get(key)
                    for key in (
                        "start",
                        "end",
                        "start_display",
                        "speaker_id",
                        "text",
                        "transcript_segment_ids",
                        "topic_id",
                    )
                }
            )
            if len(results) >= max(1, min(limit, 500)):
                break
    return results


def count_transcript_phrase(paths: SpacePaths, query: str, *, limit: int = 100) -> dict:
    """Count literal, case-insensitive mentions in the current committed text."""
    needle = query.casefold().strip()
    if not needle or len(needle) > 200:
        raise ValueError("Search phrase must contain between 1 and 200 characters")
    transcript = _load_transcript(paths)
    hits = []
    occurrences = 0
    matching_segments = 0
    for index, segment in enumerate(transcript.get("segments", [])):
        text = str(segment.get("text") or "")
        count = text.casefold().count(needle)
        if not count:
            continue
        occurrences += count
        matching_segments += 1
        if len(hits) < max(1, min(limit, 500)):
            hits.append(
                {
                    "transcript_segment_id": segment.get("id", index),
                    "start": _seconds(segment, "start"),
                    "end": _seconds(segment, "end"),
                    "text": text,
                    "occurrences": count,
                }
            )
    return {
        "query": query.strip(),
        "occurrence_count": occurrences,
        "matching_segment_count": matching_segments,
        "results": hits,
        "truncated": matching_segments > len(hits),
        "method": "literal_case_insensitive_non_overlapping; no stemming or inferred mentions",
    }
