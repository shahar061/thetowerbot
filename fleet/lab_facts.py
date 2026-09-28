"""Lab facts a worker can read from disk: slots, levels, best waves and coin income.

Shared by the Labs & Gems page and the worker's Workshop policy so both plan the
same way. Anything unreadable stays None/empty - never zero.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import db
from account_state import completed_lab_level
from fleet.build_route_preview_facts import read_lab_slots
from fleet.coin_share import LabCoinJar, jit_hold
from fleet.reroll_lifetime import read_lifetime
from fleet.resource_blocks import LabFacts, evaluate_lab_plan
from lab_plan import LabCadence


def best_waves(db_path: Path) -> dict[int, int]:
    try:
        with db.reader(db_path) as connection:
            rows = connection.execute(
                "SELECT tier, MAX(wave) FROM runs WHERE ended_at IS NOT NULL AND tier IS NOT NULL "
                "AND wave IS NOT NULL GROUP BY tier").fetchall()
    except (OSError, sqlite3.Error):
        return {}
    return {int(tier): int(wave) for tier, wave in rows}


def coins_per_hour(worker_root: Path, account_id: str) -> float | None:
    record = read_lifetime(worker_root, account_id)
    rate = record.get("recent_coins_per_hour") if record else None
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
        return None
    return float(rate)


def _completed_levels(db_path: Path, account_id: str) -> dict[str, int] | None:
    """Known lab levels from the persisted account revision.

    The same ``lab_levels`` section the Fleet State page (`state_view.build_labs`)
    and the Workshop discount signature (`reroll_progress._discount_signature`)
    read, via the same ``account_revisions`` table other read-only readers use
    (see `fleet.build_route_preview_facts._account_revision`).
    """
    try:
        with db.reader(db_path) as connection:
            row = connection.execute(
                "SELECT detail FROM account_revisions ORDER BY id DESC LIMIT 1").fetchone()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    try:
        revision = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(revision, dict) or revision.get("account_id") not in (None, account_id):
        return None
    known: dict[str, int] = {}
    for fact in revision.get("lab_levels") or ():
        if not isinstance(fact, dict):
            continue
        concept_id = fact.get("concept_id")
        if not isinstance(concept_id, str):
            continue
        level = completed_lab_level(fact.get("status"), fact.get("value"))
        if level is not None:
            known[concept_id] = level
    return known or None


def persisted_lab_facts(worker_root: Path, account_id: str, *, now: float, coins: int | None,
                        gems: int | None, db_path: Path) -> LabFacts:
    slot1, slot2 = LabCadence(worker_root, account_id).route_observation()
    waves = best_waves(db_path)
    return LabFacts(now, coins, gems, waves.get(1), slot1, slot2,
                    LabCoinJar(worker_root, account_id, read_only=True).amount(quiet=True),
                    slots=read_lab_slots(worker_root, account_id), available_coins=coins,
                    account_id=account_id, best_waves=waves or None,
                    coins_per_hour=coins_per_hour(worker_root, account_id),
                    completed_levels=_completed_levels(db_path, account_id))


def just_in_time_hold(route: Any, worker_root: Path, account_id: str, *, wallet: int | None,
                      db_path: Path, now: float) -> tuple[int, bool, str | None]:
    """`coin_share.jit_hold` for `route`'s own saving plan over this worker's persisted lab facts."""
    plan = evaluate_lab_plan(route, persisted_lab_facts(worker_root, account_id, now=now, coins=wallet,
                                                        gems=None, db_path=db_path))
    return jit_hold(plan.saving, wallet)
