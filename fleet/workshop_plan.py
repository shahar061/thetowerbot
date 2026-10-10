"""Read a worker's saved Workshop decision for the Plan graph tab."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fleet.build_route_runtime import WORKSHOP_PLAN_FILE, read_applied_revision
from fleet.workshop_plan_history import HISTORY_LIMIT, read_history, valid_plan
import db
import upgrades


def _selection(record: dict[str, Any]) -> dict[str, Any]:
    evaluation = record["evaluation"]
    decision = evaluation.get("decision") or {}
    trace = evaluation.get("trace") or {}
    uid = decision.get("upgrade_id")
    candidate = next((row for row in trace.get("candidates", [])
                      if isinstance(row, dict) and row.get("upgrade_id") == uid), {})
    return {"id": record["id"], "selected_at": record["selected_at"],
            "upgrade_id": uid, "name": decision.get("item") or "No upgrade selected",
            "score": candidate.get("score"), "weight": candidate.get("weight"),
            "odds": candidate.get("odds"), "selection": trace.get("selection"),
            "outcome": "buy_selected" if decision.get("state") == "buy" else decision.get("state", "unknown"),
            "price": decision.get("price"), "purchased_at": None,
            "reason": decision.get("reason") or trace.get("reason"), "plan": record}


def _history(worker_dir: Path, account_id: str, plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    snapshots = {row["id"]: row for row in read_history(worker_dir, account_id)}
    rows: list[dict[str, Any]] = []
    linked: set[str] = set()
    try:
        with db.reader(worker_dir / "tower_bot.db") as conn:
            if db.connection_account(conn) != account_id:
                return []
            purchases = conn.execute(
                "SELECT id,ts,item,category,price,detail FROM ledger "
                "WHERE kind='WORKSHOP_BUY' AND currency='coins' AND dry_run=0 "
                "AND json_valid(detail) AND json_extract(detail,'$.verdict') IN ('bought','free') "
                "ORDER BY id DESC LIMIT ?", (HISTORY_LIMIT,)).fetchall()
        for purchase in purchases:
            detail = json.loads(purchase["detail"])
            identity = detail.get("workshop_plan_id")
            snapshot = snapshots.get(identity) if isinstance(identity, str) else None
            upgrade = upgrades.resolve(purchase["item"], purchase["category"])
            uid = upgrade.id if upgrade else None
            # A reference must also name the purchased upgrade.
            if snapshot and (snapshot["evaluation"].get("decision") or {}).get("upgrade_id") != uid:
                snapshot = None
            if snapshot:
                row = _selection(snapshot)
                linked.add(identity)
            else:
                row = {"selected_at": purchase["ts"], "upgrade_id": uid,
                       "name": purchase["item"], "score": None, "weight": None,
                       "odds": None, "selection": None, "reason": detail.get("reason"), "plan": None}
            row.update(id=f"purchase:{purchase['id']}", outcome=detail["verdict"],
                       purchased_at=purchase["ts"], price=purchase["price"])
            rows.append(row)
    except (OSError, sqlite3.Error):
        pass  # Saved decision graphs remain usable when purchase history is unavailable.
    current_id = plan.get("id") if plan else None
    rows.extend(_selection(snapshot) for identity, snapshot in snapshots.items()
                if identity not in linked and identity != current_id)
    rows.sort(key=lambda row: row["selected_at"], reverse=True)
    return rows[:HISTORY_LIMIT]


def load_workshop_plan(worker_dir: Path, account_id: str, now: float) -> dict[str, Any]:
    try:
        plan = json.loads((worker_dir / WORKSHOP_PLAN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = None
    # Another account's record (a replaced account) is never shown.
    if not valid_plan(plan, account_id):
        plan = None
    history = _history(worker_dir, account_id, plan)
    if plan is None:
        return {"plan": None, **({"history": history} if history else {})}
    current = read_applied_revision(worker_dir / "build-route-applied.json", account_id)
    written = plan.get("written_at")
    age = max(0.0, now - written) if isinstance(written, (int, float)) else None
    return {"plan": plan, "age_seconds": age, "current_revision": current,
            "history": history,
            "stale": current is None or plan.get("revision") != current}
