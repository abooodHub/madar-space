"""Create space-info.html beside existing X Space recordings."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xsa.config import get_settings
from xsa.db import MonitoredAccount, Space, get_session, init_db
from xsa.space_document import write_space_info_html


def _space_id(folder: Path) -> str | None:
    suffix = re.search(r" - ([A-Za-z0-9_-]{10,})$", folder.name)
    candidate = suffix.group(1) if suffix else folder.name
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{10,}", candidate) else None


def _meaningful(value: object) -> bool:
    text = str(value or "").strip()
    return bool(
        text
        and text not in {"عنوان غير مسجل", "مضيف غير مسجل", "مساحة مسجلة"}
        and not re.fullmatch(r"\d{6}", text)
    )


def main() -> int:
    cfg = get_settings()
    init_db(cfg.db_path)
    with get_session() as db:
        spaces = db.query(Space).all()
        records = {
            s.space_id: {
                "space_id": s.space_id,
                "title": s.title,
                "url": s.url,
                "host": s.host_account.username if s.host_account else "",
                "host_display_name": s.host_account.display_name if s.host_account else "",
                "host_avatar_url": s.host_account.avatar_url if s.host_account else "",
                "started_at": (s.recording_started_at or s.started_at or s.detected_at).isoformat(),
                "state": s.state,
            }
            for s in spaces
        }
        accounts = {a.username.lower(): a for a in db.query(MonitoredAccount).all()}

    root = cfg.data_dir / "spaces"
    folders = [*root.glob("*/*/*"), *root.glob("*/*/*/*")]
    created = skipped = errors = 0
    for folder in folders:
        if (
            not folder.is_dir()
            or not any(
                (folder / name).exists()
                for name in ("space.json", "thumbnail.jpg", "screenshots", "recording.mkv")
            )
            and not list(folder.glob("recording_[0-9][0-9][0-9].mkv"))
        ):
            continue
        sid = _space_id(folder)
        if not sid:
            continue
        record = dict(records.get(sid, {}))
        record.setdefault("space_id", sid)
        stored: dict = {}
        meta_path = folder / "space.json"
        if meta_path.is_file():
            try:
                stored = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                stored = {}

        parts = folder.name.split(" - ")
        folder_title = folder_host = ""
        if len(parts) >= 4 and re.fullmatch(r"\d{6}", parts[0]) and parts[-1] == sid:
            folder_title, folder_host = parts[1], parts[2]

        host = str(stored.get("host") or record.get("host") or "")
        account = accounts.get(host.lower().lstrip("@"))
        title = next(
            (
                str(v).strip()
                for v in (stored.get("title"), record.get("title"), folder_title)
                if _meaningful(v)
            ),
            "مساحة بدون عنوان",
        )
        host_display = next(
            (
                str(v).strip()
                for v in (
                    stored.get("host_display_name"),
                    (account.display_name if account else ""),
                    folder_host,
                )
                if _meaningful(v)
            ),
            host or "مضيف غير مسجل",
        )
        image = "thumbnail.jpg" if (folder / "thumbnail.jpg").is_file() else ""
        info = {
            **record,
            **stored,
            "space_id": sid,
            "title": title,
            "url": stored.get("url") or record.get("url") or f"https://x.com/i/spaces/{sid}",
            "host": host or stored.get("host") or "",
            "host_display_name": host_display,
            "host_avatar_url": (
                stored.get("host_avatar_url") or (account.avatar_url if account else "") or ""
            ),
            "icon_path": image,
        }
        if not info.get("started_at_local"):
            info["started_at_local"] = info.get("started_at") or "غير متوفر"
        try:
            output = write_space_info_html(folder, info)
            created += 1
            print(f"CREATED {output}")
        except (OSError, ValueError) as exc:
            errors += 1
            print(f"ERROR {sid}: {exc}", file=sys.stderr)

    print(f"SUMMARY created={created} skipped={skipped} errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
