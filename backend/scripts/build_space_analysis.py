"""Audit the archive, select one safe completed Space, and build its reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xsa.analysis_platform import build_space_analysis  # noqa: E402
from xsa.api.space_page import SPACE_ANALYSIS_HTML  # noqa: E402
from xsa.config import get_settings  # noqa: E402
from xsa.db import Space, get_session, init_db  # noqa: E402
from xsa.inventory import write_inventory  # noqa: E402
from xsa.paths import SpacePaths  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--space-id", help="Existing Space ID; defaults to the best finalized transcript-bearing Space"
    )
    parser.add_argument(
        "--skip-inventory", action="store_true", help="Use existing data/space_inventory.json"
    )
    args = parser.parse_args()
    cfg = get_settings()
    init_db(cfg.db_path)
    inventory_path = cfg.data_dir / "space_inventory.json"
    if args.skip_inventory and inventory_path.is_file():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    else:
        inventory = write_inventory(cfg.data_dir)
    space_id = args.space_id or inventory.get("recommended_validation_space_id")
    if not space_id:
        raise SystemExit("No finalized recording with a transcript is available for the first analysis run")
    selected = next((row for row in inventory["spaces"] if row.get("space_id") == space_id), None)
    if not selected:
        raise SystemExit(f"Space {space_id} has no discovered data folder")
    if selected.get("recording_health", {}).get("overall_status") == "ACTIVE":
        raise SystemExit("Refusing to analyze an active recording")
    paths = SpacePaths.find_space(cfg.data_dir, space_id, title=selected.get("title"))
    with get_session() as db:
        row = db.query(Space).filter_by(space_id=space_id).first()
        db_space = {
            "space_id": space_id,
            "title": selected.get("title") or (row.title if row else None),
            "host": selected.get("expected_host"),
            "url": selected.get("db", {}).get("url") or (row.url if row else None),
            "state": row.state if row else None,
            "folder": selected.get("folder_relative"),
        }
    result = build_space_analysis(paths, space=db_space, inventory_record=selected)
    embedded = (
        json.dumps(result, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    preview = SPACE_ANALYSIS_HTML.replace("__SPACE_ID__", space_id).replace(
        "const API='';", "const API='http://127.0.0.1:8765';"
    )
    preview = preview.replace("<script>", f"<script>window.SPACE_ANALYSIS_DATA={embedded};", 1)
    preview_path = paths.analysis_dir / "space_analysis_preview.html"
    preview_path.write_text(preview, encoding="utf-8")
    print(
        json.dumps(
            {
                "inventory_path": str(inventory_path),
                "total_space_folders": inventory.get("space_folder_count"),
                "database_space_rows": inventory.get("db_space_count"),
                "flag_counts": inventory.get("flag_counts"),
                "top_candidates": [
                    {
                        "space_id": item["space_id"],
                        "title": item["title"],
                        "host": item["expected_host"],
                        "duration_seconds": item["media"]["duration_seconds"],
                        "transcript_segments": item["transcript"]["segment_count"],
                        "participants": item["derived"]["participants"],
                        "score": item["candidate_score"],
                        "health": item["recording_health"]["overall_status"],
                    }
                    for item in inventory["spaces"][:5]
                ],
                "selected": {
                    "space_id": space_id,
                    "folder": str(paths.root),
                    "preview_html": str(preview_path),
                    "duration_seconds": result["recording_health"]["duration_seconds"],
                    "health": result["recording_health"]["overall_status"],
                    "timeline_entries": len(result["timeline"]),
                    "transcript_segments": len(result["transcript_segments"]),
                    "topics": len(result["topics"]),
                    "questions": len(result["questions"]),
                    "mentions": len(result["mentions"]),
                    "conflict_markers": len(result["conflicts"]),
                    "acoustic_speakers": result["speakers"]["acoustic_diarization"]["speaker_count"],
                    "x_observed_participants": len(result["speakers"]["x_observed_participants"]),
                },
                "hf_token_configured": bool(__import__("os").environ.get("HF_TOKEN")),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
