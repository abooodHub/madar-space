"""Generate a read-only audit of all Space folders and derived artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xsa.config import get_settings  # noqa: E402
from xsa.db import init_db  # noqa: E402
from xsa.inventory import write_inventory  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=None, help="JSON output path (default: data/space_inventory.json)"
    )
    args = parser.parse_args()
    cfg = get_settings()
    init_db(cfg.db_path)
    result = write_inventory(cfg.data_dir, args.output)
    top = result["spaces"][:5]
    print(
        json.dumps(
            {
                "inventory_path": str(args.output or (cfg.data_dir / "space_inventory.json")),
                "space_folder_count": result["space_folder_count"],
                "flag_counts": result["flag_counts"],
                "top_candidates": [
                    {
                        "space_id": item["space_id"],
                        "title": item["title"],
                        "host": item["expected_host"],
                        "duration_seconds": item["media"]["duration_seconds"],
                        "transcript_segments": item["transcript"]["segment_count"],
                        "speakers_events": item["derived"]["speaker_events"],
                        "participants": item["derived"]["participants"],
                        "recording_health": item["recording_health"]["overall_status"],
                        "score": item["candidate_score"],
                    }
                    for item in top
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
