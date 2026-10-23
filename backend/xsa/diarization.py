"""Offline, resumable speaker diarization and transcript alignment.

All diarization times are relative to the existing normalized analysis audio.
This module never guesses an X account from a voice. Heavy model dependencies
are loaded lazily so the capture service remains lightweight.
"""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import shutil
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MODEL_ID = "pyannote/speaker-diarization-community-1"
SCHEMA_VERSION = 1


class DiarizationUnavailable(RuntimeError):
    """Optional model or required model access is unavailable."""


class ActiveRecordingError(RuntimeError):
    """Diarization is refused while a recording is active."""


def normalize_intervals(
    raw_intervals: Iterable[dict[str, Any]], duration: float, *, merge_gap_seconds: float = 0.1
) -> list[dict[str, Any]]:
    """Clip, validate, and deterministically label diarization intervals.

    Overlap between different speakers is preserved. Same-speaker fragments
    within the configured tiny-gap threshold form one turn, while original
    speech spans retain the gap for accurate coverage. Speaker IDs are assigned
    by first audible occurrence, not model label order.
    """
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("recording duration must be positive")
    if not math.isfinite(merge_gap_seconds) or merge_gap_seconds < 0:
        raise ValueError("merge_gap_seconds must be finite and non-negative")
    cleaned: list[tuple[float, float, str]] = []
    for item in raw_intervals:
        try:
            raw_start = float(item["start"])
            raw_end = float(item["end"])
            label = str(item.get("speaker") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not label or not math.isfinite(raw_start) or not math.isfinite(raw_end):
            continue
        start = max(0.0, raw_start)
        end = min(float(duration), raw_end)
        if end <= start:
            continue
        cleaned.append((start, end, label))
    labels = {
        label: f"SPEAKER_{i:02d}"
        for i, label in enumerate(
            sorted(
                {label for _, _, label in cleaned},
                key=lambda speaker: (
                    min(start for start, _, item in cleaned if item == speaker),
                    speaker,
                ),
            )
        )
    }
    by_speaker: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for start, end, label in cleaned:
        by_speaker[label].append((start, end))
    intervals: list[dict[str, Any]] = []
    for label, spans in by_speaker.items():
        merged: list[dict[str, Any]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1]["end"] + merge_gap_seconds:
                gap = max(0.0, start - merged[-1]["end"])
                if gap:
                    merged[-1]["bridged_gaps"].append([round(merged[-1]["end"], 3), round(start, 3)])
                merged[-1]["end"] = max(merged[-1]["end"], end)
                previous_span = merged[-1]["speech_spans"][-1]
                if start <= previous_span[1]:
                    previous_span[1] = max(previous_span[1], end)
                else:
                    merged[-1]["speech_spans"].append([start, end])
            else:
                merged.append(
                    {"start": start, "end": end, "speech_spans": [[start, end]], "bridged_gaps": []}
                )
        for turn in merged:
            start, end = turn["start"], turn["end"]
            intervals.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "duration_seconds": round(end - start, 3),
                    "speaker_id": labels[label],
                    "model_label": label,
                    "speech_spans": [[round(a, 3), round(b, 3)] for a, b in turn["speech_spans"]],
                    "bridged_gaps": turn["bridged_gaps"],
                    "speech_seconds": round(sum(b - a for a, b in turn["speech_spans"]), 3),
                    "merge_gap_threshold_seconds": merge_gap_seconds,
                    "username": None,
                    "identity_mapping": None,
                    "evidence_source": "pyannote.audio",
                    "confidence": None,
                    "confidence_source": "not_provided_by_model",
                    "transcript_segment_refs": [],
                    "overlaps": [],
                }
            )
    intervals.sort(key=lambda row: (row["start"], row["end"], row["speaker_id"]))
    for i, left in enumerate(intervals):
        for right in intervals[i + 1 :]:
            if right["start"] >= left["end"]:
                break
            overlaps_speech = any(
                min(a_end, b_end) > max(a_start, b_start)
                for a_start, a_end in left.get("speech_spans", [[left["start"], left["end"]]])
                for b_start, b_end in right.get("speech_spans", [[right["start"], right["end"]]])
            )
            if left["speaker_id"] != right["speaker_id"] and overlaps_speech:
                left["overlaps"].append(right["speaker_id"])
                if left["speaker_id"] not in right["overlaps"]:
                    right["overlaps"].append(left["speaker_id"])
    return intervals


def _segment_bounds(segment: dict[str, Any]) -> tuple[float, float] | None:
    try:
        start_value = segment["start"] if segment.get("start") is not None else segment["start_ms"] / 1000
        end_value = segment["end"] if segment.get("end") is not None else segment["end_ms"] / 1000
        start = float(start_value)
        end = float(end_value)
    except (KeyError, TypeError, ValueError):
        return None
    return (start, end) if math.isfinite(start) and math.isfinite(end) and end > start >= 0 else None


def associate_transcript(
    intervals: list[dict[str, Any]], segments: list[dict[str, Any]], *, min_coverage: float = 0.5
) -> list[dict[str, Any]]:
    """Associate segments only when one diarized speaker has clear temporal evidence."""
    aligned: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        bounds = _segment_bounds(segment)
        segment_id = segment.get("id", segment.get("segment_index", index))
        if not bounds:
            aligned.append(
                {
                    "transcript_segment_id": segment_id,
                    "speaker_id": None,
                    "username": None,
                    "method": "unknown",
                    "confidence": None,
                    "reason": "invalid_segment_time",
                    "candidate_speakers": [],
                    "start": None,
                    "end": None,
                    "transcript_text": segment.get("text"),
                }
            )
            continue
        start, end = bounds
        duration = end - start
        by_speaker: dict[str, float] = defaultdict(float)
        touched: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for interval in intervals:
            for speech_start, speech_end in interval.get(
                "speech_spans", [[interval["start"], interval["end"]]]
            ):
                overlap = max(0.0, min(end, speech_end) - max(start, speech_start))
                if overlap:
                    sid = interval["speaker_id"]
                    by_speaker[sid] += overlap
                    if interval not in touched[sid]:
                        touched[sid].append(interval)
        ranked = sorted(by_speaker.items(), key=lambda pair: (-pair[1], pair[0]))
        candidate_speakers = [sid for sid, overlap in ranked if overlap / duration >= 0.05]
        simultaneous = any(
            min(end, a_end, b_end) > max(start, a_start, b_start)
            for index, a in enumerate(intervals)
            for b in intervals[index + 1 :]
            if a["speaker_id"] != b["speaker_id"]
            for a_start, a_end in a.get("speech_spans", [[a["start"], a["end"]]])
            for b_start, b_end in b.get("speech_spans", [[b["start"], b["end"]]])
        )
        best_id, best_overlap = ranked[0] if ranked else (None, 0.0)
        ratio = best_overlap / duration if duration else 0.0
        ambiguous = len(candidate_speakers) > 1 or simultaneous
        attributed = best_id if best_id and ratio >= min_coverage and not ambiguous else None
        overlap_details = [
            {
                "speaker_id": sid,
                "overlap_seconds": round(overlap, 3),
                "overlap_percent_of_segment": round(overlap * 100 / duration, 2),
            }
            for sid, overlap in ranked
        ]
        associated = {
            "transcript_segment_id": segment_id,
            "speaker_id": attributed,
            "username": next((i["username"] for i in touched.get(attributed, []) if i.get("username")), None)
            if attributed
            else None,
            "method": "diarization_time_overlap" if attributed else "unknown",
            "confidence": round(min(1.0, ratio), 3) if attributed else None,
            "reason": None
            if attributed
            else (
                "overlapping_speakers"
                if simultaneous
                else "multiple_candidate_speakers"
                if len(candidate_speakers) > 1
                else "insufficient_speech_overlap"
            ),
            "candidate_speakers": candidate_speakers,
            "overlap_by_speaker": overlap_details,
            "ambiguous": ambiguous,
            "quality": "high"
            if attributed and ratio >= 0.8
            else "moderate"
            if attributed
            else "ambiguous"
            if ambiguous
            else "unassigned",
            "start": round(start, 3),
            "end": round(end, 3),
            "transcript_text": segment.get("text"),
        }
        aligned.append(associated)
        if attributed:
            for interval in touched[attributed]:
                if segment_id not in interval["transcript_segment_refs"]:
                    interval["transcript_segment_refs"].append(segment_id)
    return aligned


def _union_duration(spans: list[tuple[float, float]]) -> float:
    if not spans:
        return 0.0
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def _concurrent_overlap_duration(intervals: list[dict[str, Any]]) -> float:
    edges: list[tuple[float, int]] = []
    for interval in intervals:
        for start, end in interval.get("speech_spans", [[interval["start"], interval["end"]]]):
            edges.append((start, 1))
            edges.append((end, -1))
    edges.sort(key=lambda edge: (edge[0], edge[1]))  # end before start at a shared boundary
    count = 0
    previous: float | None = None
    overlap = 0.0
    for moment, delta in edges:
        if previous is not None and count > 1:
            overlap += moment - previous
        count += delta
        previous = moment
    return overlap


def build_offline_timeline(
    *,
    space_id: str,
    duration: float,
    raw_intervals: Iterable[dict[str, Any]],
    transcript_segments: list[dict[str, Any]],
    identity_mappings: dict[str, dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    merge_gap_seconds: float = 0.1,
    require_mapping_fingerprint: bool = False,
) -> dict[str, Any]:
    """Create speaker intervals, transcript links, uncertainty, and statistics."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("recording duration must be positive")
    metadata = metadata or {}
    intervals = normalize_intervals(raw_intervals, duration, merge_gap_seconds=merge_gap_seconds)
    for interval in intervals:
        mapping = (identity_mappings or {}).get(interval["speaker_id"])
        if not mapping:
            continue
        expected_fingerprint = metadata.get("diarization_fingerprint")
        mapped_fingerprint = mapping.get("diarization_fingerprint")
        if (require_mapping_fingerprint and not mapped_fingerprint) or (
            mapped_fingerprint and expected_fingerprint and mapped_fingerprint != expected_fingerprint
        ):
            interval["identity_mapping"] = {
                "method": mapping.get("method", "unknown"),
                "candidate_username": mapping.get("username") or mapping.get("candidate_username"),
                "verified": False,
                "blocked_reason": "mapping_fingerprint_required"
                if not mapped_fingerprint
                else "mapping_belongs_to_different_diarization_fingerprint",
                "mapping_fingerprint": mapped_fingerprint,
                "diarization_fingerprint": expected_fingerprint,
            }
            continue
        method = mapping.get("method")
        candidate = (mapping.get("username") or mapping.get("candidate_username") or "").lstrip("@")
        evidence = str(mapping.get("evidence") or "")
        aliases = {"observed": "x_dom", "inferred": "inferred_overlap"}
        method = aliases.get(method, method)
        if method not in {"manual", "x_dom", "inferred_overlap", "unknown"} or not candidate:
            continue
        normalized_evidence = evidence.casefold().replace(" ", "_")
        if (
            method == "inferred_overlap"
            and "voice" in normalized_evidence
            and "similarity" in normalized_evidence
        ):
            interval["identity_mapping"] = {
                "method": "inferred_overlap",
                "candidate_username": candidate,
                "evidence": evidence,
                "verified": False,
                "confidence": mapping.get("confidence"),
                "blocked_reason": "voice_similarity_alone_is_not_identity_proof",
            }
            continue
        if method == "x_dom" and not evidence:
            continue
        interval["identity_mapping"] = {
            "method": method,
            "evidence": evidence or None,
            "verified": method in {"manual", "x_dom"},
            "confidence": mapping.get("confidence"),
        }
        if method in {"manual", "x_dom"}:
            interval["username"] = candidate
    associations = associate_transcript(intervals, transcript_segments)
    word_segments = [
        {
            **word,
            "id": word.get("id", f"{segment.get('id', i)}:{word_index}"),
            "text": word.get("word", word.get("text", "")),
        }
        for i, segment in enumerate(transcript_segments)
        for word_index, word in enumerate(segment.get("words", []) or [])
        if isinstance(word, dict)
    ]
    word_associations = associate_transcript(intervals, word_segments) if word_segments else []
    for association in word_associations:
        if association.get("speaker_id"):
            association["method"] = "diarization_word_time_overlap"
    by_speaker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for interval in intervals:
        by_speaker[interval["speaker_id"]].append(interval)
    overlap_duration = _concurrent_overlap_duration(intervals)
    speech_spans = [
        tuple(span)
        for interval in intervals
        for span in interval.get("speech_spans", [[interval["start"], interval["end"]]])
    ]
    unique_speech = _union_duration(speech_spans)
    segment_by_id = {
        segment.get("id", segment.get("segment_index", i)): segment
        for i, segment in enumerate(transcript_segments)
    }
    known_speech_spans = [
        bounds
        for segment in transcript_segments
        if segment.get("text") and (bounds := _segment_bounds(segment))
    ]
    known_speech_total = _union_duration(known_speech_spans)
    speakers: list[dict[str, Any]] = []
    for speaker_id, turns in sorted(by_speaker.items()):
        durations = [i.get("speech_seconds", i["end"] - i["start"]) for i in turns]
        username = next((i["username"] for i in turns if i["username"]), None)
        assigned_spans = []
        for association in associations:
            if association["speaker_id"] == speaker_id:
                segment = segment_by_id.get(association["transcript_segment_id"], {})
                bounds = _segment_bounds(segment) if segment else None
                if bounds:
                    assigned_spans.append(bounds)
        known_speaker_speech = _union_duration(assigned_spans)
        speakers.append(
            {
                "speaker_id": speaker_id,
                "username": username,
                "identity_mapping": next(
                    (i["identity_mapping"] for i in turns if i["identity_mapping"]), None
                ),
                "total_speaking_seconds": round(sum(durations), 3),
                "turn_count": len(turns),
                "average_turn_seconds": round(sum(durations) / len(durations), 3),
                "median_turn_seconds": round(statistics.median(durations), 3),
                "longest_turn_seconds": round(max(durations), 3),
                "shortest_turn_seconds": round(min(durations), 3),
                "first_spoken": min(i["start"] for i in turns),
                "last_spoken": max(i["end"] for i in turns),
                "recording_coverage_percent": round(sum(durations) * 100 / duration, 3),
                "transcript_attributed_seconds": round(known_speaker_speech, 3),
                "percentage_of_known_speech": round(known_speaker_speech * 100 / known_speech_total, 3)
                if known_speech_total
                else None,
                "transcript_segment_ids": [
                    a["transcript_segment_id"] for a in associations if a["speaker_id"] == speaker_id
                ],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "space_id": space_id,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "completed",
        "detection_result": "speaker_intervals_found" if intervals else "no_intervals_returned_by_model",
        "timebase": {
            "coordinate": "analysis_audio_seconds",
            "description": "Seconds from the start of the Phase B normalized analysis audio.",
            "media_wall_origin_known": False,
            "space_time_available": False,
        },
        "source": metadata,
        "duration_seconds": round(duration, 3),
        "intervals": intervals,
        "transcript_associations": associations,
        # Derived from Phase B timestamps at read/build time; not written as
        # per-word DB rows to avoid multiplying database storage.
        "word_associations": word_associations,
        "speakers": speakers,
        "coverage": {
            "speech_seconds_unique": round(unique_speech, 3),
            "speech_seconds_summed_by_speaker": round(
                sum(i.get("speech_seconds", i["end"] - i["start"]) for i in intervals), 3
            ),
            "silence_or_unassigned_seconds": round(max(0.0, duration - unique_speech), 3),
            "overlap_seconds": round(overlap_duration, 3),
            "recording_coverage_percent": round(unique_speech * 100 / duration, 3),
        },
        "identity_policy": "Voice similarity alone never establishes an X account identity.",
        "quality_metrics": {
            "segment_count": len(intervals),
            "speaker_count": len(speakers),
            "median_segment_seconds": round(statistics.median([i["end"] - i["start"] for i in intervals]), 3)
            if intervals
            else 0.0,
            "shortest_segment_seconds": round(
                min((i["end"] - i["start"] for i in intervals), default=0.0), 3
            ),
            "longest_segment_seconds": round(max((i["end"] - i["start"] for i in intervals), default=0.0), 3),
            "unassigned_transcript_segments": sum(1 for a in associations if not a.get("speaker_id")),
            "ambiguous_transcript_segments": sum(1 for a in associations if a.get("ambiguous")),
            "transcript_segments_total": len(associations),
            "transcript_segments_assigned": sum(1 for a in associations if a.get("speaker_id")),
            "transcript_segments_with_multiple_speakers": sum(
                1 for a in associations if len(a.get("candidate_speakers", [])) > 1
            ),
            "words_total": len(word_associations),
            "words_assigned": sum(1 for a in word_associations if a.get("speaker_id")),
            "words_ambiguous": sum(1 for a in word_associations if a.get("ambiguous")),
            "words_unassigned": sum(1 for a in word_associations if not a.get("speaker_id")),
            "ground_truth_der_measured": False,
        },
        "normalization": {
            "merge_gap_threshold_seconds": merge_gap_seconds,
            "bridged_gaps_preserved_per_interval": True,
        },
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


@dataclass
class PyannoteDiarizer:
    """Lazy adapter for the local pyannote Community-1 pipeline."""

    token: str | None = None
    device: str = "auto"
    model_id: str = MODEL_ID
    _pipeline: Any = None
    actual_device: str | None = None
    execution_metrics: dict[str, Any] = field(default_factory=dict)

    def _load(self) -> None:
        try:
            import pyannote.audio as pyannote_audio
            import torch
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise DiarizationUnavailable(
                "The isolated worker image must provide compatible torch, torchaudio, torchcodec, and pyannote.audio packages."
            ) from exc
        pyannote_version = getattr(pyannote_audio, "__version__", "unknown")
        if pyannote_version == "unknown":
            try:
                from importlib.metadata import version

                pyannote_version = version("pyannote.audio")
            except Exception:
                pass
        selected = "cuda" if self.device == "auto" and torch.cuda.is_available() else self.device
        if selected == "auto":
            selected = "cpu"
        if selected == "cuda" and not torch.cuda.is_available():
            raise DiarizationUnavailable(
                "CUDA was requested but is not available; choose device='cpu' or 'auto'."
            )
        if selected == "cuda":
            torch.cuda.synchronize()
            free_before, total_before = torch.cuda.mem_get_info()
            self.execution_metrics.update(
                {
                    "gpu_name": torch.cuda.get_device_name(0),
                    "vram_total_mb": round(total_before / (1024 * 1024), 1),
                    "vram_free_before_model_mb": round(free_before / (1024 * 1024), 1),
                }
            )
        try:
            self._pipeline = Pipeline.from_pretrained(self.model_id, token=self.token)
            if self._pipeline is None:
                raise DiarizationUnavailable(
                    f"Model {self.model_id} was not available to the current account."
                )
            self._pipeline.to(torch.device(selected))
        except DiarizationUnavailable:
            raise
        except Exception as exc:
            # Never include the token or raw HTTP response in user-facing errors.
            raise DiarizationUnavailable(
                f"Could not load {self.model_id}; verify model access terms, token permissions, and cached assets."
            ) from exc
        self.actual_device = selected
        self.execution_metrics["device"] = selected
        self.execution_metrics["pyannote_version"] = pyannote_version
        if selected == "cuda":
            torch.cuda.synchronize()
            free_after, _ = torch.cuda.mem_get_info()
            self.execution_metrics.update(
                {
                    "vram_free_after_model_mb": round(free_after / (1024 * 1024), 1),
                    "model_allocated_mb": round(torch.cuda.memory_allocated(0) / (1024 * 1024), 1),
                    "model_reserved_mb": round(torch.cuda.memory_reserved(0) / (1024 * 1024), 1),
                }
            )

    def diarize(self, audio_path: Path) -> list[dict[str, Any]]:
        if self._pipeline is None:
            self._load()
        import torch

        started = time.perf_counter()
        try:
            if self.actual_device == "cuda":
                torch.cuda.synchronize()
                torch.cuda.reset_peak_memory_stats(0)
            result = self._pipeline(str(audio_path))
            if self.actual_device == "cuda":
                torch.cuda.synchronize()
                self.execution_metrics["inference_peak_allocated_mb"] = round(
                    torch.cuda.max_memory_allocated(0) / (1024 * 1024), 1
                )
            annotation = getattr(result, "speaker_diarization", result)
            return [
                {"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)}
                for turn, _, speaker in annotation.itertracks(yield_label=True)
            ]
        except Exception as exc:
            raise RuntimeError(
                f"Diarization inference failed for {audio_path.name}: {type(exc).__name__}"
            ) from exc
        finally:
            self.execution_metrics["processing_seconds"] = round(time.perf_counter() - started, 3)


def _diarize_child(connection: Any, audio_path: str, token: str | None, device: str, model_id: str) -> None:
    # The isolated Linux worker must not leave inference running after its
    # owning process dies. Handle the parent-death race before model loading.
    if sys.platform.startswith("linux"):
        import ctypes
        import signal

        parent = os.getppid()
        ctypes.CDLL(None).prctl(1, signal.SIGKILL)
        if os.getppid() != parent or parent == 1:
            os.kill(os.getpid(), signal.SIGKILL)
    """Spawn target: isolate model memory and allow the parent to cancel safely."""
    try:
        engine = PyannoteDiarizer(token=token, device=device, model_id=model_id)
        intervals = engine.diarize(Path(audio_path))
        connection.send(("ok", intervals, engine.actual_device, engine.execution_metrics))
    except BaseException as exc:
        connection.send(("error", type(exc).__name__, str(exc)))
    finally:
        connection.close()


@dataclass
class GuardedPyannoteDiarizer:
    """Run inference in a child and cancel it if capture resumes mid-analysis."""

    token: str | None = None
    device: str = "auto"
    model_id: str = MODEL_ID
    active_space_ids: Callable[[], Iterable[str]] = lambda: ()
    process_rows: Callable[[], Iterable[tuple[str, str]]] = lambda: ()
    poll_seconds: float = 3.0
    actual_device: str | None = None
    execution_metrics: dict[str, Any] = field(default_factory=dict)

    def diarize(self, audio_path: Path) -> list[dict[str, Any]]:
        assert_safe_to_diarize(active_space_ids=self.active_space_ids(), process_rows=self.process_rows())
        ctx = multiprocessing.get_context("spawn")
        receiver, sender = ctx.Pipe(duplex=False)
        worker = ctx.Process(
            target=_diarize_child,
            args=(sender, str(audio_path), self.token, self.device, self.model_id),
            name="xsa-speaker-diarization",
        )
        worker.start()
        sender.close()
        response = None
        try:
            while worker.is_alive():
                if receiver.poll(self.poll_seconds):
                    response = receiver.recv()
                    worker.join(timeout=2)
                    break
                try:
                    assert_safe_to_diarize(
                        active_space_ids=self.active_space_ids(), process_rows=self.process_rows()
                    )
                except Exception as exc:
                    worker.terminate()
                    worker.join(timeout=10)
                    if worker.is_alive() and hasattr(worker, "kill"):
                        worker.kill()
                        worker.join(timeout=5)
                    if isinstance(exc, ActiveRecordingError):
                        raise
                    raise ActiveRecordingError(
                        "Could not confirm that recording remains idle; diarization worker was stopped."
                    ) from exc
            if response is None and receiver.poll(0.1):
                response = receiver.recv()
        finally:
            receiver.close()
            if worker.is_alive():
                worker.join(timeout=1)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        if not response:
            raise RuntimeError("Diarization worker exited without a result.")
        if response[0] == "error":
            error_type, message = response[1], response[2]
            # Never echo the token even if an upstream exception includes it.
            if self.token:
                message = message.replace(self.token, "[redacted]")
            raise RuntimeError(f"Diarization failed ({error_type}): {message[:400]}")
        self.actual_device = response[2]
        self.execution_metrics = response[3] if len(response) > 3 else {"device": response[2]}
        return response[1]


def recording_processes(process_rows: Iterable[tuple[str, str]]) -> list[str]:
    """Return PIDs whose command lines show a recording MKV output."""
    active: list[str] = []
    for pid, command in process_rows:
        lower = command.lower()
        if "ffmpeg" in lower and "recording_" in lower and ".mkv" in lower:
            active.append(str(pid))
    return active


def assert_safe_to_diarize(
    *, active_space_ids: Iterable[str] = (), process_rows: Iterable[tuple[str, str]] = ()
) -> None:
    active = list(active_space_ids)
    ffmpeg = recording_processes(process_rows)
    if active or ffmpeg:
        raise ActiveRecordingError(
            f"Speaker diarization deferred while recording is active (spaces={len(active)}, ffmpeg={len(ffmpeg)})."
        )


def load_transcript_segments(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("segments", "transcript_segments", "items"):
            if isinstance(value.get(key), list):
                return value[key]
    raise ValueError(f"Unsupported transcript segments format: {path}")


def run_offline_diarization(
    *,
    space_id: str,
    audio_path: Path,
    transcript_path: Path,
    analysis_dir: Path,
    diarizer: Any,
    duration: float,
    active_space_ids: Callable[[], Iterable[str]] = lambda: (),
    process_rows: Callable[[], Iterable[tuple[str, str]]] = lambda: (),
    identity_mappings: dict[str, dict[str, Any]] | None = None,
    force_model: bool = False,
    merge_gap_seconds: float = 0.1,
    source_is_current: Callable[[], bool] = lambda: True,
) -> dict[str, Any]:
    """Run/resume diarization without touching recording or transcript sources."""
    if not math.isfinite(merge_gap_seconds) or merge_gap_seconds < 0:
        raise ValueError("merge_gap_seconds must be finite and non-negative")
    if not audio_path.is_file() or not transcript_path.is_file():
        raise FileNotFoundError("Existing analysis audio and transcript segment files are required.")
    timeline_path = analysis_dir / "speaker_timeline.json"
    diarization_path = analysis_dir / "speaker_diarization.json"
    raw_path = analysis_dir / "diarization_raw.json"
    manifest_path = analysis_dir / "speaker_diarization_manifest.json"
    audio_hash = file_sha256(audio_path)
    transcript_hash = file_sha256(transcript_path)
    model_id = getattr(diarizer, "model_id", type(diarizer).__name__)
    fingerprint_source = f"{space_id}:{audio_hash}:{model_id}:v{SCHEMA_VERSION}"
    if force_model:
        fingerprint_source += f":forced:{datetime.now(UTC).isoformat()}"
    diarization_fingerprint = hashlib.sha256(fingerprint_source.encode()).hexdigest()
    mapping_json = json.dumps(
        identity_mappings or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    timeline_fingerprint = hashlib.sha256(
        f"{diarization_fingerprint}:{transcript_hash}:{mapping_json}:merge={merge_gap_seconds:g}".encode()
    ).hexdigest()

    def verify_sources() -> None:
        if (
            not source_is_current()
            or file_sha256(audio_path) != audio_hash
            or file_sha256(transcript_path) != transcript_hash
        ):
            raise RuntimeError("Diarization inputs changed during processing; refusing publication")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing = json.loads(timeline_path.read_text(encoding="utf-8"))
        if (
            manifest.get("timeline_fingerprint") == timeline_fingerprint
            and existing.get("status") == "completed"
        ):
            verify_sources()
            return {"status": "skipped", "reason": "identical_inputs_already_processed", "timeline": existing}
    except (OSError, ValueError, AttributeError):
        manifest = {}
    try:
        saved_diarization = json.loads(diarization_path.read_text(encoding="utf-8"))
        if saved_diarization.get("diarization_fingerprint") == diarization_fingerprint:
            raw_intervals = saved_diarization["intervals"]
        else:
            raw_intervals = None
    except (OSError, ValueError, KeyError, TypeError):
        raw_intervals = None
    if raw_intervals is None:
        assert_safe_to_diarize(active_space_ids=active_space_ids(), process_rows=process_rows())
        if force_model and diarization_path.is_file():
            try:
                previous = json.loads(diarization_path.read_text(encoding="utf-8"))
                previous_fingerprint = str(previous.get("diarization_fingerprint") or "previous")
                archive_path = analysis_dir / "diarization_runs" / f"{previous_fingerprint}.json"
                if not archive_path.exists():
                    archive_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(diarization_path, archive_path)
            except (OSError, ValueError):
                pass
        raw_intervals = diarizer.diarize(audio_path)
        verify_sources()
        raw_payload = {
            "schema_version": SCHEMA_VERSION,
            "space_id": space_id,
            "engine": "pyannote",
            "model": model_id.removeprefix("pyannote/"),
            "model_id": model_id,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "audio_duration": round(duration, 4),
            "audio_duration_seconds": round(duration, 4),
            "device": getattr(diarizer, "actual_device", None),
            "execution_metrics": getattr(diarizer, "execution_metrics", {}),
            "speaker_labels": sorted(
                {str(item.get("speaker")) for item in raw_intervals if item.get("speaker")}
            ),
            "audio_sha256": audio_hash,
            "diarization_fingerprint": diarization_fingerprint,
            "timeline_fingerprint": timeline_fingerprint,
            "segments": raw_intervals,
            "intervals": raw_intervals,
        }
        atomic_json(diarization_path, raw_payload)
        atomic_json(raw_path, raw_payload)
    else:
        # Keep the requested stable artifact name available when resuming older runs.
        try:
            if not raw_path.exists():
                atomic_json(raw_path, json.loads(diarization_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    timeline = build_offline_timeline(
        space_id=space_id,
        duration=duration,
        raw_intervals=raw_intervals,
        transcript_segments=load_transcript_segments(transcript_path),
        identity_mappings=identity_mappings,
        metadata={
            "audio_sha256": audio_hash,
            "transcript_sha256": transcript_hash,
            "model_id": model_id,
            "device": getattr(diarizer, "actual_device", None),
            "execution_metrics": getattr(diarizer, "execution_metrics", {}),
            "diarization_fingerprint": diarization_fingerprint,
            "timeline_fingerprint": timeline_fingerprint,
        },
        merge_gap_seconds=merge_gap_seconds,
        require_mapping_fingerprint=True,
    )
    verify_sources()
    atomic_json(timeline_path, timeline)
    atomic_json(
        manifest_path,
        {
            "diarization_fingerprint": diarization_fingerprint,
            "timeline_fingerprint": timeline_fingerprint,
            "audio_sha256": audio_hash,
            "transcript_sha256": transcript_hash,
            "model_id": model_id,
            "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )
    return {"status": "completed", "timeline": timeline}
