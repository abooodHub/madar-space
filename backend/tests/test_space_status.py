"""The library must describe artifacts and jobs independently of the DB lifecycle."""

import pytest

from xsa.api.space_status import describe_space_status


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {},
            {
                "capture": "saved",
                "transcription": "not_started",
                "analysis": "not_started",
                "next_action": "inspect_transcription",
            },
        ),
        (
            {"state": "analyzed", "transcript_status": "NO_SPEECH"},
            {"transcription": "no_speech", "next_action": "review_audio"},
        ),
        ({"recording_quality": "PARTIAL"}, {"capture": "partial", "next_action": "review_partial"}),
        (
            {"processing_state": "queued"},
            {"transcription": "queued", "analysis": "queued", "next_action": "wait"},
        ),
        (
            {"processing_state": "running"},
            {"transcription": "processing", "analysis": "processing", "next_action": "wait"},
        ),
        (
            {"processing_state": "failed"},
            {"transcription": "failed", "analysis": "failed", "next_action": "inspect_failure"},
        ),
        (
            {"transcript_status": "DONE", "has_summary": True},
            {"transcription": "ready", "analysis": "summary_ready", "next_action": "view_summary"},
        ),
        (
            {"state": "error", "has_video": False},
            {"capture": "failed", "transcription": "unavailable", "next_action": "inspect_capture"},
        ),
    ],
)
def test_space_status_follows_artifacts_and_job(changes, expected):
    inputs = {
        "state": "ended",
        "has_video": True,
        "recording_quality": "NOT_ASSESSED",
        "transcript_status": "NOT_TRANSCRIBED",
        "has_summary": False,
        "processing_state": None,
    }
    inputs.update(changes)
    actual = describe_space_status(**inputs)
    for key, value in expected.items():
        assert actual[key] == value
