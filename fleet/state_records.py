"""Read-only reads of one worker database for the Fleet State page.

Opened mode=ro with a 0.1 s busy timeout, so a worker in the middle of a
write costs this page a tenth of a second and never costs the bot a lock.
Every list is bounded in SQL and every total is summed by SQL, so a ledger of
any length is read in a fixed number of rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import db
import upgrades
import workshop_levels
from fleet.workshop_prices import WorkshopPrices
from fleet.workshop_replay import (WorkshopEvidence, discount_signature, read_actions,
                                   replay_quotes)

logger = logging.getLogger(__name__)

RECENT_LIMIT = 30
RUNS_LIMIT = 5
_BOUGHT = "dry_run=0 AND json_extract(detail, '$.verdict') IN ('bought','free')"


class ForeignDatabase(ValueError):
    """The worker database is bound to another account, or to none."""


@dataclass(frozen=True)
class WorkerRecords:
    revision: dict[str, Any] | None
    workshop_spent: list[tuple[Any, Any, Any]]
    workshop_recent: list[dict[str, Any]]
    card_gems: int
    card_recent: list[dict[str, Any]]
    lab_recent: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    best_waves: dict[int, int]
    run_upgrades: list[dict[str, Any]]
    run_upgrades_scope: str | None
    last_seen: float | None
    balances: dict[str, int | None]
    gems_claimed: int
    workshop_evidence: WorkshopEvidence


def _recent(conn: sqlite3.Connection, kind: str, where: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT ts, item, category, COALESCE(-delta, price) AS price FROM ledger "
        f"WHERE kind=? AND {where} ORDER BY ts DESC, id DESC LIMIT ?",
        (kind, RECENT_LIMIT)).fetchall()
    return [dict(row) for row in rows]


def read_records(db_path: Path, account_id: str, live_run_id: int | None,
                 *, now: float | None = None) -> WorkerRecords:
    """Everything the page needs from one worker DB, in one short read-only connection.

    Raises FileNotFoundError when the DB is missing, ForeignDatabase when it
    belongs to another account, and sqlite3.Error when it cannot be read
    (locked mid-write, corrupt). The caller turns each into an account error.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    uri = f"{path.absolute().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=.1)) as conn:
        conn.row_factory = sqlite3.Row
        if db.connection_account(conn) != account_id:
            raise ForeignDatabase(str(path))
        row = conn.execute("SELECT detail FROM account_revisions ORDER BY id DESC LIMIT 1").fetchone()
        revision = None
        if row is not None:
            try:
                parsed = json.loads(row["detail"])
            except json.JSONDecodeError as exc:
                logger.warning("Ignoring unparseable account_revisions detail for %s: %s",
                               account_id, exc)
            else:
                if isinstance(parsed, dict):
                    revision = parsed
                else:
                    logger.warning("Ignoring non-dict account_revisions detail for %s: %r",
                                   account_id, parsed)
        # A revision stamped with another account predates a replacement.
        if revision is not None and revision.get("account_id") not in (None, account_id):
            revision = None
        if revision is not None:
            for section in ("workshop_stats", "unlocks"):
                facts = revision.get(section)
                if isinstance(facts, list):
                    revision[section] = [fact for fact in facts if isinstance(fact, dict)
                        and (not isinstance(fact.get("scope"), dict)
                             or fact["scope"].get("account_id") == account_id)]
        spent = [tuple(row) for row in conn.execute(
            f"SELECT item, category, SUM(-delta) FROM ledger WHERE kind='WORKSHOP_BUY' AND {_BOUGHT} "
            "GROUP BY item, category")]
        memory = WorkshopPrices(path.parent, account_id)
        observed = {row["id"]: row["observed_at"]
                    for row in workshop_levels.workshop_state((revision or {}).get("workshop_stats"))
                    if row["observed_at"] is not None}
        current_time = time.time() if now is None else now
        actions = read_actions(conn, memory, now=current_time, level_anchors=observed)
        scope = None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='fact_scope' AND type='table'").fetchone():
            record = conn.execute("SELECT detail FROM fact_scope WHERE id=1").fetchone()
            if record is not None:
                try:
                    candidate = json.loads(record[0])
                    if isinstance(candidate, dict) and candidate.get("account_id") == account_id:
                        scope = candidate
                except (ValueError, TypeError):
                    pass
        purchased = {upgrade.id: 1 for item, category, _ in spent
                     if (upgrade := upgrades.resolve(item, category)) is not None}
        evidence = WorkshopEvidence(actions, replay_quotes(memory, actions, purchased,
            signature=discount_signature(revision, scope, now=current_time)), frozenset(memory.entries))
        card_gems = conn.execute(
            "SELECT COALESCE(SUM(-delta), 0) FROM ledger "
            "WHERE kind='CARD_BUY' AND dry_run=0 AND delta IS NOT NULL").fetchone()[0]
        runs = [dict(row) for row in conn.execute(
            "SELECT id, tier, wave, coins, started_at, ended_at, abandoned FROM runs "
            "WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT ?", (RUNS_LIMIT,))]
        best = {tier: wave for tier, wave in conn.execute(
            "SELECT tier, MAX(wave) FROM runs WHERE abandoned=0 AND ended_at IS NOT NULL "
            "AND tier IS NOT NULL AND wave IS NOT NULL GROUP BY tier")}
        scope: str | None = None
        bought: list[dict[str, Any]] = []
        if live_run_id is not None:
            try:
                purchases = db.run_purchases(conn, live_run_id)
            except (json.JSONDecodeError, AttributeError, TypeError) as exc:
                logger.warning("Ignoring unreadable purchase events for run %s: %s",
                               live_run_id, exc)
                purchases = []
            counts = Counter(p["upgrade_id"] for p in purchases if p["upgrade_id"])
            bought = [{"upgrade_id": key, "levels": levels} for key, levels in counts.items()]
            scope = "current"
        elif runs:
            bought = db.run_upgrade_levels(conn, runs[0]["id"]) or []
            scope = "last"
        # Only claims the bot made itself: an UNEXPLAINED gain is a net balance
        # movement, not proof of where the gems came from.
        gems_claimed = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE currency='gems' AND dry_run=0 "
            "AND delta > 0 AND kind IN ('MISSION_CLAIM','MAIL_CLAIM','MILESTONE_CLAIM','GEM_CLAIM')"
        ).fetchone()[0]
        last_seen = conn.execute("SELECT MAX(ts) FROM events").fetchone()[0]
        return WorkerRecords(
            revision=revision, workshop_spent=spent,
            workshop_recent=_recent(conn, "WORKSHOP_BUY", _BOUGHT),
            card_gems=int(card_gems), card_recent=_recent(conn, "CARD_BUY", "dry_run=0"),
            lab_recent=_recent(conn, "LAB", "dry_run=0"), runs=runs, best_waves=best,
            run_upgrades=bought, run_upgrades_scope=scope, last_seen=last_seen,
            balances=db.last_balances(conn), gems_claimed=int(gems_claimed),
            workshop_evidence=evidence)
