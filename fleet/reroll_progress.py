"""Bind pure reroll decisions to one worker's verified account evidence."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, replace
from typing import Any, Iterable, Mapping
from pathlib import Path
from uuid import uuid4

import config
import db
import ocr
import upgrades
from account_state import AccountState
from fleet.reroll_journal import RerollJournal
from fleet.account_metrics import game_started_date
from fleet.reroll_lifetime import read_lifetime
from fleet.reroll_planner import (FILLER_SHARE, STARTER_MAX_PRICE, UTILITY_CEILING_COINS,
                                  UTILITY_TARGET_COINS, RerollDecision,
                                  RerollFacts, choose_next, project_next)
from fleet.reroll_variants import read_variant
from fleet.workshop_prices import WorkshopPrices, PriceQuote
from fleet.workshop_replay import discount_signature, read_actions, replay_quotes
from fleet.reroll_survival import prioritize_survival
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.build_route import GemRoute, RouteRules, resolve_route
from fleet.build_route_eval import (RouteFacts, RouteEvaluation, evaluate_battle,
                                    evaluate_resources, evaluate_workshop,
                                    select_battle_phase)
from fleet.build_route_store import RouteUnavailable
from fleet import coin_share
from fleet.lab_facts import best_waves, coins_per_hour, just_in_time_hold
from fleet.resource_blocks import (LabFacts, LabPlan, evaluate_lab_plan,
                                   gem_lane_blocks, next_unlock_slot)
from lab_plan import LabCadence, LabDecision, LabVisitOptions
import lab_catalog
from lab_unlock_rollout import LabUnlockRollout
from policy import AutopilotPolicy, UpgradeRule
from strategy import Shopping, ShoppingRule, Strategy

logger = logging.getLogger(__name__)

# Runs between death-screen detours home for a plan whose price or wallet is
# still unknown. The first detour reads it; if that read fails, going home
# again after every short early run costs more time than it plays.
UNKNOWN_PLAN_DETOUR_RUNS = 5


def workshop_locked(purchases: Mapping[str, int],
                    observations: Mapping[str, Mapping[str, Any]] | None = None) -> frozenset[str]:
    """Battle upgrades whose Workshop unlock tile this account has not confirmed.

    A row the battle tab has drawn is unlocked whatever the ledger says.
    """
    from workshop_unlocks import locked_upgrade_ids, owned_groups

    drawn = {uid for uid, row in (observations or {}).items()
             if row.get("status") in {"available", "maxed", "verified"}}
    bought = {uid for uid, count in purchases.items() if type(count) is int and count > 0}
    return locked_upgrade_ids(owned_groups(visible_ids=drawn, purchased_ids=bought))


class RerollProgress:
    """Read only this registered account and publish a bounded next action."""

    def __init__(self, worker_root: Path, account_id: str, account_state: AccountState,
                 *, read_only: bool = False) -> None:
        self.root = Path(worker_root)
        self.account_id = account_id
        self.account_state = account_state
        self.read_only = read_only
        from fleet.battle_prices import BattlePrices
        self.battle_prices = BattlePrices(self.root, account_id, read_only=read_only)
        self.price_memory = WorkshopPrices(self.root, account_id)
        self._quotes: dict[str, PriceQuote] = {}
        if not read_only:
            self._import_legacy_target()
        self._last_state: tuple[str, str | None, str] | None = None
        self._last_decision: RerollDecision | None = None
        self._last_published_at = 0.0
        self._spend_fraction: float | None = None
        self._battle_stage: str | None = None
        self._last_stats_attempt = 0.0
        self._last_skip_note: str | None = None
        # (upgrade_id, run id) of the last detour taken on an unknown plan.
        self._unknown_detour: tuple[str | None, int] | None = None
        self.lab_cadence = LabCadence(self.root, account_id)
        self.coin_jar = coin_share.LabCoinJar(self.root, account_id, read_only=read_only)
        self.route_runtime: BuildRouteRuntime | None = None
        self.route_error: str | None = None
        # Why the last shopping_policy() gave Workshop nothing to buy, for the
        # visit's ShoppingEnded. None when it returned something to buy.
        self.stop_reason: str | None = None
        self._route_evaluation: RouteEvaluation | None = None
        self.route_policy_revision: int | None = None
        self._menu_wallet: tuple[int, float] | None = None
        # The lab jar the last menu route evaluation settled.
        self._route_jar = 0
        # Levels this process confirmed in the current run, per upgrade,
        # ahead of the asynchronous event sink. Starts from the database
        # counts the first time a run is seen, so a restart mid-run loses
        # nothing; battle_policy reads max(database, tally) per upgrade.
        self._battle_tally: tuple[int | None, dict[str, int]] = (None, {})

    def note_menu_wallet(self, wallet_coins: int | None) -> None:
        """Keep a fresh, observed menu balance for the next route decision."""
        if type(wallet_coins) is int and wallet_coins >= 0:
            now = time.time()
            self._menu_wallet = (wallet_coins, now)
            # Also the run-payout replay's anchor: a run closed without a
            # payout otherwise leaves that replay unknown until the next
            # Workshop read, however many menu balances are read meanwhile.
            with db.reader(self.root / "tower_bot.db") as conn:
                last_run = conn.execute("SELECT COALESCE(MAX(id),0) FROM runs WHERE ended_at IS NOT NULL").fetchone()[0]
            anchor = self.price_memory.wallet
            if anchor is None or (anchor["coins"], anchor["run_id"]) != (wallet_coins, last_run):
                self.price_memory.wallet = {"coins": wallet_coins, "run_id": last_run, "observed_at": now}
                self.price_memory.save()

    def route_changed_since_policy(self) -> bool:
        if self.route_runtime is None or self.route_policy_revision is None:
            return False
        try:
            return self.route_runtime.current().revision != self.route_policy_revision
        except RouteUnavailable as exc:
            self.route_error = exc.reason
            return True

    def resource_rules(self) -> RouteRules:
        """This account's strategy rules; today's defaults when no route applies."""
        if self.route_runtime is None:
            return RouteRules()
        try:
            route = self.route_runtime.current()
        except RouteUnavailable:
            return RouteRules()
        try:
            return resolve_route(route, self.root.name, self.account_id).rules
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("resource_rules: resolve_route failed for %s (%s); using defaults",
                           self.account_id, exc)
            return RouteRules()

    def lab_unlock_due(self, now: float | None = None, *, wallet_gems: int | None = None) -> bool:
        """Whether the next lab slot is worth a gem-purchase visit."""
        moment = time.time() if now is None else now
        if not self.lab_unlocked() or self.lab_cadence.backing_off(moment):
            return False
        rules = self.resource_rules()
        slot = self.next_unlock_slot() if rules.gems.auto_unlock_lab_slots else None
        price = lab_catalog.lab_slot_gems(slot) if slot is not None else None
        return price is not None and self.lab_cadence.slot_due(slot, moment, wallet_gems,
                                                                min_gems=price + rules.gems.keep)

    def lab_due(self, now: float | None = None, *,
                wallet_coins: int | None = None,
                wallet_gems: int | None = None) -> bool:
        moment = time.time() if now is None else now
        if not self.lab_unlocked() or self.lab_cadence.backing_off(moment):
            return False
        research = self.resource_rules().labs.auto_start and self.lab_cadence.due(moment, wallet_coins)
        return self.lab_unlock_due(moment, wallet_gems=wallet_gems) or research

    def lab_visit_options(self) -> LabVisitOptions:
        rules = self.resource_rules()
        slot = self.next_unlock_slot() if rules.gems.auto_unlock_lab_slots else None
        return LabVisitOptions(start_research=rules.labs.auto_start,
                               unlock_slots=(slot,) if slot is not None else (),
                               keep_gems=rules.gems.keep,
                               direct_start=rules.labs.direct_start,
                               native_repeat=rules.labs.native_repeat)

    def note_lab_coin_debit(self, now: float | None = None) -> None:
        """A confirmed lab coin debit spent the savings: empty the jar."""
        if self.read_only:
            return
        self.coin_jar.reset(time.time() if now is None else now)

    def lab_unlocked(self) -> bool:
        """Whether this account has positive, persisted Labs unlock evidence."""
        return self.lab_cadence.unlocked()

    def note_lab_locked(self, source: str, *, now: float | None = None) -> None:
        if self.read_only:
            return
        self.lab_cadence.note_locked(source, time.time() if now is None else now)

    def note_lab_unlocked(self, source: str, *, caption: str | None = None,
                          now: float | None = None) -> None:
        if self.read_only:
            return
        self.lab_cadence.note_unlocked(
            source, time.time() if now is None else now, caption=caption)

    def initial_workshop_due(self) -> bool:
        """A new reroll account should visit Workshop for its tutorial grant."""
        try:
            _, purchases = self._history()
        except (OSError, ValueError):
            return False
        return not purchases

    def note_lab_slots(self, statuses: Mapping[int, str], wallet_gems: int | None,
                       now: float | None = None) -> None:
        self.lab_cadence.note_slots(statuses, wallet_gems, time.time() if now is None else now)

    def _effective_gems(self) -> GemRoute:
        """This account's gem lane; today's default when no route applies."""
        if self.route_runtime is None:
            return GemRoute()
        try:
            route = self.route_runtime.current()
        except RouteUnavailable:
            return GemRoute()
        try:
            return resolve_route(route, self.root.name, self.account_id).gems
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("_effective_gems: resolve_route failed for %s (%s); using defaults",
                           self.account_id, exc)
            return GemRoute()

    @property
    def fleet_root(self) -> Path | None:
        """<fleet root> for a worker root laid out as <fleet root>/workers/<worker id>."""
        return self.root.parent.parent if self.root.parent.name == "workers" else None

    @property
    def worker_id(self) -> str | None:
        return self.root.name if self.fleet_root is not None else None

    def unlock_rollout(self) -> LabUnlockRollout | None:
        """The fleet's shared lab-slot rollout record. A solo bot has none, so it never unlocks."""
        root = self.fleet_root
        return LabUnlockRollout(root) if root is not None else None

    def starter_rollout(self) -> "LabStarterRollout | None":
        """The fleet's shared lab-start rollout record. A solo bot has none: only legacy Game Speed."""
        from lab_starter_rollout import LabStarterRollout
        root = self.fleet_root
        return LabStarterRollout(root) if root is not None else None

    def next_unlock_slot(self) -> int | None:
        """The slot the gem lane unlocks next, from this account's slot record."""
        context = getattr(self, 'card_context', None)
        return next_unlock_slot(gem_lane_blocks(self._effective_gems()), self.lab_cadence.slot_records(),
                                card_context=context() if context is not None else None)

    def speed_target(self) -> float:
        return self.lab_cadence.speed_target()

    def note_lab_observation(self, decision: LabDecision, now: float | None = None) -> None:
        self.lab_cadence.note(decision, time.time() if now is None else now)

    def note_lab_failure(self, now: float | None = None) -> None:
        self.lab_cadence.note_failed(time.time() if now is None else now)

    def note_other_lab_research(self, now: float | None = None) -> None:
        self.lab_cadence.note_other_research(time.time() if now is None else now)

    def lab_strategy_plan(self, runtime: Any, *, available_coins: int | None,
                          wallet_gems: int | None = None, now: float | None = None,
                          excluded_research: frozenset[str] = frozenset()) -> LabPlan | None:
        """Plan from this account's assigned revision and scoped slot observations."""
        if self.route_runtime is None or getattr(getattr(runtime, "scope", None), "account_id", None) != self.account_id:
            return None
        observed_now = time.time() if now is None else now
        facts = self.account_state.lab_facts(runtime, now=observed_now)
        if facts is None:
            return None
        # Caller policy may reduce, never enlarge, the shared conservative budget.
        available = (min(facts.available_coins, available_coins)
                     if type(facts.available_coins) is int and type(available_coins) is int
                     and available_coins >= 0 else None)
        gems = facts.wallet_gems
        if wallet_gems is not None:
            gems = (min(gems, wallet_gems) if type(gems) is int and type(wallet_gems) is int
                    and wallet_gems >= 0 else None)
        rollout = self.unlock_rollout()
        rollout_slots = rollout.slots() if rollout is not None else None
        starter = self.starter_rollout()
        starter_state = starter.state() if starter is not None else None
        facts = replace(facts, available_coins=available, wallet_coins=available,
                        wallet_gems=gems,
                        jar=self.coin_jar.amount(quiet=True),
                        coins_per_hour=coins_per_hour(self.root, self.account_id),
                        best_waves=best_waves(self.root / "tower_bot.db") or None,
                        reserved_research=facts.reserved_research | excluded_research,
                        slot_ownership=self.lab_cadence.slot_records(),
                        owned_floor=(getattr(runtime, "slots_owned", None)
                                     if type(getattr(runtime, "slots_owned", None)) is int else None),
                        rollout=rollout_slots, worker=self.worker_id, starter=starter_state)
        route = self.route_runtime.current()
        return evaluate_lab_plan(resolve_route(route, self.root.name, self.account_id), facts)

    def resource_evaluation(self, wallet_coins: int | None,
                            wallet_gems: int | None) -> None:
        if self.route_runtime is None:
            return
        from web.account_catalog import registered_worker
        registration = registered_worker(self.root)
        if (registration is None or registration.account_id != self.account_id
                or db.bound_account(registration.db_path) != self.account_id):
            self.route_error = "worker account binding changed"
            return
        try:
            route = self.route_runtime.current()
            lab, slot2 = self.lab_cadence.route_observation()
            facts = RouteFacts(
                self.account_id, self.root.name, "main_menu", time.time(), time.time(),
                wallet_coins=wallet_coins, wallet_gems=wallet_gems,
                lab_slot2_owned=(slot2.get("status") == "owned" if slot2 else None),
                lab_slot_status=self.lab_cadence.slot_status_map(),
                game_speed_maxed=(lab.get("kind") == "done" if lab else None),
                lab_decision_kind=(str(lab["kind"]) if lab and isinstance(lab.get("kind"), str) else None),
                lab_price=(lab.get("price") if lab and type(lab.get("price")) is int else None),
            )
            rollout = self.unlock_rollout()
            self.route_runtime.publish_resources(
                evaluate_resources(resolve_route(route, self.root.name, self.account_id), facts,
                                   rollout.slots() if rollout is not None else None), facts)
        except (OSError, ValueError, RouteUnavailable) as exc:
            self.route_error = str(exc)

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

    def _account_readings(self, *, persist_lifetime: bool = True) -> tuple[dict[str, float], int | None]:
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
                    stones_field = fields.get("stones_earned") or {}
                    started = (game_started_date(started_field["raw_value"])
                               if started_field.get("status") == "observed"
                               and isinstance(started_field.get("raw_value"), str) else None)
                    hourly = (ocr.parse_number(rate_field["raw_value"])
                              if rate_field.get("status") == "observed"
                              and isinstance(rate_field.get("raw_value"), str) else None)
                    stones = (ocr.parse_number(stones_field["raw_value"])
                              if stones_field.get("status") == "observed"
                              and isinstance(stones_field.get("raw_value"), str) else None)
                    stored = self.lifetime_record()
                    observed_at = latest.get("observed_at")
                    if (persist_lifetime and isinstance(observed_at, (int, float))
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
                                              if hourly is not None and hourly >= 0 else {}),
                                           **({"lifetime_stones": stones}
                                              if stones is not None and stones >= 0 else {})}, output)
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
        wallet, self._quotes = self._pricing(purchases)
        prices = {uid: quote.price for uid, quote in self._quotes.items()}
        result = choose_next(RerollFacts(
            self.account_id, best, purchases, values, wallet, lifetime,
            prices, self._spend_fraction,
            variant=read_variant(self.root),
            utility_spent_coins=self._utility_spent(),
        ))
        quote = self._quotes.get(result.upgrade_id or "")
        if quote is not None:
            label = ("Observed Workshop price" if quote.source == "observed" else
                     "Catalog estimate; live price will be checked")
            result = replace(result, reason=f"{result.reason} {label}." )
        self._battle_stage = result.stage
        return result

    def route_facts(self) -> RouteFacts:
        """Capture account-bound facts used unchanged by worker and preview."""
        if self.route_runtime is None:
            raise ValueError("route runtime unavailable")
        best, purchases = self._history()
        values, lifetime = self._account_readings(persist_lifetime=not self.read_only)
        wallet, quotes = self._pricing(purchases)
        self._quotes = quotes
        anchor = self.price_memory.wallet
        observed_at = anchor["observed_at"] if anchor else None
        with db.reader(self.root / "tower_bot.db") as connection:
            last_run = connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM runs WHERE ended_at IS NOT NULL").fetchone()[0]
            if anchor:
                latest_run = connection.execute(
                    "SELECT MAX(ended_at) FROM runs WHERE id>? AND ended_at IS NOT NULL",
                    (anchor["run_id"],)).fetchone()[0]
                latest_balance = connection.execute(
                    "SELECT MAX(ts) FROM ledger WHERE ts>=? AND dry_run=0 "
                    "AND observed IS NOT NULL AND balance_after IS NOT NULL",
                    (anchor["observed_at"],)).fetchone()[0]
                observed_at = max(value for value in (observed_at, latest_run, latest_balance)
                                  if value is not None)
        if self._menu_wallet is not None and (
                observed_at is None or self._menu_wallet[1] >= observed_at):
            wallet, observed_at = self._menu_wallet
        return RouteFacts(
            self.account_id, self.root.name, "main_menu", observed_at, time.time(),
            best_tier_1_wave=best, purchases=purchases, values=values,
            wallet_coins=wallet, lifetime_coins=lifetime,
            prices={uid: quote.price for uid, quote in quotes.items()},
            variant=read_variant(self.root), utility_spent_coins=self._utility_spent(),
            visit_id=f"after-run:{last_run}",
            decision_sequence=self.route_runtime.sequence(),
            confirmed_purchases=self.route_runtime.purchase_counts("workshop"),
            price_evidence={uid: {"source": quote.source,
                "observed_at": self.price_memory.entries.get(uid, {}).get("observed_at")}
                for uid, quote in quotes.items()},
            maxed_ids=self.price_memory.maxed_ids(purchases),
        )

    def shopping_policy(self, base: Shopping) -> Shopping:
        jar = 0

        def stop(policy: Shopping, reason: str) -> Shopping:
            held = f" · lab jar holds {jar} coins" if jar > 0 else ""
            self.stop_reason = f"{reason}{held}"
            return policy

        self.stop_reason = None
        route = None
        if self.route_runtime is not None:
            try:
                route = self.route_runtime.current()
            except RouteUnavailable as exc:
                self.route_error = exc.reason
                self._route_evaluation = None
                return stop(replace(base, enabled=False, workshop=()), f"Route unavailable: {exc.reason}")
            self.route_error = None
        # Reserve the first 100 gems for the second lab even when a custom
        # reroll policy enables card spending.
        if not self.lab_cadence.slot_owned(2):
            base = replace(base, cards=replace(base.cards, enabled=False))
        rules = (resolve_route(route, self.root.name, self.account_id).rules
                 if route is not None else RouteRules())
        # gems.keep is a reserve: cards never spend below it.
        if rules.gems.keep > base.cards.gem_floor:
            base = replace(base, cards=replace(base.cards, gem_floor=rules.gems.keep))
        # The reroll planner selects one item; a visit-wide percentage cap
        # otherwise rejects an affordable unlock after the planner selects it.
        self._spend_fraction = None
        paused = False
        saving_reason: str | None = None
        route_wallet: int | None = None
        if route is not None and route.revision > 0:
            from web.account_catalog import registered_worker
            registration = registered_worker(self.root)
            if (registration is None or registration.account_id != self.account_id
                    or db.bound_account(registration.db_path) != self.account_id):
                self.route_error = "worker account binding changed"
                self._route_evaluation = None
                return stop(replace(base, enabled=False, workshop=()), "Worker account binding changed")
            try:
                self.route_runtime.sync_confirmed()
                pending = self.route_runtime.pending()
                if pending is not None and pending.revision != route.revision:
                    self.route_runtime.abandon_decision("route revision changed")
                    pending = None
                facts = self.route_facts()
                effective = resolve_route(route, self.root.name, self.account_id)
                lab_record, _ = self.lab_cadence.route_observation()
                if effective.rules.coins.lab_share.mode == "just_in_time":
                    # The saving plan's hold replaces the jar, which is neither grown
                    # nor reset here. A leftover save_pct amount stays on disk, unused
                    # by Workshop, until a visit in another mode settles it: save_pct
                    # keeps up to the waiting lab's price, the other modes empty it.
                    jar, paused, saving_reason = just_in_time_hold(
                        effective, self.root, self.account_id, wallet=facts.wallet_coins,
                        db_path=registration.db_path, now=time.time())
                else:
                    # Grows at most once per visit key: this runs on every menu scan.
                    jar = self.coin_jar.settle(effective, lab_record, facts.wallet_coins,
                                               facts.visit_id or "", time.time())
                    paused = coin_share.workshop_paused(effective, lab_record, facts.wallet_coins)
                    saving_reason = None
                facts = replace(facts, lab_coin_jar=jar)
                self._route_jar = jar
                route_wallet = facts.wallet_coins
                self.route_runtime.publish_facts(facts)
                evaluation = evaluate_workshop(effective, facts, pending)
                if (evaluation.status == "blocked" and pending is not None
                        and "Pending choice became unavailable" in evaluation.trace.reason):
                    self.route_runtime.abandon_decision("pending candidate unavailable")
                    facts = replace(facts, decision_sequence=self.route_runtime.sequence())
                    self.route_runtime.publish_facts(facts)
                    evaluation = evaluate_workshop(effective, facts, None)
                self._route_evaluation = evaluation
                if evaluation.status == "unknown" or evaluation.decision is None:
                    self.route_error = evaluation.trace.reason
                    return stop(replace(base, enabled=False, workshop=()),
                                f"{evaluation.trace.reason} ({evaluation.trace.matched_rule_id})")
                if evaluation.pending is not None and self.route_runtime.pending() is None:
                    self.route_runtime.remember_pending(evaluation.pending)
                self.route_runtime.acknowledge(route.revision, self.account_id)
                self.route_policy_revision = route.revision
                plan = evaluation.decision
            except (OSError, ValueError, RouteUnavailable) as exc:
                self.route_error = str(exc)
                self._route_evaluation = None
                return stop(replace(base, enabled=False, workshop=()), f"Strategy unavailable: {exc}")
        else:
            self._route_evaluation = None
            self.route_policy_revision = route.revision if route is not None else None
            plan = self.decision()
        # A fresh reroll account must enter Workshop once to claim its 50-coin
        # tutorial grant. Keep this first visit bounded to the starter budget;
        # the buyer still checks the live wallet and price before every tap.
        if (self.initial_workshop_due() and plan.stage != "strategy_observe"
                and plan.item is not None and plan.category is not None):
            self._publish(plan)
            return replace(base, enabled=base.enabled,
                           workshop=(ShoppingRule(plan.item, plan.category),),
                           allow_unlocks=True, coin_budget=50, coin_budget_pct=None,
                           cards=replace(base.cards, enabled=False))
        if paused:
            # labs_first: an automated lab waits for coins, so Workshop holds
            # every coin until the lab check starts it. just_in_time: the labs
            # starting now plus the reserve take the whole wallet (or it is
            # unread). Claims, the tutorial
            # grant and Cards are not Workshop visits, so they continue.
            # Publish that Workshop is paused instead of the buy `plan` above:
            # that plan cannot run while paused, and publishing it anyway
            # would show a "next buy" on the fleet UI that never happens.
            reason = (f"Workshop paused: saving coins for labs · {saving_reason}" if saving_reason
                      else f"Workshop paused: saving coins for the next automated lab "
                           f"({coin_share.waiting_lab_price(effective, lab_record)} coins).")
            self._publish(replace(plan, state="save_coins", upgrade_id=None, item=None, category=None,
                                  price=None, reason=reason))
            self.stop_reason = reason
            return replace(base, workshop=())
        self._publish(plan)
        if plan.stage == "strategy_observe":
            ids = self._route_evaluation.trace.observation_ids
            rows = tuple(ShoppingRule(upgrades.by_id(uid).name, upgrades.by_id(uid).category)
                         for uid in ids)
            return replace(base, workshop=rows, allow_unlocks=False, coin_budget=0, coin_budget_pct=None,
                           cards=replace(base.cards, enabled=False))
        if plan.item is None or plan.category is None:
            return stop(replace(base, enabled=False, workshop=()), plan.reason)
        if not self.workshop_worthwhile():
            return stop(replace(base, enabled=False),
                        plan.reason if plan.state == "save_coins" else
                        f"Next buy {plan.item} not affordable yet ({plan.wallet_coins}/{plan.price} coins)")
        budget = base.coin_budget
        if route is not None and route.revision > 0 and route_wallet is not None:
            effective = resolve_route(route, self.root.name, self.account_id)
            ceiling = coin_share.workshop_ceiling(effective, route_wallet, jar)
            budget = ceiling if budget is None else min(budget, ceiling)
        if plan.stage == "strategy" and plan.price is not None:
            budget = plan.price if budget is None else min(budget, plan.price)
        if plan.starter:
            budget = STARTER_MAX_PRICE if budget is None else min(budget, STARTER_MAX_PRICE)
        elif plan.filler:
            assert plan.wallet_coins is not None
            # This fallback was selected against a known Defense Absolute
            # price relative to Thorns, not the general 20%-of-wallet limit.
            # Bound the visit to that exact quote; the buyer checks the live
            # price again before spending.
            ceiling = (plan.price if plan.stage == "turtle"
                       and plan.upgrade_id == "defense_absolute"
                       and plan.price is not None
                       else int(plan.wallet_coins * FILLER_SHARE))
            budget = ceiling if budget is None else min(budget, ceiling)
        elif plan.stage != "strategy":
            spent = self._utility_spent()
            if (plan.category == "UTILITY" and spent is not None
                    and spent < UTILITY_TARGET_COINS):
                ceiling = UTILITY_CEILING_COINS - spent
                budget = ceiling if budget is None else min(budget, ceiling)
        return replace(base, workshop=(ShoppingRule(plan.item, plan.category),),
                       allow_unlocks=True, coin_budget=budget, coin_budget_pct=None)

    def await_battle_receipt(self, run_id: int | None, sequence: int | None) -> None:
        """Fence every confirmed modeled tap, including the last in a batch."""
        if type(run_id) is int and type(sequence) is int:
            previous = getattr(self, '_battle_receipt_fence', None)
            minimum = max(sequence + 1, previous[1]) if previous and previous[0] == run_id else sequence + 1
            self._battle_receipt_fence = (run_id, minimum)

    def note_battle_levels(self, run_id: int | None, upgrade_id: str, levels: int) -> None:
        """Count confirmed battle levels before the event sink commits them."""
        if type(run_id) is not int or type(levels) is not int or levels < 1:
            return
        tally_run, tally = self._battle_tally
        if tally_run != run_id:
            return  # never started from this run's database counts; the sink catches up
        tally[upgrade_id] = tally.get(upgrade_id, 0) + levels

    def _tallied_counts(self, run_id: int | None,
                        counts: dict[str, int] | None) -> dict[str, int] | None:
        """Per-upgrade max(database, tally); the tally restarts with each run."""
        if counts is None or type(run_id) is not int:
            return counts
        if self._battle_tally[0] != run_id:
            self._battle_tally = (run_id, dict(counts))
        tally = self._battle_tally[1]
        for upgrade_id, count in counts.items():
            tally[upgrade_id] = max(tally.get(upgrade_id, 0), count)
        return dict(tally)

    def battle_policy(self, base: AutopilotPolicy,
                      observations: Mapping[str, Mapping[str, Any]] | None = None,
                      *, run_id: int | None = None, wave: int | None = None,
                      cash: int | None = None,
                      combat: Mapping[str, float] | None = None, pending_purchase: bool = False,
                      visible_upgrade_ids: tuple[str, ...] = (), battle_batch_purchases: int = 0,
                      batch_route_token: str | None = None, batch_rule_id: str | None = None,
                      after_receipt_sequence: int | None = None,
                      battle_tab: str | None = None) -> AutopilotPolicy:
        route = None
        if self.route_runtime is not None:
            try:
                route = self.route_runtime.current()
            except RouteUnavailable as exc:
                self.route_error = exc.reason
                return replace(base, enabled=False, rules=())
            self.route_error = None
        effective = resolve_route(route, self.root.name, self.account_id) if route is not None else None
        if effective is not None and effective.battle.mode in {"phases", "blocks"}:
            from web.account_catalog import registered_worker
            registration = registered_worker(self.root)
            if (registration is None or registration.account_id != self.account_id
                    or db.bound_account(registration.db_path) != self.account_id):
                self.route_error = "worker account binding changed"
                return replace(base, enabled=False, rules=())
            moment = time.time()
            best, purchases = self._history()
            # A row still locked in the Workshop is never drawn in battle, so
            # it is recorded as locked rather than left unknown: unknown sends
            # the autopilot scrolling for it and pauses any condition that
            # reads it, and neither can end before the Workshop unlock.
            locked = workshop_locked(purchases, observations or {})
            rows = {**(observations or {}), **{
                uid: dict(upgrade_id=uid, status="locked", value=None, price=None, observed_at=moment)
                for uid in locked}}
            counts = self.route_runtime.purchase_counts("battle", run_id)
            if config.BATTLE_BURST_ENABLED:
                # The tally replaces the receipt fence: a confirmed level
                # counts at once instead of disabling buying until the sink
                # commits it.
                counts = self._tallied_counts(run_id, counts)
            else:
                if after_receipt_sequence is not None:
                    self.await_battle_receipt(run_id, after_receipt_sequence)
                fence = getattr(self, '_battle_receipt_fence', None)
                if fence is not None:
                    if fence[0] == run_id and (counts is None or sum(counts.values()) < fence[1]):
                        # The event sink commits asynchronously. Do not calibrate
                        # a post-purchase frame against the old receipt count.
                        return replace(base, enabled=False, rules=())
                    self._battle_receipt_fence = None
            if batch_route_token != f'{self.account_id}:{route.revision}:{run_id}':
                battle_batch_purchases = 0
            facts = RouteFacts(
                self.account_id, self.root.name, "battle", moment, moment,
                best_tier_1_wave=best, run_id=run_id, wave=wave, battle_cash=cash,
                battle_health=(combat or {}).get("health"),
                battle_max_health=(combat or {}).get("max_health"),
                enemy_damage=(combat or {}).get("enemy_damage"),
                upgrade_rows=rows,
                battle_price_quotes=self.battle_prices.update(run_id=run_id, wave=wave,
                    counts=counts, rows=rows, now=moment, pending=pending_purchase),
                visible_upgrade_ids=visible_upgrade_ids, battle_batch_purchases=battle_batch_purchases,
                battle_batch_rule_id=batch_rule_id,
                battle_tab=battle_tab,
                visit_id=(f"battle:{run_id}" if effective.battle.mode == "blocks" else
                          f"battle:{run_id}:{wave}") if run_id is not None and wave is not None else None,
                run_purchases=counts, decision_sequence=sum((counts or {}).values()),
            )
            effective = resolve_route(route, self.root.name, self.account_id)
            pending = (self.route_runtime.battle_pending(facts, route.revision)
                       if effective.battle.mode == "blocks" else None)
            evaluation = evaluate_battle(effective, facts, pending)
            if evaluation.pending is not None and evaluation.pending != pending:
                self.route_runtime.remember_battle_pending(evaluation.pending)
            self.route_runtime.publish_battle_evaluation(evaluation, facts)
            if wave is None or cash is None or run_id is None:
                return replace(base, enabled=False, rules=())
            if effective.battle.mode == "blocks":
                from fleet.strategy_blocks import batch_size_for, program_upgrade_ids, uses_modeled_prices
                self.route_runtime.acknowledge(route.revision, self.account_id)
                observe_only = evaluation.status != "observed" or evaluation.decision is None
                ids = (tuple(uid for uid in evaluation.trace.observation_ids
                             or program_upgrade_ids(effective.battle.blocks) if uid not in locked)
                       if observe_only else (evaluation.decision.upgrade_id,))
                return replace(base, enabled=True, preset="manual", purpose="milestone",
                    rules=tuple(UpgradeRule(uid, target=(evaluation.decision.target if not observe_only else None))
                                for uid in ids),
                    cash_spend_limit_pct=100, observe_only=observe_only, single_purchase=True,
                    decision_token=f"{self.account_id}:{route.revision}:{run_id}:{facts.decision_sequence}",
                    max_purchase_price=evaluation.decision.price if not observe_only else None,
                    modeled_pool=uses_modeled_prices(effective.battle.blocks),
                    burst_price_ceiling=(evaluation.decision.burst_price_ceiling
                        if not observe_only and evaluation.decision.price_source == "model" else None),
                    battle_price_quote=(dict(facts.battle_price_quotes[evaluation.decision.upgrade_id],
                        upgrade_id=evaluation.decision.upgrade_id, sequence=facts.decision_sequence,
                        rule_id=evaluation.trace.matched_rule_id,
                        batch_size=batch_size_for(effective.battle.blocks, evaluation.trace.matched_rule_id)) if not observe_only
                        and evaluation.decision.price_source == "model" else None))
            _, phase = select_battle_phase(effective, facts)
            if phase is None:
                return replace(base, enabled=False, rules=())
            self.route_runtime.acknowledge(route.revision, self.account_id)
            observe_only = evaluation.status != "observed" or evaluation.decision is None
            ids = ([uid for uid in phase.priority_ids if uid not in locked] if observe_only else
                   [evaluation.decision.upgrade_id])
            return replace(base, enabled=True, preset="manual", purpose="milestone",
                           rules=tuple(UpgradeRule(uid) for uid in ids),
                           cash_spend_limit_pct=phase.cash_spend_limit_pct,
                           observe_only=observe_only)
        if self._battle_stage is None:
            best, _ = self._history()
            self._battle_stage = "stones" if best is not None and best >= 60 else (
                "turtle" if best is not None and best >= 20 else "opening")
        candidates = (
            UpgradeRule("cash_per_wave", target=10),
            UpgradeRule("coins_per_kill_bonus", target=1.25),
            UpgradeRule("cash_bonus", target=1.25),
            UpgradeRule("defense_absolute"), UpgradeRule("thorns", target=51),
            UpgradeRule("health"),
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
        utility_open = any(entry.unlock and entry.category == "UTILITY" and purchases.get(entry.id, 0) > 0
                           for entry in upgrades.CATALOG)
        locked = workshop_locked(purchases)
        available = tuple(rule for rule in candidates
                          if (utility_open or upgrades.by_id(rule.upgrade_id).category != "UTILITY")
                          and rule.upgrade_id not in locked)
        return replace(base, preset="manual", purpose="milestone",
                       rules=prioritize_survival(available, observations or {}))

    def _discount_signature(self) -> str:
        revision = self.account_state.snapshot().get("revision") or {}
        scope = getattr(self.account_state, "verified_scope", None)
        return discount_signature(revision, asdict(scope) if scope is not None else None,
                                  now=time.time())

    def _import_legacy_target(self) -> None:
        if self.price_memory.path.exists():
            return
        try:
            record = json.loads((self.root / "workshop-target.json").read_text(encoding="utf-8"))
            if record.get("account_id") != self.account_id:
                return
            uid, count, observed = record["upgrade_id"], record["purchase_count"], record["observed_at"]
            self.price_memory.observe(uid, record["price"], count, now=observed)
            if type(record["wallet"]) is int and record["wallet"] >= 0 and type(record["baseline_run_id"]) is int:
                self.price_memory.wallet = {"coins": record["wallet"], "run_id": record["baseline_run_id"],
                                            "observed_at": observed}
            self.price_memory.save()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return

    def _pricing(self, purchases: Mapping[str, int]) -> tuple[int | None, dict[str, PriceQuote]]:
        anchor = self.price_memory.wallet
        earliest = min([entry["observed_at"] for entry in self.price_memory.entries.values()] +
                       ([anchor["observed_at"]] if anchor else [time.time()]))
        changes: list[tuple[float, int, int | None, int | None]] = []
        with db.reader(self.root / "tower_bot.db") as conn:
            actions = read_actions(conn, self.price_memory, now=time.time())
            rows = conn.execute("SELECT id,ts,kind,item,category,delta,balance_after,observed,detail,reason "
                                # ROUTE_DECISION audits a weighted draw; it moves no coins.
                                "FROM ledger WHERE dry_run=0 AND ts>=? AND kind!='ROUTE_DECISION' AND "
                                "(currency='coins' OR (currency IS NULL AND kind='WORKSHOP_BUY') "
                                "OR (kind='BUY_SKIPPED' AND reason='unconfirmed')) "
                                "ORDER BY ts,id", (earliest,)).fetchall()
            if anchor:
                runs = conn.execute("SELECT id,ended_at,coins FROM runs WHERE id>? AND ended_at>?",
                                    (anchor["run_id"], anchor["observed_at"])).fetchall()
                changes.extend((row["ended_at"], -row["id"], row["coins"], None) for row in runs)
        for row in rows:
            if row["kind"] == "BUY_SKIPPED" and row["reason"] == "unconfirmed":
                upgrade = upgrades.resolve(row["item"], row["category"])
                if upgrade:
                    if anchor and row["ts"] >= anchor["observed_at"]:
                        changes.append((row["ts"], row["id"], None, None))
                continue
            if anchor and row["ts"] >= anchor["observed_at"] and row["kind"] != "RUN_PAYOUT":
                # A balance observation closes an earlier uncertainty. Other
                # ledger debits/rewards are applied once; run payouts above
                # come from completed runs, so they aren't counted twice.
                balance = row["balance_after"] if row["observed"] is not None else None
                changes.append((row["ts"], row["id"], row["delta"], balance))
        wallet = anchor["coins"] if anchor else None
        for _, _, delta, balance in sorted(changes):
            if balance is not None:
                wallet = balance
            elif delta is None:
                wallet = None
            elif wallet is not None:
                wallet += delta
        if wallet is not None and wallet < 0:
            wallet = None
        quotes = replay_quotes(self.price_memory, actions, purchases, signature=self._discount_signature())
        return wallet, quotes

    def price_quotes(self) -> dict[str, PriceQuote]:
        """Typed planning evidence; callers must retain live row/wallet checks."""
        _, purchases = self._history()
        return self._pricing(purchases)[1]

    def inspection_resolved(self) -> bool:
        if self.route_runtime is not None:
            self.shopping_policy(Strategy.from_config().shopping)
            return self._route_evaluation is not None and self._route_evaluation.status != "unknown"
        decision = self.decision()
        return decision.price is not None and decision.wallet_coins is not None

    def observe_prices(self, prices: Mapping[str, int | None], wallet: int | None,
                       *, maxed: Iterable[str] = ()) -> None:
        """Learn all readable rows during an already necessary Workshop visit."""
        _, purchases = self._history()
        now = time.time()
        signature = self._discount_signature()
        for uid, price in prices.items():
            self.price_memory.observe(uid, price, purchases.get(uid, 0), now=now,
                                      discount_signature=signature)
        for uid in maxed:
            self.price_memory.observe_maxed(uid, purchases.get(uid, 0), now=now)
        if type(wallet) is int and wallet >= 0:
            with db.reader(self.root / "tower_bot.db") as conn:
                last_run = conn.execute("SELECT COALESCE(MAX(id),0) FROM runs WHERE ended_at IS NOT NULL").fetchone()[0]
            self.price_memory.wallet = {"coins": wallet, "run_id": last_run, "observed_at": now}
        self.price_memory.save()

    def observe_price(self, upgrade_id: str, wallet: int | None, price: int | None) -> None:
        if self._route_evaluation is not None:
            plan = self._route_evaluation.decision
            if plan is not None and upgrade_id == plan.upgrade_id:
                self.observe_prices({upgrade_id: price}, wallet)
            return
        if upgrade_id != self.decision().upgrade_id:
            return
        self.observe_prices({upgrade_id: price}, wallet)
        self._publish(self.decision())

    def purchase_reason(self, upgrade_id: str) -> str | None:
        """Why the current route decision picked this upgrade, if it did."""
        evaluation = self._route_evaluation
        if evaluation is None or evaluation.decision is None or evaluation.decision.upgrade_id != upgrade_id:
            return None
        odds = evaluation.trace.eligible_odds.get(upgrade_id)
        if odds is None:
            return evaluation.trace.reason
        return f"Random draw ({odds:.0%}) · {evaluation.trace.reason}"

    def workshop_worthwhile(self, *, publish_estimate: bool = False, detour: bool = False) -> bool:
        """Whether a Workshop visit could buy the planned upgrade now.

        `publish_estimate` is for the GAME_OVER caller only. A skipped visit
        retries without passing the menu that republishes the plan, so the
        fleet card would keep the last menu balance run after run; publish
        the run-payout estimate instead. The menu caller must not: its plan
        was just published from a fresh balance read.

        `detour` marks the GAME_OVER caller choosing HOME over RETRY. An
        unknown price or wallet still earns a detour to read it, but the same
        unknown plan earns another only every UNKNOWN_PLAN_DETOUR_RUNS runs.
        """
        evaluation = self._route_evaluation
        undecided = evaluation is not None and evaluation.decision is None
        # An undecided route evaluation records its own reason as the route
        # error. That is a wait, not a failure, and run payouts can end it.
        if self.route_error is not None and not (
                undecided and self.route_error == evaluation.trace.reason):
            return False
        if undecided:
            # Re-ask the route on the run-payout wallet, never the legacy
            # planner: that one names rows the strategy does not plan.
            evaluation = self._projected_route_evaluation() or evaluation
        if evaluation is not None and evaluation.decision is None:
            waiting = evaluation.status != "unknown"
            worthwhile = not waiting and (not detour or self._unknown_detour_due(None))
            note = (None if worthwhile else f"workshop skipped: {evaluation.trace.reason}" if waiting
                    else f"workshop skipped: {evaluation.trace.reason}; next read within "
                         f"{UNKNOWN_PLAN_DETOUR_RUNS} runs")
            if note is not None and note != self._last_skip_note:
                RerollJournal(self.root.parent.parent).append(
                    instance=self.root.name, level="info", kind="workshop_skip", message=note)
            self._last_skip_note = note
            return worthwhile
        if evaluation is not None:
            # The route decision was cached at the last menu visit. Asked on
            # GAME_OVER, its wallet would never grow: the worker retries
            # instead of going home, so no menu read ever refreshes it. Carry
            # the choice forward with the run-payout projection instead.
            _, purchases = self._history()
            wallet, _ = self._pricing(purchases)
            plan = replace(evaluation.decision, wallet_coins=wallet)
            if plan.state == "save_coins" and plan.item is not None and plan.price is not None:
                plan = replace(plan, reason=f"Saving for {plan.item} ({wallet}/{plan.price} coins, "
                                            "estimated from run payouts)")
        else:
            plan = self.decision()
        worthwhile = (plan.upgrade_id is not None and
                      (plan.wallet_coins is None or plan.wallet_coins > 0) and
                      (plan.price is None or plan.wallet_coins is None or plan.wallet_coins >= plan.price))
        if detour and worthwhile and (plan.price is None or plan.wallet_coins is None):
            worthwhile = self._unknown_detour_due(plan.upgrade_id)
        note = (None if worthwhile else
                f"workshop skipped: {plan.item} price or balance still unread; next read within "
                f"{UNKNOWN_PLAN_DETOUR_RUNS} runs" if plan.price is None or plan.wallet_coins is None else
                f"workshop skipped: {plan.wallet_coins} coins; {plan.item} needs {plan.price}")
        if note is not None and note != self._last_skip_note:
            RerollJournal(self.root.parent.parent).append(
                instance=self.root.name, level="info", kind="workshop_skip", message=note)
            if publish_estimate:
                self._publish(plan)
        self._last_skip_note = note
        return worthwhile

    def _projected_route_evaluation(self) -> RouteEvaluation | None:
        """The route re-evaluated on the run-payout wallet, publishing nothing.

        For the death screen, where no menu read can refresh the wallet. The
        jar is the one the last menu evaluation settled, not settled again.
        """
        try:
            route = self.route_runtime.current()
            facts = replace(self.route_facts(), lab_coin_jar=self._route_jar)
            pending = self.route_runtime.pending()
        except (OSError, ValueError, RouteUnavailable):
            return None
        return evaluate_workshop(resolve_route(route, self.root.name, self.account_id),
                                 facts, pending)

    def _unknown_detour_due(self, upgrade_id: str | None) -> bool:
        # Keyed on the newest run's id, not a finished-run count: the death
        # screen is decided over many frames while RunEnded is still landing,
        # and a count that ticks mid-screen would flip HOME to RETRY.
        path = self.root / "tower_bot.db"
        if not path.is_file():
            return True
        with db.reader(path) as connection:
            run_id = connection.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        if run_id is None:
            return True
        last = self._unknown_detour
        if last is not None and last[0] == upgrade_id and 0 < run_id - last[1] < UNKNOWN_PLAN_DETOUR_RUNS:
            return False
        self._unknown_detour = (upgrade_id, run_id)
        return True

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
        banned: frozenset[str] = frozenset()
        priorities: tuple[str, ...] = ()
        block_program = False
        route_revision = None
        if self.route_runtime is not None:
            try:
                route = self.route_runtime.current()
            except RouteUnavailable:
                route = None
            if route is not None and route.revision > 0:
                route_revision = route.revision
                workshop = resolve_route(route, self.root.name, self.account_id).workshop
                banned = workshop.banned_upgrade_ids
                block_program = workshop.mode == "blocks"
                if workshop.mode == "priorities":
                    priorities = workshop.priority_ids
        preview = () if block_program else project_next(RerollFacts(self.account_id, best, purchases, values,
                                           variant=read_variant(self.root),
                                           utility_spent_coins=self._utility_spent()),
                               banned_upgrade_ids=banned, priority_ids=priorities)
        payload = {**asdict(decision), "observed_at": now,
                   "route_revision": route_revision,
                   "projection_note": ("Future block choices depend on fresh prices and confirmed purchases"
                                       if block_program else None),
                   "confirmed_purchases": purchases,
                   "price_source": (self._quotes[decision.upgrade_id].source
                                    if decision.upgrade_id in self._quotes else None),
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
