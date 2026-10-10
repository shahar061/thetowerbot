"""Bounded, account-scoped snapshots for Workshop selection history."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from fleet.build_route_store import _write_json_atomic

HISTORY_FILE = "build-route-workshop-history.json"
HISTORY_LIMIT = 100


def valid_plan(record: Any, account_id: str) -> bool:
    return (isinstance(record, dict) and record.get("account_id") == account_id
            and all(isinstance(record.get(key), dict)
                    for key in ("evaluation", "budget", "strategy", "upgrade_names")))


def read_history(worker_dir: Path, account_id: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads((worker_dir / HISTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict) or raw.get("account_id") != account_id:
        return []
    records = raw.get("records")
    if not isinstance(records, list):
        return []
    return [record for record in records[-HISTORY_LIMIT:] if _valid_snapshot(record, account_id)]


def _valid_snapshot(record: Any, account_id: str) -> bool:
    if (not valid_plan(record, account_id) or not isinstance(record.get("id"), str)
            or type(record.get("selected_at")) not in (int, float)
            or not math.isfinite(record["selected_at"])):
        return False
    evaluation = record["evaluation"]
    decision, trace = evaluation.get("decision"), evaluation.get("trace")
    if (evaluation.get("account_id", account_id) != account_id
            or not isinstance(trace, dict)
            or (decision is not None and not isinstance(decision, dict))):
        return False
    if decision is not None and decision.get("account_id", account_id) != account_id:
        return False
    candidates = trace.get("candidates", [])
    return isinstance(candidates, list) and all(isinstance(row, dict) for row in candidates)


def plan_id(record: dict[str, Any]) -> str:
    decision = record.get("evaluation", {}).get("decision") or {}
    trace = record.get("evaluation", {}).get("trace") or {}
    identity = {key: record.get(key) for key in (
        "account_id", "revision", "visit_id", "decision_sequence", "purchase_count", "override")}
    identity.update(upgrade_id=decision.get("upgrade_id"), state=decision.get("state"),
                    price=decision.get("price"), selection=trace.get("selection"),
                    block=trace.get("matched_rule_id"))
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def save_snapshot(worker_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Keep the original graph immutable while allowing the current graph to refresh."""
    worker_dir.mkdir(parents=True, exist_ok=True)
    with (worker_dir / ".workshop-plan-history.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            records = read_history(worker_dir, record["account_id"])
            identity = plan_id(record)
            previous = next((row for row in records if row["id"] == identity), None)
            saved = {**record, "id": identity,
                     "selected_at": previous["selected_at"] if previous else record["written_at"]}
            if previous is None:
                records.append(saved)
                _write_json_atomic(worker_dir / HISTORY_FILE, {
                    "account_id": record["account_id"], "records": records[-HISTORY_LIMIT:]})
            return saved
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
