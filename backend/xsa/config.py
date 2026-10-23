"""Central configuration (environment variables, `XSA_` prefix).

Secrets (X API bearer token, HF token, LLM key) are typed as SecretStr so they
never appear in logs or repr() output.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XSA_", env_file=".env", extra="ignore")

    # storage (inside the container /data is the bind-mounted Windows folder)
    data_dir: Path = Path("/data")
    # SQLite lives on a Linux volume (POSIX locking) - see ARCHITECTURE.md
    db_path: Path = Path("/state/xsa.sqlite3")
    profile_dir: Path = Path("/profile")

    bind_host: str = "127.0.0.1"
    api_port: int = 8765
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # monitoring & privacy
    poll_interval_seconds: int = Field(default=300, ge=1, le=3600)
    max_active_accounts: int = Field(default=10, ge=1, le=59)
    max_graphql_requests_per_cycle: int = Field(default=60, ge=1, le=1000)
    # Spread profile navigation instead of opening a burst of parallel X pages.
    account_poll_spacing_seconds: float = Field(default=3.0, ge=0, le=60)
    account_poll_jitter_seconds: float = Field(default=1.0, ge=0, le=30)
    sidebar_scan_interval_seconds: int = Field(default=300, ge=60, le=3600)
    max_concurrent_recordings: int = Field(default=10, ge=1, le=10)
    # Require repeated explicit Space-end evidence; silence never proves X ended.
    end_confirm_seconds: int = Field(default=2, ge=0, le=5)
    # Sustained audio silence triggers a Space-state recheck; silence never proves X ended.
    silence_suspect_seconds: int = Field(default=180, ge=30, le=7200)
    # A continuous, measured silent input is not usable capture. Stop FFmpeg
    # after this bound, but keep the Space end state unconfirmed unless X
    # independently supplies the normal explicit end evidence.
    silence_capture_stop_seconds: int = Field(default=420, ge=60, le=14400)
    silence_noise_db: float = Field(default=-50.0, ge=-100.0, le=0.0)
    session_start_timeout_seconds: int = Field(default=120, ge=30, le=600)
    capture_recovery_timeout_seconds: int = Field(default=120, ge=15, le=300)
    max_capture_recoveries: int = Field(default=3, ge=0, le=10)
    speaker_poll_interval_ms: int = Field(default=5000, ge=1000, le=60000)
    speaker_start_debounce_ms: int = Field(default=10000, ge=0, le=120000)
    speaker_stop_debounce_ms: int = Field(default=10000, ge=0, le=120000)
    # Anonymous listening preferences
    prefer_anonymous_listening: bool = True
    # Allow authenticated listening when anonymous mode is unavailable for a Space.
    # The identity exposure guard (post-join participant check) is always active and
    # will abort the session if the authenticated profile becomes visible.
    allow_authenticated_fallback: bool = True

    # recording
    record_fps: int = Field(default=25, ge=5, le=60)
    record_size: str = "720x1280"
    video_encoder: Literal["x264", "nvenc"] = "x264"
    audio_bitrate: str = Field(default="160k", pattern=r"^\d+[kKmM]$")
    min_free_disk_gb: float = Field(default=20.0, ge=0)
    master_display: str = ":99"
    pulse_server: str = "unix:/tmp/pulse/native"

    @field_validator("record_size")
    @classmethod
    def validate_record_size(cls, value: str) -> str:
        normalized = value.lower().replace("×", "x")
        try:
            width, height = (int(part) for part in normalized.split("x", 1))
        except (ValueError, TypeError) as exc:
            raise ValueError("record_size must use WIDTHxHEIGHT format") from exc
        if not (320 <= width <= 7680 and 320 <= height <= 7680):
            raise ValueError("record_size dimensions must be between 320 and 7680 pixels")
        return normalized

    # optional X API
    x_api_bearer_token: SecretStr | None = Field(default=None, validation_alias="X_API_BEARER_TOKEN")

    # transcription
    transcribe_engine: Literal["faster-whisper", "whisperx"] = "faster-whisper"
    whisper_model: str = "large-v3"
    whisper_compute_type: str = "int8_float16"
    whisper_language: str | None = None
    # local LLM
    local_llm_base_url: str | None = Field(default=None, validation_alias="LOCAL_LLM_BASE_URL")
    local_llm_model: str | None = Field(default=None, validation_alias="LOCAL_LLM_MODEL")
    local_llm_api_key: SecretStr | None = Field(default=None, validation_alias="LOCAL_LLM_API_KEY")

    @property
    def record_width(self) -> int:
        return int(self.record_size.split("x")[0])

    @property
    def record_height(self) -> int:
        return int(self.record_size.split("x")[1])


@lru_cache
def get_settings() -> Settings:
    return Settings()
