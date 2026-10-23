"""Whisper transcription engine wrapper.

Supports:
  - faster-whisper (CTranslate2 backend, GPU/CPU, word-level timestamps)
  - fallback to standard openai-whisper if available
  - mock/stub transcription for environments without whisper installed or unit tests
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path

from xsa.blocking import run_blocking
from xsa.config import Settings, get_settings
from xsa.logging_setup import get_logger
from xsa.owned_process import run_owned_process

log = get_logger("TRANSCRIBER")


def _transcribe_in_child(cfg, path: str, language, checkpoint_root=None, identity=None):
    import asyncio

    return asyncio.run(
        WhisperTranscriber(cfg)._transcribe_windows_async(
            Path(path), language, Path(checkpoint_root) if checkpoint_root else None, identity
        )
    )


@dataclass
class WordItem:
    word: str
    start: float  # seconds
    end: float  # seconds
    probability: float = 1.0


@dataclass
class SegmentItem:
    id: int
    start: float  # seconds
    end: float  # seconds
    text: str
    words: list[WordItem] = field(default_factory=list)
    speaker: str | None = None
    language: str | None = None
    confidence: float = 1.0
    no_speech_prob: float = 0.0


@dataclass
class TranscriptionResult:
    language: str
    duration: float
    segments: list[SegmentItem]
    text: str


class WhisperTranscriber:
    """Transcription engine supporting faster-whisper with automatic fallback."""

    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or get_settings()
        self._model = None
        self._engine_type = "unknown"

    def _load_model(self):
        if self._model is not None:
            return self._model

        # 1. Try faster-whisper
        try:
            import ctranslate2
            from faster_whisper import WhisperModel

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            compute_type = self.cfg.whisper_compute_type
            if device == "cpu" and ("float16" in compute_type or "8_float" in compute_type):
                compute_type = "int8"

            log.info(
                "Loading faster-whisper model",
                model=self.cfg.whisper_model,
                device=device,
                compute=compute_type,
            )
            self._model = WhisperModel(
                self.cfg.whisper_model,
                device=device,
                compute_type=compute_type,
                cpu_threads=4,
                num_workers=1,
            )
            self._engine_type = "faster-whisper"
            return self._model
        except ImportError as e:
            log.warning("faster-whisper import failed", error=str(e))

        except Exception as e:
            log.warning("faster-whisper failed to load, trying cpu fallback", error=str(e))
            try:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.cfg.whisper_model, device="cpu", compute_type="int8", cpu_threads=4, num_workers=1
                )
                self._engine_type = "faster-whisper"
                return self._model
            except Exception as ex:
                log.warning("faster-whisper cpu fallback failed", error=str(ex))

        # 2. Try openai-whisper
        try:
            import whisper

            log.info("Loading openai-whisper model", model=self.cfg.whisper_model)
            self._model = whisper.load_model(self.cfg.whisper_model)
            self._engine_type = "openai-whisper"
            return self._model
        except ImportError:
            log.warning("openai-whisper not installed")
        except Exception as e:
            log.warning("openai-whisper load failed", error=str(e))

        self._engine_type = "mock"
        log.warning("No Whisper engine available — transcription will use fallback")
        return None

    def transcribe(
        self,
        audio_path: Path,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Synchronously transcribe audio file."""
        model = self._load_model()
        target_lang = language or self.cfg.whisper_language

        if self._engine_type == "faster-whisper" and model is not None:
            segments_gen, info = model.transcribe(
                str(audio_path),
                language=target_lang,
                word_timestamps=True,
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
            )

            result_segments: list[SegmentItem] = []
            full_text_parts: list[str] = []

            for i, seg in enumerate(segments_gen):
                words = []
                if seg.words:
                    for w in seg.words:
                        words.append(
                            WordItem(
                                word=w.word,
                                start=w.start,
                                end=w.end,
                                probability=getattr(w, "probability", 1.0),
                            )
                        )
                clean_text = seg.text.strip()
                full_text_parts.append(clean_text)
                result_segments.append(
                    SegmentItem(
                        id=i,
                        start=seg.start,
                        end=seg.end,
                        text=clean_text,
                        words=words,
                        language=info.language,
                        confidence=float(getattr(seg, "avg_logprob", 0.0)),
                        no_speech_prob=float(getattr(seg, "no_speech_prob", 0.0)),
                    )
                )

            return TranscriptionResult(
                language=info.language,
                duration=info.duration,
                segments=result_segments,
                text=" ".join(full_text_parts),
            )

        elif self._engine_type == "openai-whisper" and model is not None:
            opts = {"word_timestamps": True}
            if target_lang:
                opts["language"] = target_lang
            res = model.transcribe(str(audio_path), **opts)
            result_segments: list[SegmentItem] = []
            for i, seg in enumerate(res.get("segments", [])):
                words = []
                for w in seg.get("words", []):
                    words.append(
                        WordItem(
                            word=w["word"],
                            start=w["start"],
                            end=w["end"],
                            probability=w.get("probability", 1.0),
                        )
                    )
                result_segments.append(
                    SegmentItem(
                        id=i,
                        start=seg["start"],
                        end=seg["end"],
                        text=seg["text"].strip(),
                        words=words,
                        language=res.get("language"),
                    )
                )
            return TranscriptionResult(
                language=res.get("language", "ar"),
                duration=result_segments[-1].end if result_segments else 0.0,
                segments=result_segments,
                text=res.get("text", "").strip(),
            )

        else:
            # Never report an empty/mock transcript as success: the caller may
            # replace previously valid artifacts and database rows on success.
            raise RuntimeError("No transcription engine is available; install a supported Whisper engine")

    async def transcribe_async(
        self,
        audio_path: Path,
        language: str | None = None,
        *,
        checkpoint_root: Path | None = None,
        source_path: Path | None = None,
        progress_callback=None,
    ) -> TranscriptionResult:
        import asyncio

        from xsa.transcriber.checkpoints import WindowCheckpoints, source_identity

        identity = None
        if checkpoint_root is not None:
            identity = await run_blocking(
                source_identity,
                source_path or audio_path,
                model=self.cfg.whisper_model,
                language=language or self.cfg.whisper_language,
                compute_type=self.cfg.whisper_compute_type,
            )
        task = asyncio.create_task(
            run_owned_process(
                _transcribe_in_child,
                self.cfg,
                str(audio_path),
                language,
                str(checkpoint_root) if checkpoint_root else None,
                identity,
            )
        )
        try:
            if progress_callback is not None and checkpoint_root is not None:
                from xsa.transcriber.audio import get_audio_duration_seconds

                duration = await run_blocking(get_audio_duration_seconds, audio_path)
                total = max(1, ceil((duration or 0) / 600))
                checkpoints = WindowCheckpoints(checkpoint_root, identity, total)
                previous = -1
                while not task.done():
                    completed = await run_blocking(checkpoints.completed_count)
                    if completed != previous:
                        progress_callback(completed, total)
                        previous = completed
                    await asyncio.sleep(2)
            result = await task
            if identity is not None:
                current_identity = await run_blocking(
                    source_identity,
                    source_path or audio_path,
                    model=self.cfg.whisper_model,
                    language=language or self.cfg.whisper_language,
                    compute_type=self.cfg.whisper_compute_type,
                )
                if current_identity != identity:
                    raise RuntimeError(
                        "Recording source changed during transcription; refusing to publish mixed results"
                    )
            return result
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _transcribe_windows_async(
        self,
        audio_path: Path,
        language: str | None = None,
        checkpoint_root: Path | None = None,
        identity: dict | None = None,
    ) -> TranscriptionResult:
        """Run transcription in executor so it does not block the asyncio event loop."""
        from xsa.transcriber.audio import extract_audio_window, get_audio_duration_seconds

        duration = await run_blocking(get_audio_duration_seconds, audio_path)
        if duration is None or (duration <= 1800 and checkpoint_root is None):
            return await run_blocking(self.transcribe, audio_path, language)
        from xsa.transcriber.checkpoints import WindowCheckpoints

        total_windows = ceil(duration / 600)
        checkpoints = (
            WindowCheckpoints(checkpoint_root, identity, total_windows)
            if checkpoint_root is not None and identity is not None
            else None
        )
        # faster-whisper decodes/VADs the entire input into RAM. Multi-day
        # captures exceeded the app cgroup limit in real production. Only one
        # ten-minute PCM window is loaded at a time; source audio is immutable.
        merged = []
        detected_language = language or self.cfg.whisper_language or "unknown"
        with tempfile.TemporaryDirectory(prefix="xsa-transcription-") as folder:
            window = Path(folder) / "window.wav"
            for index in range(total_windows):
                offset = index * 600
                length = min(600, duration - offset)
                if length <= 0:
                    continue
                result = checkpoints.load(index, offset, length) if checkpoints else None
                if result is None:
                    if not await extract_audio_window(audio_path, window, offset, length):
                        raise RuntimeError(f"Failed to extract analysis window at {offset}s")
                    result = await run_blocking(self.transcribe, window, language)
                    if checkpoints:
                        checkpoints.save(index, offset, length, result)
                detected_language = result.language
                for segment in result.segments:
                    segment.id = len(merged)
                    segment.start += offset
                    segment.end += offset
                    for word in segment.words:
                        word.start += offset
                        word.end += offset
                    merged.append(segment)
                window.unlink(missing_ok=True)
        return TranscriptionResult(detected_language, duration, merged, " ".join(s.text for s in merged))
