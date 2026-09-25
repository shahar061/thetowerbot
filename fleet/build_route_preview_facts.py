"""Read-only, account-bound evidence acquisition for Strategy Studio previews."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import db
from fleet.build_route_eval import EVIDENCE_MAX_AGE_SECONDS, RouteFacts


@dataclass(frozen=True)
class PreviewLaneFacts:
    facts: RouteFacts | None
    source: str
    observed_at: float | None
    reason: str | None
    status: str


@dataclass(frozen=True)
class PreviewFactSnapshot:
    workshop: PreviewLaneFacts
    battle: PreviewLaneFacts
    resources: PreviewLaneFacts


def _lane(facts: RouteFacts | None, source: str, reason: str | None = None,
          *, max_age: float | None = None, now: float | None = None) -> PreviewLaneFacts:
    moment = time.time() if now is None else now
    observed = facts.observed_at if facts is not None else None
    if reason is not None or facts is None:
        return PreviewLaneFacts(facts, source, observed, reason, "unknown")
    age = moment - observed if observed is not None else None
    if observed is None:
        return PreviewLaneFacts(facts, source, None, "Observation time is unknown", "unknown")
    if age is None or age < 0:
        return PreviewLaneFacts(facts, source, observed, "Observation time is invalid", "unknown")
    if max_age is not None and age > max_age:
        return PreviewLaneFacts(replace(facts, now=moment), source, observed,
                                "Observation is stale", "stale")
    return PreviewLaneFacts(replace(facts, now=moment), source, observed, None, "verified")


def _read_snapshot(worker_root: Path, worker: str, account_id: str,
                   filename: str, lane_name: str) -> PreviewLaneFacts:
    try:
        raw = json.loads((worker_root / filename).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _lane(None, "route snapshot", f"Waiting for first verified {lane_name} observation")
    except (OSError, json.JSONDecodeError):
        return _lane(None, "route snapshot", f"{lane_name} observation unavailable")
    if not isinstance(raw, dict) or raw.get("account_id") != account_id or raw.get("worker") != worker:
        return _lane(None, "route snapshot", "Worker fact snapshot belongs to another account")
    try:
        facts = RouteFacts(**raw)
    except (TypeError, ValueError):
        return _lane(None, "route snapshot", f"{lane_name} observation unavailable")
    max_age = EVIDENCE_MAX_AGE_SECONDS if lane_name == "Workshop" else (
        2. if lane_name == "battle" else 600.)
    return _lane(facts, "route snapshot", max_age=max_age)


def _account_revision(db_path: Path, account_id: str) -> dict[str, Any] | None:
    try:
        with db.reader(db_path) as connection:
            row = connection.execute(
                "SELECT detail FROM account_revisions ORDER BY id DESC LIMIT 1").fetchone()
    except (OSError, sqlite3.OperationalError):
        return None
    if row is None:
        return None
    try:
        revision = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(revision, dict) or revision.get("account_id") != account_id:
        return None
    return revision


class _ReadOnlyAccountSnapshot:
    def __init__(self, revision: dict[str, Any]) -> None:
        self._revision = revision

    def snapshot(self) -> dict[str, object]:
        return {"revision": self._revision, "screen_readings": {"readings": []}}


def _reconstructed_workshop(root: Path, worker_root: Path, worker: str,
                            account_id: str, db_path: Path) -> PreviewLaneFacts:
    revision = _account_revision(db_path, account_id)
    if revision is None:
        return _lane(None, "account revision + price memory + ledger",
                     "Waiting for first verified Workshop observation")
    try:
        from fleet.build_route_runtime import BuildRouteRuntime
        from fleet.reroll_progress import RerollProgress

        progress = RerollProgress(worker_root, account_id,
                                  _ReadOnlyAccountSnapshot(revision), read_only=True)  # type: ignore[arg-type]
        progress.route_runtime = BuildRouteRuntime(root, worker, account_id)
        facts = progress.route_facts()
    except (OSError, ValueError, TypeError, KeyError):
        return _lane(None, "account revision + price memory + ledger",
                     "Verified Workshop evidence unavailable")
    if facts.observed_at is None:
        return _lane(None, "account revision + price memory + ledger",
                     "Waiting for first verified Workshop observation")
    return _lane(facts, "account revision + price memory + ledger",
                 max_age=EVIDENCE_MAX_AGE_SECONDS)


def _reconstructed_resources(worker_root: Path, worker: str,
                             account_id: str) -> PreviewLaneFacts:
    from lab_plan import LabCadence

    lab, slot2 = LabCadence(worker_root, account_id).route_observation()
    evidence_times = [record.get("observed_at") for record in (lab, slot2)
                      if record is not None and isinstance(record.get("observed_at"), (int, float))]
    if not evidence_times:
        return _lane(None, "account-bound lab cadence",
                     "Waiting for first verified Lab observation")
    # Use the oldest contributing observation so a mixed-age snapshot never
    # presents old gems/ownership beside a newer research reading as fresh.
    observed_at = min(float(value) for value in evidence_times)
    facts = RouteFacts(
        account_id, worker, "main_menu", observed_at, time.time(),
        wallet_gems=(slot2.get("wallet_gems") if slot2 and type(slot2.get("wallet_gems")) is int else None),
        lab_slot2_owned=(slot2.get("status") == "owned" if slot2 else None),
        game_speed_maxed=(lab.get("kind") == "done" if lab else None),
        lab_decision_kind=(lab.get("kind") if lab and isinstance(lab.get("kind"), str) else None),
        lab_price=(lab.get("price") if lab and type(lab.get("price")) is int else None),
    )
    return _lane(facts, "account-bound lab cadence", max_age=600.)


def load_preview_facts(root: Path, worker_root: Path, worker: str,
                       account_id: str, db_path: Path) -> PreviewFactSnapshot:
    """Load each preview lane independently after registration/account checks."""
    from web.account_catalog import registered_worker

    registration = registered_worker(worker_root)
    if (registration is None or registration.account_id != account_id
            or registration.db_path != db_path or db.bound_account(db_path) != account_id):
        reason = "Worker database belongs to another account"
        unknown = _lane(None, "account binding", reason)
        return PreviewFactSnapshot(unknown, unknown, unknown)

    workshop = _read_snapshot(worker_root, worker, account_id,
                              "build-route-facts.json", "Workshop")
    if workshop.facts is None:
        workshop = _reconstructed_workshop(root, worker_root, worker, account_id, db_path)
    battle = _read_snapshot(worker_root, worker, account_id,
                            "build-route-battle-facts.json", "battle")
    resources = _read_snapshot(worker_root, worker, account_id,
                               "build-route-resource-facts.json", "resource")
    if resources.facts is None:
        resources = _reconstructed_resources(worker_root, worker, account_id)
    return PreviewFactSnapshot(workshop, battle, resources)
