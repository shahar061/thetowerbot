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
from fleet.account_metrics import game_started_date
from fleet.reroll_lifetime import read_lifetime
from fleet.reroll_planner import (FILLER_SHARE, UTILITY_CEILING_COINS,
                                  UTILITY_TARGET_COINS, RerollDecision,
                                  RerollFacts, choose_next, project_next)
from fleet.reroll_variants import read_variant
from policy import AutopilotPolicy, UpgradeRule
from strategy import Shopping, ShoppingRule

# A skipped trip rests on an estimate - the last balance read in the workshop
# plus the coins each run since reported. Coins from claims or ads are
# missing from it and spends outside the workshop are not subtracted, so the
# estimate is re-anchored by a real visit at least this often.
RECHECK_EVERY_N_RUNS = 10
PRICE_MEMORY_SECONDS = 600


class RerollProgress:
    """Read only this registered account and publish a bounded next action."""

    def __init__(self, worker_root: Path, account_id: str, account_state: AccountState) -> None:
        self.root = Path(worker_root)
        self.account_id = account_id
        self.account_state = account_state
        self._observed: dict[str, tuple[int | None, int | None, float, int]] = {}
        self._last_state: tuple[str, str | None, str] | None = None
        self._last_decision: RerollDecision | None = None
        self._last_published_at = 0.0
        self._spend_fraction: float | None = None
        self._battle_stage: str | None = None
        self._last_stats_attempt = 0.0
        self._last_skip_note: str | None = None

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
                "SELECT kind, item, category, reason, detail FROM ledger "
                "WHERE kind IN ('WORKSHOP_BUY', 'BUY_SKIPPED') AND dry_run=0"
            ).fetchall()
        for kind, item, category, reason, detail in rows:
            try:
                evidence = json.loads(detail or "{}")
            except (TypeError, ValueError):
                continue
            bought = kind == "WORKSHOP_BUY" and evidence.get("verdict") in {"bought", "free"}
            observed_unlock = (kind == "BUY_SKIPPED" and reason == "already_unlocked"
                               and evidence.get("detail") == "the rows it grants are on the tab")
            if not bought and not observed_unlock:
                continue
            upgrade = upgrades.resolve(item, category)
            if upgrade is not None and (bought or upgrade.unlock):
                if observed_unlock and purchases.get(upgrade.id, 0):
                    continue
                purchases[upgrade.id] = purchases.get(upgrade.id, 0) + 1
        return best, purchases

    def _utility_spent(self) -> int | None:
        """Sum proven utility debits, retaining uncertainty as unknown."""
        path = self.root / "tower_bot.db"
        if not path.is_file() or db.bound_account(path) != self.account_id:
            raise ValueError("reroll account database binding changed")
        with db.reader(path) as connection:
            rows = connection.execute(
                "SELECT delta, detail FROM ledger WHERE kind='WORKSHOP_BUY' "
                "AND category='UTILITY' AND currency='coins' AND dry_run=0"
            ).fetchall()
        spent = 0
        for delta, detail in rows:
            try:
                verdict = json.loads(detail or "{}").get("verdict")
            except (TypeError, ValueError, AttributeError):
                continue
            if verdict not in {"bought", "free"}:
                continue
            if delta is None or delta > 0:
                return None
            spent -= delta
        return spent

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
                    fields = {field.get("key"): field for field in latest.get("fields", [])}
                    started_field = fields.get("game_started") or {}
                    rate_field = fields.get("recent_coins_per_hour") or {}
                    started = (game_started_date(started_field["raw_value"])
                               if started_field.get("status") == "observed"
                               and isinstance(started_field.get("raw_value"), str) else None)
                    hourly = (ocr.parse_number(rate_field["raw_value"])
                              if rate_field.get("status") == "observed"
                              and isinstance(rate_field.get("raw_value"), str) else None)
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
                                           "baseline_run_id": baseline_run_id,
                                           **({"game_started": started} if started else {}),
                                           **({"recent_coins_per_hour": hourly}
                                              if hourly is not None and hourly >= 0 else {})}, output)
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
        if not self._observed:
            try:
                record = json.loads((self.root / "workshop-target.json").read_text(
                    encoding="utf-8"))
            except (OSError, ValueError):
                record = None
            if isinstance(record, dict) and record.get("account_id") == self.account_id:
                upgrade_id = record.get("upgrade_id")
                observed_at = record.get("observed_at")
                if (isinstance(upgrade_id, str) and upgrades.by_id(upgrade_id) is not None
                        and isinstance(observed_at, (int, float))
                        and 0 <= time.time() - observed_at <= PRICE_MEMORY_SECONDS
                        and record.get("purchase_count") == purchases.get(upgrade_id, 0)
                        and isinstance(record.get("wallet"), int)
                        and isinstance(record.get("price"), int)):
                    self._observed[upgrade_id] = (
                        record["wallet"], record["price"], observed_at,
                        record["purchase_count"])
        observed = {upgrade_id: entry for upgrade_id, entry in self._observed.items()
                    if time.time() - entry[2] <= PRICE_MEMORY_SECONDS
                    and entry[3] == purchases.get(upgrade_id, 0)}
        latest = max(observed.values(), key=lambda entry: entry[2], default=None)
        wallet = latest[0] if latest is not None else None
        prices = {upgrade_id: entry[1] for upgrade_id, entry in observed.items()
                  if entry[1] is not None}
        result = choose_next(RerollFacts(
            self.account_id, best, purchases, values, wallet, lifetime,
            prices, self._spend_fraction,
            variant=read_variant(self.root),
            utility_spent_coins=self._utility_spent(),
        ))
        self._battle_stage = result.stage
        return result

    def shopping_policy(self, base: Shopping) -> Shopping:
        # The reroll planner selects one item; a visit-wide percentage cap
        # otherwise rejects an affordable unlock after the planner selects it.
        self._spend_fraction = None
        plan = self.decision()
        self._publish(plan)
        if plan.item is None or plan.category is None:
            return replace(base, enabled=False, workshop=())
        if not self.workshop_worthwhile():
            return replace(base, enabled=False)
        budget = base.coin_budget
        if plan.filler:
            assert plan.wallet_coins is not None
            ceiling = int(plan.wallet_coins * FILLER_SHARE)
            budget = ceiling if budget is None else min(budget, ceiling)
        else:
            spent = self._utility_spent()
            if (plan.category == "UTILITY" and spent is not None
                    and spent < UTILITY_TARGET_COINS):
                ceiling = UTILITY_CEILING_COINS - spent
                budget = ceiling if budget is None else min(budget, ceiling)
        return replace(base, workshop=(ShoppingRule(plan.item, plan.category),),
                       allow_unlocks=True, coin_budget=budget, coin_budget_pct=None)

    def battle_policy(self, base: AutopilotPolicy) -> AutopilotPolicy:
        if self._battle_stage is None:
            best, _ = self._history()
            self._battle_stage = "stones" if best is not None and best >= 60 else (
                "turtle" if best is not None and best >= 20 else "opening")
        candidates = (
            UpgradeRule("cash_per_wave", target=10),
            UpgradeRule("coins_per_kill_bonus", target=1.25),
            UpgradeRule("cash_bonus", target=1.25),
            UpgradeRule("defense_absolute"), UpgradeRule("thorns", target=51),
            UpgradeRule("coins_per_wave", target=10),
            UpgradeRule("damage"), UpgradeRule("attack_speed"),
        ) if self._battle_stage == "opening" else (
            UpgradeRule("cash_per_wave", target=10),
            UpgradeRule("coins_per_kill_bonus", target=1.25),
            UpgradeRule("cash_bonus", target=1.25),
            UpgradeRule("defense_absolute"), UpgradeRule("thorns", target=51),
            UpgradeRule("health"),
            UpgradeRule("coins_per_wave", target=10),
            UpgradeRule("damage"), UpgradeRule("attack_speed"),
        )
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
        _, purchases = self._history()
        self._observed[upgrade_id] = (wallet, price, time.time(),
                                      purchases.get(upgrade_id, 0))
        if not current.filler:
            self._remember_target(upgrade_id, wallet, price)
        self._publish(self.decision())

    def workshop_worthwhile(self) -> bool:
        """Could the planned item be affordable yet, by the last visit's numbers?

        Every visit ends with the planned row's price and the balance beside
        it, both saved. Until the coins earned by the runs since cover the
        gap, a trip would only read the same "unaffordable" again. Anything
        that makes the saved numbers doubtful - none saved, another account,
        a different planned item, too many runs since - says visit.
        """
        path = self.root / "workshop-target.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if (not isinstance(record, dict) or record.get("account_id") != self.account_id
                or record.get("upgrade_id") != self.decision().upgrade_id):
            return True
        with db.reader(self.root / "tower_bot.db") as connection:
            runs, earned = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(coins),0) FROM runs "
                "WHERE id > ? AND ended_at IS NOT NULL",
                (record["baseline_run_id"],)).fetchone()
        estimate = record["wallet"] + earned
        worthwhile = runs >= RECHECK_EVERY_N_RUNS or estimate >= record["price"]
        note = None if worthwhile else (
            f"workshop skipped: ~{estimate} coins < {record['price']} for "
            f"{record['upgrade_id']}")
        if note is not None and note != self._last_skip_note:
            RerollJournal(self.root.parent.parent).append(
                instance=self.root.name, level="info", kind="workshop_skip", message=note)
        self._last_skip_note = note
        return worthwhile

    def _remember_target(self, upgrade_id: str, wallet: int | None,
                         price: int | None) -> None:
        path = self.root / "workshop-target.json"
        if wallet is None or price is None:
            path.unlink(missing_ok=True)
            return
        with db.reader(self.root / "tower_bot.db") as connection:
            baseline_run_id = connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM runs WHERE ended_at IS NOT NULL"
            ).fetchone()[0]
        temporary = path.with_name(f".workshop-target.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"account_id": self.account_id, "upgrade_id": upgrade_id,
                           "wallet": wallet, "price": price,
                           "purchase_count": self._history()[1].get(upgrade_id, 0),
                           "baseline_run_id": baseline_run_id,
                           "observed_at": time.time()}, output)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _publish(self, decision: RerollDecision) -> None:
        now = time.time()
        if decision == self._last_decision and now - self._last_published_at < 60:
            return
        self._last_decision = decision
        self._last_published_at = now
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "reroll-plan.json"
        best, purchases = self._history()
        values, _ = self._account_readings()
        preview = project_next(RerollFacts(self.account_id, best, purchases, values,
                                           variant=read_variant(self.root),
                                           utility_spent_coins=self._utility_spent()))
        payload = {**asdict(decision), "observed_at": now,
                   "next_purchases": [asdict(step) for step in preview]}
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
