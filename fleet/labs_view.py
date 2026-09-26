"""Read-only Labs & Gems rows for the fleet page. Never writes facts or spends."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

import db
import lab_catalog
from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_store import BuildRouteStore, RouteUnavailable
from fleet.coin_share import LabCoinJar
from fleet.resource_blocks import LabFacts, automated_list, evaluate_lab_plan
from lab_plan import LabCadence

logger = logging.getLogger(__name__)
RECENT_LIMIT = 8


def _unknown(worker: str, account_id: str | None, reason: str) -> dict[str, Any]:
    return {"worker": worker, "account_id": account_id, "strategy_name": None, "read_at": None,
            "wallet": {"coins": None, "gems": None}, "plan": None, "state": "unknown",
            "reason": reason, "recent": []}


def _menu_wallet(worker_root: Path, worker: str,
                 account_id: str) -> tuple[int | None, int | None, float | None]:
    """The last main-menu wallet this worker published for this account."""
    try:
        raw = json.loads((worker_root / "build-route-resource-facts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, None
    if not isinstance(raw, dict) or raw.get("account_id") != account_id or raw.get("worker") != worker:
        return None, None, None

    def whole(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    observed = raw.get("observed_at")
    read_at = float(observed) if isinstance(observed, (int, float)) and not isinstance(observed, bool) else None
    return whole(raw.get("wallet_coins")), whole(raw.get("wallet_gems")), read_at


def _history(db_path: Path) -> tuple[int | None, list[dict[str, Any]]]:
    try:
        with db.reader(db_path) as connection:
            best = connection.execute(
                "SELECT MAX(wave) FROM runs WHERE tier=1 AND ended_at IS NOT NULL").fetchone()[0]
            rows = connection.execute(
                "SELECT ts, kind, item, category, currency, delta, price, reason, detail FROM ledger "
                "WHERE kind IN ('LAB','CARD_BUY') AND dry_run=0 ORDER BY ts DESC, id DESC LIMIT ?",
                (RECENT_LIMIT,)).fetchall()
    except (OSError, sqlite3.Error):
        return None, []
    recent = []
    for ts, kind, item, category, currency, delta, price, reason, detail in rows:
        try:
            detail_reason = json.loads(detail or "{}").get("reason")
        except (ValueError, AttributeError):
            detail_reason = None
        recent.append({"at": ts, "kind": kind, "item": item, "category": category,
                       "currency": currency, "amount": -delta if delta is not None else price,
                       "reason": reason or detail_reason})
    return best, recent


def _row(root: Path, worker: str, route: RouteDocument, route_error: str | None,
         now: float) -> dict[str, Any]:
    from web.account_catalog import registered_worker

    worker_root = root / "workers" / worker
    registration = registered_worker(worker_root)
    if registration is None or registration.account_id is None:
        return _unknown(worker, None, "Worker is not registered to an account")
    account_id = registration.account_id
    if db.bound_account(registration.db_path) != account_id:
        return _unknown(worker, account_id, "Worker database belongs to another account")
    assignment = route.assignments.get(worker)
    strategy = ("Fleet baseline" if assignment is None
                else assignment.strategy_name if assignment.account_id == account_id
                else "Fleet baseline · assignment inactive")
    coins, gems, read_at = _menu_wallet(worker_root, worker, account_id)
    best, recent = _history(registration.db_path)
    slot1, slot2 = LabCadence(worker_root, account_id).route_observation()
    jar = LabCoinJar(worker_root, account_id, read_only=True).amount(quiet=True)
    plan = evaluate_lab_plan(resolve_route(route, worker, account_id),
                             LabFacts(now, coins, gems, best, slot1, slot2, jar))
    return {"worker": worker, "account_id": account_id, "strategy_name": strategy,
            "read_at": read_at, "wallet": {"coins": coins, "gems": gems}, "plan": asdict(plan),
            "state": "ok", "reason": route_error, "recent": recent}


def labs_snapshot(root: Path, workers: Iterable[str], now: float | None = None) -> dict[str, Any]:
    """One row per worker; a failing worker becomes an unknown row, never an error."""
    moment = time.time() if now is None else now
    root = Path(root)
    route_error: str | None = None
    try:
        route = BuildRouteStore(root).read()
    except RouteUnavailable as exc:
        route = RouteDocument.compatibility()
        route_error = f"Route unavailable ({exc.reason}); showing default rules"
    rows = []
    for worker in workers:
        try:
            rows.append(_row(root, worker, route, route_error, moment))
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            logger.exception("Labs view unavailable for %s", worker)
            rows.append(_unknown(worker, None, "Lab evidence unavailable"))
    return {"workers": rows, "automated": automated_list(), "reference": lab_catalog.reference()}
