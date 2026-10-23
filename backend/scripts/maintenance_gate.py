"""Arm/release a safe maintenance gate without stopping active recordings."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xsa.maintenance_gate import (  # noqa: E402
    arm_maintenance,
    lock_path,
    release_maintenance,
)


def get_status(api: str) -> dict:
    try:
        with urlopen(f"{api.rstrip('/')}/maintenance/status", timeout=8) as response:
            status = json.load(response)
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError("Maintenance status API unavailable; no deployment is authorized.") from exc
    required = {
        "maintenance_mode",
        "database_healthy",
        "active_recordings",
        "recording_ffmpeg_count",
        "active_post_processing",
        "unsafe_spaces",
        "running_diarization_jobs",
        "safe_to_deploy",
    }
    if not isinstance(status, dict) or not required.issubset(status):
        raise RuntimeError(
            "App does not implement the maintenance preflight; refusing to claim a safe window."
        )
    return status


def print_json(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    db_path = Path(os.getenv("XSA_DB_PATH", "/state/xsa.sqlite3"))
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=db_path.parent,
        help="Persistent state volume containing xsa.sqlite3 and maintenance.lock",
    )
    parser.add_argument("--api", default="http://127.0.0.1:8765")
    sub = parser.add_subparsers(dest="command", required=True)
    arm = sub.add_parser("arm", help="Pause new Space discovery, then wait for a double idle check")
    arm.add_argument(
        "--timeout-seconds",
        type=int,
        default=0,
        help="Optional timeout; zero waits without automatically releasing the gate",
    )
    sub.add_parser("status", help="Show the gate and current deployment preflight")
    sub.add_parser("release", help="Remove the gate after deployment is complete")
    args = parser.parse_args()

    try:
        if args.command == "status":
            status = get_status(args.api)
            status["lock_file"] = str(lock_path(args.state_dir))
            print_json(status)
            return 0 if status["maintenance_mode"] else 2

        if args.command == "arm":
            # Verify the running app knows the protocol before writing a marker
            # that an older app would silently ignore.
            get_status(args.api)
            lock = arm_maintenance(args.state_dir)
            print(
                f"Maintenance gate armed ({lock['lock_id']}). Active sessions are allowed to finish.",
                flush=True,
            )
            started = time.monotonic()
            consecutive_idle = 0
            while True:
                status = get_status(args.api)
                print_json(status)
                consecutive_idle = consecutive_idle + 1 if status["safe_to_deploy"] else 0
                if consecutive_idle >= 2:
                    print("SAFE_TO_DEPLOY: two idle checks passed; gate remains armed.")
                    return 0
                if args.timeout_seconds and time.monotonic() - started >= args.timeout_seconds:
                    print("Still waiting; gate remains armed and no recording was stopped.")
                    return 3
                time.sleep(3)

        # Requiring the newer status route prevents releasing a stale marker
        # into an app version that would not report maintenance state.
        status = get_status(args.api)
        if not status["maintenance_mode"]:
            print("Maintenance gate is not active; nothing released.")
            return 2
        released = release_maintenance(args.state_dir)
        print(
            "Maintenance gate released; automatic discovery can resume."
            if released
            else "Maintenance gate was already absent."
        )
        return 0
    except FileExistsError:
        print("Maintenance gate already exists; inspect status before continuing.")
        return 2
    except Exception as exc:
        print(f"Maintenance operation stopped safely: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
