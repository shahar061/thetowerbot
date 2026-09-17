"""Bind pure reroll decisions to one worker's verified account evidence."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import db
import ocr
import upgrades
from account_state import AccountState
from fleet.reroll_journal import RerollJournal
from fleet.reroll_lifetime import read_lifetime
from fleet.reroll_planner import RerollDecision, RerollFacts, choose_next
from policy import AutopilotPolicy, UpgradeRule, preset_rules
from strategy import Shopping, ShoppingRule


class RerollProgress:
    """Read only this registered account and publish a bounded next action."""

    def __init__(self, worker_root: Path, account_id: str, account_state: AccountState) -> None:
        self.root = Path(worker_root)
        self.account_id = account_id
        self.account_state = account_state
        self._observed: tuple[str, int | None, int | None, float] | None = None
        self._last_state: tuple[str, str | None, str] | None = None
        self._last_decision: RerollDecision | None = None
        self._last_published_at = 0.0
        self._spend_fraction: float | None = None
        self._battle_stage: str | None = None
        self._last_stats_attempt = 0.0

    def lifetime_record(self) -> dict[str, object] | None:
        return read_lifetime(self.root, self.account_id)

    def stats_due(self, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        record = self.lifetime_record()
        if self._last_stats_attempt == 0:
            return True
        return (record is None or float(record["observed_at"]) < self._last_stats_attempt) and (
            moment - self._last_stats_attempt >= 300)

    def note_stats_requested(self, now: float | None = None) -> None:
        self._last_stats_attempt = time.time() if now is None else now

    def _history(self) -> tuple[int | None, dict[str, int]]:
        path = self.root / "tower_bot.db"
        if not path.is_file() or db.bound_account(path) != self.account_id:
            raise ValueError("reroll account database binding changed")
        purchases: dict[str, int] = {}
        with db.reader(path) as connection:
            best = connection.execute(
                "SELECT MAX(wave) FROM runs WHERE tier=1 AND ended_at IS NOT NULL"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT item, category, detail FROM ledger WHERE kind='WORKSHOP_BUY' "
                "AND dry_run=0"
            ).fetchall()
        for item, category, detail in rows:
            try:
                verdict = json.loads(detail or "{}").get("verdict")
            except (TypeError, ValueError):
                continue
            if verdict not in {"bought", "free"}:
                continue
            upgrade = upgrades.resolve(item, category)
            if upgrade is not None:
                purchases[upgrade.id] = purchases.get(upgrade.id, 0) + 1
        return best, purchases

    def _account_readings(self) -> tuple[dict[str, float], int | None]:
        snapshot = self.account_state.snapshot()
        revision = snapshot.get("revision") or {}
        values = {}
        for fact in revision.get("workshop_stats") or []:
            concept = fact.get("concept_id")
            value = fact.get("value")
            if (fact.get("status") == "verified" and isinstance(concept, str)
                    and concept.startswith("stats.")
                    and isinstance(value, (int, float)) and not isinstance(value, bool)):
                values[concept.removeprefix("stats.")] = float(value)
        lifetime = None
        readings = snapshot.get("screen_readings", {}).get("readings", [])
        summaries = [row for row in readings if row.get("screen_id") == "account.stats.summary"]
        if summaries:
            latest = max(summaries, key=lambda row: row.get("observed_at") or 0)
            coins = next((field for field in latest.get("fields", [])
                          if field.get("key") == "coins_earned"), None)
            if (coins is not None and coins.get("status") == "observed"
                    and isinstance(coins.get("raw_value"), str)):
                lifetime = ocr.parse_number(coins["raw_value"])
                if lifetime is not None and lifetime >= 0:
                    stored = self.lifetime_record()
                    observed_at = latest.get("observed_at")
                    if (isinstance(observed_at, (int, float))
                            and (stored is None or observed_at > stored["observed_at"])):
                        self.root.mkdir(parents=True, exist_ok=True)
                        with db.reader(self.root / "tower_bot.db") as connection:
                            baseline_run_id = connection.execute(
                                "SELECT COALESCE(MAX(id),0) FROM runs WHERE ended_at IS NOT NULL"
                            ).fetchone()[0]
                        path = self.root / "reroll-lifetime.json"
                        temporary = path.with_name(f".reroll-lifetime.{uuid4().hex}.tmp")
                        try:
                            with temporary.open("x", encoding="utf-8") as output:
                                json.dump({"account_id": self.account_id,
                                           "lifetime_coins": lifetime,
                                           "observed_at": observed_at,
                                           "baseline_run_id": baseline_run_id}, output)
                                output.write("\n")
                                output.flush()
                                os.fsync(output.fileno())
                            os.replace(temporary, path)
                        finally:
                            temporary.unlink(missing_ok=True)
        record = self.lifetime_record()
        if record is not None:
            lifetime = int(record["lifetime_coins"])
        return values, lifetime

    def decision(self) -> RerollDecision:
        best, purchases = self._history()
        values, lifetime = self._account_readings()
        target = choose_next(RerollFacts(self.account_id, best, purchases, values))
        self._battle_stage = target.stage
        wallet = price = None
        if (self._observed is not None and self._observed[0] == target.upgrade_id
                and time.time() - self._observed[3] <= 30):
            wallet, price = self._observed[1:3]
        return choose_next(RerollFacts(
            self.account_id, best, purchases, values, wallet, lifetime,
            {target.upgrade_id: price} if target.upgrade_id and price is not None else {},
            self._spend_fraction,
        ))

    def shopping_policy(self, base: Shopping) -> Shopping:
        # The reroll planner selects one item; a visit-wide percentage cap
        # otherwise rejects an affordable unlock after the planner selects it.
        self._spend_fraction = None
        plan = self.decision()
        self._publish(plan)
        if plan.item is None or plan.category is None:
            return replace(base, enabled=False, workshop=())
        return replace(base, workshop=(ShoppingRule(plan.item, plan.category),),
                       allow_unlocks=True, coin_budget_pct=None)

    def battle_policy(self, base: AutopilotPolicy) -> AutopilotPolicy:
        if self._battle_stage is None:
            best, _ = self._history()
            self._battle_stage = "stones" if best is not None and best >= 60 else (
                "turtle" if best is not None and best >= 20 else "opening")
        if self._battle_stage == "opening":
            candidates = (
                UpgradeRule("cash_per_wave", target=10),
                UpgradeRule("coins_per_wave", target=10),
                UpgradeRule("damage"), UpgradeRule("attack_speed"),
                UpgradeRule("coins_per_kill_bonus"),
            )
        else:
            candidates = preset_rules("turtle")
        _, purchases = self._history()
        confirmed_unlocks = {entry.id for entry in upgrades.CATALOG
                             if entry.unlock and purchases.get(entry.id, 0) > 0}
        utility_open = any(upgrades.by_id(unlock_id).category == "UTILITY"
                           for unlock_id in confirmed_unlocks)
        locked_children = {child: entry.id for entry in upgrades.CATALOG
                           if entry.unlock for child in entry.unlocks}
        available = tuple(rule for rule in candidates
                          if (utility_open or upgrades.by_id(rule.upgrade_id).category != "UTILITY")
                          and locked_children.get(rule.upgrade_id) in
                          (None, *confirmed_unlocks))
        return replace(base, preset="manual", purpose="milestone", rules=available)

    def observe_price(self, upgrade_id: str, wallet: int | None,
                      price: int | None) -> None:
        current = self.decision()
        if upgrade_id != current.upgrade_id:
            return
        self._observed = (upgrade_id, wallet, price, time.time())
        self._publish(self.decision())

    def _publish(self, decision: RerollDecision) -> None:
        now = time.time()
        if decision == self._last_decision and now - self._last_published_at < 60:
            return
        self._last_decision = decision
        self._last_published_at = now
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "reroll-plan.json"
        payload = {**asdict(decision), "observed_at": now}
        temporary = path.with_name(f".reroll-plan.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        state = (decision.stage, decision.upgrade_id, decision.state)
        if state != self._last_state:
            self._last_state = state
            RerollJournal(self.root.parent.parent).append(
                instance=self.root.name, level="info", kind="reroll_plan",
                message=f"{decision.goal}: {decision.item or 'operator review'} · {decision.reason}")
