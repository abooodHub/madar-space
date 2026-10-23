"""User-facing states derived from verified Space artifacts and the latest job."""

WAITING_JOB_STATES = frozenset({"queued", "retrying", "waiting_for_resources", "waiting_for_configuration"})


def describe_space_status(
    *,
    state: str,
    has_video: bool,
    recording_quality: str,
    transcript_status: str,
    has_summary: bool,
    processing_state: str | None,
) -> dict[str, str]:
    """Keep capture, quality, transcription and summary readiness independent."""
    if state == "recording":
        capture = "recording"
    elif state == "finalizing":
        capture = "finalizing"
    elif has_video:
        capture = "partial" if recording_quality == "PARTIAL" else "saved"
    elif state in {"error", "rejected", "blocked", "interrupted", "development_interrupted"}:
        capture = "failed"
    else:
        capture = "unavailable"

    if transcript_status == "DONE":
        transcription = "ready"
    elif transcript_status == "NO_SPEECH":
        transcription = "no_speech"
    elif transcript_status == "UNREADABLE":
        transcription = "unreadable"
    elif processing_state == "running":
        transcription = "processing"
    elif processing_state in WAITING_JOB_STATES:
        transcription = "queued"
    elif processing_state == "failed":
        transcription = "failed"
    elif not has_video:
        transcription = "unavailable"
    else:
        transcription = "not_started"

    if has_summary:
        analysis = "summary_ready"
    elif processing_state == "running":
        analysis = "processing"
    elif processing_state in WAITING_JOB_STATES:
        analysis = "queued"
    elif processing_state == "failed":
        analysis = "failed"
    else:
        analysis = "not_started"

    if processing_state == "failed" or transcription == "unreadable":
        next_action = "inspect_failure"
    elif capture == "failed":
        next_action = "inspect_capture"
    elif transcription == "no_speech" or recording_quality in {
        "SILENT_CAPTURE_STOPPED",
        "AUDIO_MONITORING_UNAVAILABLE",
        "NO_SPEECH",
    }:
        next_action = "review_audio"
    elif capture == "partial":
        next_action = "review_partial"
    elif capture in {"recording", "finalizing"} or processing_state in {"running", *WAITING_JOB_STATES}:
        next_action = "wait"
    elif transcription == "not_started":
        next_action = "inspect_transcription"
    elif analysis == "summary_ready":
        next_action = "view_summary"
    else:
        next_action = "inspect_recording"

    return {
        "capture": capture,
        "quality": recording_quality.lower(),
        "transcription": transcription,
        "analysis": analysis,
        "next_action": next_action,
    }
