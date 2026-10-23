"""Transcription and speaker alignment package."""

from xsa.transcriber.aligner import align_segments_with_speakers, build_speaker_intervals
from xsa.transcriber.audio import extract_analysis_audio
from xsa.transcriber.engine import WhisperTranscriber
from xsa.transcriber.worker import process_space_transcription

__all__ = [
    "WhisperTranscriber",
    "extract_analysis_audio",
    "align_segments_with_speakers",
    "build_speaker_intervals",
    "process_space_transcription",
]
