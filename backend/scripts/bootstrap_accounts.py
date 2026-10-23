"""One-time, non-destructive 10-account portfolio selection under maintenance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import func

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xsa.config import get_settings  # noqa: E402
from xsa.db import MonitoredAccount, Space, get_session, init_db  # noqa: E402
from xsa.maintenance_gate import maintenance_active  # noqa: E402


def bootstrap(*, apply: bool, expected_count: int | None = None) -> dict:
    cfg = get_settings()
    if apply and not maintenance_active(cfg.db_path.parent):
        raise RuntimeError("A maintenance gate and validated backup are required")
    init_db(cfg.db_path)
    with get_session() as db:
        accounts = db.query(MonitoredAccount).all()
        if expected_count is not None and len(accounts) != expected_count:
            raise RuntimeError(f"Expected {expected_count} preserved accounts, found {len(accounts)}")
        counts = dict(db.query(Space.host_account_id, func.count(Space.id)).group_by(Space.host_account_id))
        ranked = sorted(
            accounts,
            key=lambda account: (-counts.get(account.id, 0), account.added_at, account.id),
        )
        selected = {account.id for account in ranked[: cfg.max_active_accounts]}
        before = {"total": len(accounts), "active": sum(account.active for account in accounts)}
        result = {
            "before": before,
            "after": {"total": len(accounts), "active": len(selected)},
            "selection_basis": "saved Space count, then original add time and ID",
            "selected": [
                {"username": account.username, "saved_spaces": counts.get(account.id, 0)}
                for account in ranked
                if account.id in selected
            ],
            "priority_counts": {
                "P1": len(selected),
                "P2": sum(counts.get(account.id, 0) > 0 for account in ranked if account.id not in selected),
                "P3": sum(counts.get(account.id, 0) == 0 for account in ranked if account.id not in selected),
            },
            "applied": apply,
        }
        if apply:
            for account in accounts:
                account.active = account.id in selected
                account.priority = (
                    "P1" if account.id in selected else "P2" if counts.get(account.id, 0) else "P3"
                )
            db.commit()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args()
    print(json.dumps(bootstrap(apply=args.apply, expected_count=args.expected_count), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
