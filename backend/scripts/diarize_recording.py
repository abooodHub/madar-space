"""Run resumable offline diarization for a finished Space.

Run inside an environment with the optional diarization extra installed. The
script fails closed if the application API cannot verify idle recording state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xsa.config import get_settings  # noqa: E402
from xsa.diarization import GuardedPyannoteDiarizer, run_offline_diarization  # noqa: E402
from xsa.paths import SpacePaths  # noqa: E402
from xsa.transcriber.audio import get_audio_duration_seconds  # noqa: E402


def live_space_ids(api_base: str) -> list[str]:
    try:
        with urlopen(f"{api_base.rstrip('/')}/live", timeout=5) as response:
            data = json.load(response)
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError("Cannot verify application recording state; diarization refused.") from exc
    active = data.get("active") if isinstance(data, dict) else None
    if not isinstance(active, list):
        raise RuntimeError("Application did not provide an active-session list; diarization refused.")
    return [
        str(item["space_id"])
        for item in active
        if isinstance(item, dict) and item.get("state") == "recording" and item.get("space_id")
    ]


def process_rows() -> list[tuple[str, str]]:
    rows = []
    for process in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
        try:
            info = process.info
            rows.append((str(info["pid"]), " ".join(info.get("cmdline") or []) or info.get("name", "")))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("space_id")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Host/container data root (default: XSA_DATA_DIR or configured data_dir)",
    )
    parser.add_argument(
        "--api",
        default="http://127.0.0.1:8765",
        help="Local XSA API used to verify no live recording sessions",
    )
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument(
        "--identity-mappings",
        type=Path,
        help="Optional JSON mapping keyed by SPEAKER_00 etc.; diarization is reused",
    )
    args = parser.parse_args()

    cfg = get_settings()
    data_dir = args.data_dir or cfg.data_dir
    paths = SpacePaths.resolve_existing(data_dir, args.space_id)
    if paths is None:
        raise SystemExit(f"Space directory not found by ID: {args.space_id}")
    audio_path = paths.audio_analysis
    transcript_path = paths.transcript_segments
    duration = get_audio_duration_seconds(audio_path)
    if duration is None:
        raise SystemExit(f"Existing normalized analysis audio was not found: {audio_path}")
    mappings = {}
    if args.identity_mappings:
        mappings = json.loads(args.identity_mappings.read_text(encoding="utf-8"))
        if not isinstance(mappings, dict):
            raise SystemExit("Identity mappings must be a JSON object keyed by SPEAKER IDs.")
    token = os.environ.get("HF_TOKEN")
    diarizer = GuardedPyannoteDiarizer(
        token=token,
        device=args.device,
        active_space_ids=lambda: live_space_ids(args.api),
        process_rows=process_rows,
    )
    result = run_offline_diarization(
        space_id=args.space_id,
        audio_path=audio_path,
        transcript_path=transcript_path,
        analysis_dir=paths.analysis_dir,
        diarizer=diarizer,
        duration=duration,
        active_space_ids=lambda: live_space_ids(args.api),
        process_rows=process_rows,
        identity_mappings=mappings,
    )
    timeline = result["timeline"]
    print(
        json.dumps(
            {
                "status": result["status"],
                "space_id": args.space_id,
                "speakers": len(timeline["speakers"]),
                "intervals": len(timeline["intervals"]),
                "speech_seconds_unique": timeline["coverage"]["speech_seconds_unique"],
                "silence_or_unassigned_seconds": timeline["coverage"]["silence_or_unassigned_seconds"],
                "overlap_seconds": timeline["coverage"]["overlap_seconds"],
                "output": str(paths.analysis_dir / "speaker_timeline.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
