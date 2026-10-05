"""Pure, explainable evaluation of a reroll account's effective Build Route."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from fleet.build_route import BattleBranch, BattlePhase, EffectiveRoute, is_lab_list
from fleet.coin_share import spendable_wallet, workshop_ceiling, workshop_limit_pct
from fleet.reroll_planner import (DRAW_SHARPNESS, RerollDecision, RerollFacts,
                                  _ban_closure, _owned_groups, choose_next)
import builds
import lab_catalog
import upgrades
from workshop_unlocks import available


EVIDENCE_MAX_AGE_SECONDS = 120


@dataclass(frozen=True)
class PendingDecision:
    account_id: str
    revision: int
    visit_id: str
    sequence: int
    candidate_fingerprint: str
    chosen_id: str
    draw_gate: int | None = None
    eligible_weights: Mapping[str, int | float] = field(default_factory=dict)
    matched_rule_id: str = ""


@dataclass(frozen=True)
class RouteFacts:
    account_id: str
    worker: str
    screen: str | None = None
    observed_at: float | None = None
    now: float | None = None
    best_tier_1_wave: int | None = None
    purchases: Mapping[str, int] = field(default_factory=dict)
    values: Mapping[str, float] = field(default_factory=dict)
    wallet_coins: int | None = None
    lifetime_coins: int | None = None
    prices: Mapping[str, int] = field(default_factory=dict)
    variant: str | None = None
    utility_spent_coins: int | None = None
    run_id: int | None = None
    wave: int | None = None
    battle_cash: int | None = None
    battle_health: float | None = None
    battle_max_health: float | None = None
    enemy_damage: float | None = None
    upgrade_rows: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    wallet_gems: int | None = None
    lab_slot2_owned: bool | None = None
    # lab-slots.json statuses keyed "2"-"5" (string keys survive the JSON facts snapshot).
    lab_slot_status: Mapping[str, str] = field(default_factory=dict)
    game_speed_maxed: bool | None = None
    lab_decision_kind: str | None = None
    lab_price: int | None = None
    visit_id: str | None = None
    decision_sequence: int = 0
    confirmed_purchases: Mapping[str, int] | None = None
    run_purchases: Mapping[str, int] | None = None
    price_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    lab_coin_jar: int = 0
    battle_price_quotes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    visible_upgrade_ids: tuple[str, ...] = ()
    battle_batch_purchases: int = 0
    battle_batch_rule_id: str | None = None
    # Workshop rows read as MAX with no later buy or priced read. A tuple, not
    # a set: the facts snapshot is written with json.dump(asdict(facts)).
    maxed_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionTrace:
    matched_rule_id: str
    reason: str
    evidence_age_seconds: float | None = None
    price_source: str = "unknown"
    variant: str | None = None
    rejected: tuple[str, ...] = ()
    spend_ceiling: int | None = None
    branch_id: str | None = None
    phase_id: str | None = None
    phase_state: str | None = None
    next_phase_id: str | None = None
    transition_reason: str | None = None
    eligible_odds: Mapping[str, float] = field(default_factory=dict)
    draw_gate: int | None = None
    observation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BattleDecision:
    account_id: str
    stage: str
    state: str
    upgrade_id: str | None
    item: str | None
    category: str | None
    price: int | None
    battle_cash: int | None
    reason: str
    target: float | None = None
    price_source: str = "observed"


@dataclass(frozen=True)
class ResourceStep:
    action: str
    status: str
    reason: str


@dataclass(frozen=True)
class ResourceEvaluation:
    gem_step: ResourceStep
    lab_step: ResourceStep


def _future_lab_action(route: EffectiveRoute) -> str | None:
    if route.labs.mode != "blocks":
        return next((step for step in route.labs.steps if step != "research_game_speed"), None)
    if is_lab_list(route.labs):
        return next((f"research_{entry['lab_id']}" for entry in route.labs.blocks[0]["entries"]
                     if entry["lab_id"] != lab_catalog.GAME_SPEED), None)
    track = next((item for item in route.labs.blocks if 1 in item["slots"]), None)
    if track is None:
        return None
    for block in track["children"][1:]:
        return f"research_{block['lab_id']}" if block["type"] == "research" else block["type"]
    return None


def _slot_ownership(facts: RouteFacts) -> dict[int, dict[str, str]]:
    """Slot 2-5 ownership from the worker's lab-slots record, else the legacy slot-2 flag."""
    if facts.lab_slot_status:
        return {int(slot): {"status": status} for slot, status in facts.lab_slot_status.items()
                if str(slot) in {"2", "3", "4", "5"} and status in {"owned", "locked"}}
    if facts.lab_slot2_owned is None:
        return {}
    return {2: {"status": "owned" if facts.lab_slot2_owned else "locked"}}


def _gem_action(route: EffectiveRoute, block: Mapping[str, Any]) -> str:
    if block["type"] == "unlock_lab_slot":
        return f"unlock_lab_slot_{block['slot']}"
    return block["id"].removeprefix("legacy.gems.") if route.gems.mode != "blocks" else block["type"]


def _gem_step(route: EffectiveRoute, facts: RouteFacts,
              rollout: Mapping[int, Any] | None) -> ResourceStep:
    """Fleet State's gem step: the next slot unlock and where its rollout stands."""
    from fleet.resource_blocks import _gem_met, gem_lane_blocks
    from lab_unlock_rollout import SlotRollout, rollout_status
    blocks = gem_lane_blocks(route.gems)
    ownership = _slot_ownership(facts)
    first = _gem_action(route, blocks[0]) if blocks else "unlock_lab_slot_2"
    if facts.wallet_gems is None:
        return ResourceStep(first, "unknown", "Gem balance or lab ownership unverified")
    pending = next((block for block in blocks if _gem_met(block, ownership) is not True), None)
    if pending is not None and pending["type"] == "unlock_lab_slot":
        slot, action = pending["slot"], _gem_action(route, pending)
        if _gem_met(pending, ownership) is None:
            return ResourceStep(action, "unknown", "Gem balance or lab ownership unverified")
        price = lab_catalog.lab_slot_gems(slot)
        if price is None:
            return ResourceStep(action, "blocked", f"Slot {slot} price unknown")
        if not route.rules.gems.auto_unlock_lab_slots:
            return ResourceStep(action, "planned", "Planned · auto-unlock off")
        state = (rollout or {}).get(slot) or SlotRollout()
        if state.stage == "halted":
            frames = f" · {len(state.evidence)} evidence frame(s)" if state.evidence else ""
            return ResourceStep(action, "blocked", rollout_status(state, slot, facts.worker) + frames)
        need = price + route.rules.gems.keep
        if facts.wallet_gems < need:
            return ResourceStep(action, "blocked", f"Save {need - facts.wallet_gems} more gems")
        waiting = state.stage == "canary" and state.canary_worker != facts.worker
        return ResourceStep(action, "blocked" if waiting else "supported",
                            rollout_status(state, slot, facts.worker))
    owned = max((slot for slot, record in ownership.items() if record["status"] == "owned"), default=None)
    if owned is None:
        return ResourceStep(first, "unknown", "Gem balance or lab ownership unverified")
    starter = f"Slot {owned} owned"
    if pending is not None:
        return ResourceStep(_gem_action(route, pending), "planned", f"{starter} · planned, not automated")
    return ResourceStep(f"lab_slot_{owned}_owned", "supported", starter)


def evaluate_resources(route: EffectiveRoute, facts: RouteFacts,
                       rollout: Mapping[int, Any] | None = None) -> ResourceEvaluation:
    """Describe the gem step from the unlock rollout, and existing lab automation."""
    gem = _gem_step(route, facts, rollout)
    if facts.game_speed_maxed is True:
        future_lab = _future_lab_action(route)
        lab = (ResourceStep(future_lab, "planned", "Planned · not automated") if future_lab else
               ResourceStep("game_speed_maxed", "supported", "Game Speed research complete"))
    elif facts.lab_decision_kind is None:
        lab = ResourceStep("research_game_speed", "unknown", "Lab decision unverified")
    elif facts.lab_decision_kind == "start":
        lab = ResourceStep("research_game_speed", "supported",
                           f"Game Speed available for {facts.lab_price} coins")
    elif facts.lab_decision_kind in {"wait_coins", "wait_running", "wait_unlock", "inspect"}:
        lab = ResourceStep("research_game_speed", "blocked",
                           f"Game Speed: {facts.lab_decision_kind.replace('_', ' ')}")
    elif facts.lab_decision_kind == "done":
        lab = ResourceStep("game_speed_maxed", "supported", "Game Speed research complete")
    else:
        lab = ResourceStep("research_game_speed", "unknown", "Lab decision unverified")
    return ResourceEvaluation(gem, lab)


@dataclass(frozen=True)
class RouteEvaluation:
    account_id: str
    revision: int | None
    status: str
    decision: RerollDecision | BattleDecision | None
    trace: DecisionTrace
    evidence_at: float | None
    pending: PendingDecision | None = None

    @classmethod
    def unknown(cls, reason: str, facts: RouteFacts) -> RouteEvaluation:
        age = (facts.now - facts.observed_at
               if facts.now is not None and facts.observed_at is not None else None)
        return cls(facts.account_id, None, "unknown", None,
                   DecisionTrace("", reason, age, variant=facts.variant), facts.observed_at)


def evaluate(route: EffectiveRoute, facts: RouteFacts,
             pending: PendingDecision | None) -> RouteEvaluation:
    """A recommendation only; live executors still recheck every spend."""
    return evaluate_workshop(route, facts, pending)


def evaluate_battle(route: EffectiveRoute, facts: RouteFacts,
                    pending: PendingDecision | None) -> RouteEvaluation:
    """Choose one in-game row from a verified run, phase and cash budget."""
    if route.battle.mode == "blocks":
        from fleet.strategy_blocks import evaluate_program
        return evaluate_program(route, facts, pending, "battle")
    if facts.account_id == "" or facts.worker == "" or facts.run_id is None or facts.wave is None or facts.battle_cash is None:
        return RouteEvaluation.unknown("Live run, wave or cash is unverified", facts)
    if facts.wave < 1 or facts.battle_cash < 0 or facts.observed_at is None or facts.now is None:
        return RouteEvaluation.unknown("Battle evidence is missing or invalid", facts)
    age = facts.now - facts.observed_at
    if age < 0 or age > 2:
        return RouteEvaluation.unknown("Battle evidence is stale", facts)
    if route.battle.mode != "phases":
        return RouteEvaluation.unknown("Current battle policy has no route phase", facts)
    branch, phase = select_battle_phase(route, facts)
    if branch is None:
        return RouteEvaluation.unknown("No fallback battle branch", facts)
    if phase is None:
        return RouteEvaluation(facts.account_id, route.revision, "blocked", None,
                               DecisionTrace(branch.id, "No phase covers this wave", age,
                                             branch_id=branch.id), facts.observed_at)
    ceiling = facts.battle_cash * phase.cash_spend_limit_pct // 100
    eligible: list[tuple[str, int, int]] = []
    rejected: list[str] = []
    for uid in phase.priority_ids:
        row = facts.upgrade_rows.get(uid, {})
        price = row.get("price")
        seen = row.get("observed_at")
        if (row.get("status") != "available" or type(price) is not int or price < 0
                or not isinstance(seen, (int, float)) or facts.now - seen > 2
                or seen > facts.now or row.get("value") is None):
            rejected.append(f"{uid}: current row unavailable")
            continue
        if price > ceiling or price > facts.battle_cash:
            rejected.append(f"{uid}: exceeds cash limit")
            continue
        eligible.append((uid, price, phase.weights.get(uid, 1)))
    if not eligible:
        return RouteEvaluation(facts.account_id, route.revision, "blocked", None,
                               DecisionTrace(phase.id, "No affordable observed row", age,
                                             "live battle rows", facts.variant, tuple(rejected),
                                             ceiling, branch.id, phase.id), facts.observed_at)
    if phase.emergency_survival and facts.battle_health is not None and facts.battle_max_health is not None and facts.battle_max_health > 0 and facts.battle_health / facts.battle_max_health < .75:
        survival = next((candidate for candidate in eligible
                         if candidate[0] in {"defense_absolute", "health", "thorns"}), None)
        if survival is not None:
            eligible.remove(survival)
            eligible.insert(0, survival)
    odds: dict[str, float] = {}
    gate: int | None = None
    chosen = eligible[0]
    selected_pending = None
    if phase.draw_chance_pct > 0:
        total = sum(weight for _, _, weight in eligible)
        odds = {uid: weight / total for uid, _, weight in eligible}
        visit_id = facts.visit_id or f"battle:{facts.run_id}"
        key = f"{facts.account_id}:{route.revision}:{visit_id}:{phase.id}:{facts.decision_sequence}".encode()
        gate = int.from_bytes(hashlib.sha256(key + b":gate").digest()[:8], "big") % 100
        if pending is not None and pending.account_id == facts.account_id and pending.revision == route.revision and pending.visit_id == visit_id:
            chosen = next((candidate for candidate in eligible if candidate[0] == pending.chosen_id), chosen)
            selected_pending = pending
        elif gate < phase.draw_chance_pct:
            roll = int.from_bytes(hashlib.sha256(key + b":candidate").digest()[:8], "big") % total
            for candidate in eligible:
                if roll < candidate[2]:
                    chosen = candidate
                    break
                roll -= candidate[2]
        if selected_pending is None:
            fingerprint = hashlib.sha256(repr(eligible).encode()).hexdigest()
            selected_pending = PendingDecision(facts.account_id, route.revision, visit_id,
                facts.decision_sequence, fingerprint, chosen[0], gate,
                {uid: weight for uid, _, weight in eligible}, phase.id)
    upgrade = upgrades.by_id(chosen[0])
    assert upgrade is not None
    decision = BattleDecision(facts.account_id, "battle", "buy", chosen[0], upgrade.name,
                              upgrade.category, chosen[1], facts.battle_cash,
                              f"{branch.id} → {phase.id}: observed row within cash budget")
    trace = DecisionTrace(phase.id, decision.reason, age, "live battle rows",
                          facts.variant, tuple(rejected), ceiling, branch.id, phase.id,
                          odds, gate)
    return RouteEvaluation(facts.account_id, route.revision, "observed", decision,
                           trace, facts.observed_at, selected_pending)


def select_battle_phase(route: EffectiveRoute, facts: RouteFacts
                        ) -> tuple[BattleBranch | None, BattlePhase | None]:
    branches = [branch for branch in route.battle.branches
                if branch.min_best_tier_1_wave is None or
                (facts.best_tier_1_wave is not None and
                 facts.best_tier_1_wave >= branch.min_best_tier_1_wave)]
    branch = max(branches, key=lambda candidate: candidate.min_best_tier_1_wave or -1,
                 default=None)
    if branch is None:
        return None, None
    phase = next((entry for entry in branch.phases
                  if entry.start_wave <= facts.wave and
                  (entry.end_wave is None or facts.wave <= entry.end_wave)), None)
    return branch, phase


def evaluate_workshop(route: EffectiveRoute, facts: RouteFacts,
                      pending: PendingDecision | None) -> RouteEvaluation:
    """Apply hard bans and wallet limits before any Workshop recommendation."""
    if route.workshop.mode == "blocks":
        from fleet.strategy_blocks import evaluate_program
        return evaluate_program(route, facts, pending, "workshop")
    if not facts.account_id or not facts.worker:
        return RouteEvaluation.unknown("Verified account or worker identity is missing", facts)
    if facts.observed_at is None or facts.now is None:
        return RouteEvaluation.unknown("Evidence time is unknown", facts)
    age = facts.now - facts.observed_at
    if age < 0 or age > EVIDENCE_MAX_AGE_SECONDS:
        return RouteEvaluation.unknown("Account evidence is stale", facts)
    if facts.wallet_coins is None or facts.wallet_coins < 0:
        return RouteEvaluation.unknown("Workshop wallet is unknown", facts)
    jar = facts.lab_coin_jar
    spend_ceiling = workshop_ceiling(route, facts.wallet_coins, jar)
    limit = workshop_limit_pct(route)
    edited = route.workshop.mode == "priorities"
    if edited:
        candidates = _weighted_candidates(route, facts, spend_ceiling)
        first_candidate = (route.workshop.priority_ids.index(candidates[0][0])
                           if candidates else len(route.workshop.priority_ids))
        missing = _unpriced_priorities(route, facts, first_candidate)
        if missing:
            upgrade = upgrades.by_id(missing[0])
            assert upgrade is not None
            reason = "Read higher-priority Workshop prices before spending coins"
            decision = RerollDecision(facts.account_id, "strategy_observe", "Follow route priorities",
                                      "observe_price", upgrade.id, upgrade.name, upgrade.category,
                                      None, facts.wallet_coins, facts.lifetime_coins, reason)
            trace = DecisionTrace(route.workshop.id, reason, age, "unknown", facts.variant,
                                  spend_ceiling=spend_ceiling, observation_ids=tuple(missing))
            return RouteEvaluation(facts.account_id, route.revision, "projected", decision,
                                   trace, facts.observed_at)
        if not candidates:
            reason = "No affordable upgrade remains in the Workshop priority list"
            trace = DecisionTrace(route.workshop.id, reason, age, "worker price evidence",
                                  facts.variant, spend_ceiling=spend_ceiling)
            return RouteEvaluation(facts.account_id, route.revision, "blocked", None,
                                   trace, facts.observed_at)
        chosen = candidates[0][0]
        upgrade = upgrades.by_id(chosen)
        assert upgrade is not None
        decision = RerollDecision(facts.account_id, "strategy", "Follow route priorities", "buy",
                                  chosen, upgrade.name, upgrade.category, facts.prices[chosen],
                                  facts.wallet_coins, facts.lifetime_coins,
                                  f"First affordable Workshop priority: {upgrade.name}")
    else:
        decision = choose_next(RerollFacts(
            facts.account_id, facts.best_tier_1_wave, facts.purchases,
            facts.values, spendable_wallet(facts.wallet_coins, jar), facts.lifetime_coins,
            facts.prices,
            spend_fraction=(limit / 100 if limit < 100 else None),
            draw_sharpness=DRAW_SHARPNESS,
            variant=facts.variant,
            utility_spent_coins=facts.utility_spent_coins,
        ), banned_upgrade_ids=route.workshop.banned_upgrade_ids)
    selected_pending: PendingDecision | None = None
    draw_gate: int | None = None
    odds: dict[str, float] = {}
    if edited and route.workshop.draw_chance_pct > 0:
        if candidates:
            total = sum(weight for _, weight in candidates)
            odds = {uid: weight / total for uid, weight in candidates}
            visit_id = facts.visit_id or f"workshop:{facts.account_id}"
            key = f"{facts.account_id}:{route.revision}:{visit_id}:{facts.decision_sequence}".encode()
            draw_gate = int.from_bytes(hashlib.sha256(key + b":gate").digest()[:8], "big") % 100
            chosen = decision.upgrade_id
            matching_pending = (pending is not None and pending.account_id == facts.account_id
                                and pending.revision == route.revision and pending.visit_id == visit_id
                                and pending.sequence == facts.decision_sequence)
            if matching_pending:
                chosen = pending.chosen_id
                if chosen not in odds:
                    trace = DecisionTrace(route.workshop.id,
                                          "Pending choice became unavailable; abandon it explicitly",
                                          age, "unknown", facts.variant,
                                          (f"{chosen}: pending choice unavailable",), spend_ceiling,
                                          eligible_odds=odds, draw_gate=draw_gate)
                    return RouteEvaluation(facts.account_id, route.revision, "blocked", None,
                                           trace, facts.observed_at, pending)
                selected_pending = pending
            else:
                if draw_gate < route.workshop.draw_chance_pct:
                    roll = int.from_bytes(hashlib.sha256(key + b":candidate").digest()[:8], "big") % total
                    for uid, weight in candidates:
                        if roll < weight:
                            chosen = uid
                            break
                        roll -= weight
                if chosen not in odds:
                    chosen = candidates[0][0]
                fingerprint = hashlib.sha256(repr(candidates).encode()).hexdigest()
                selected_pending = PendingDecision(
                    facts.account_id, route.revision, visit_id,
                    facts.decision_sequence, fingerprint, chosen,
                    draw_gate, dict(candidates), route.workshop.id)
            if chosen is not None and chosen != decision.upgrade_id:
                upgrade = upgrades.by_id(chosen)
                assert upgrade is not None
                decision = RerollDecision(
                    facts.account_id, decision.stage, decision.goal, "buy", chosen,
                    upgrade.name, upgrade.category, facts.prices[chosen], facts.wallet_coins,
                    facts.lifetime_coins,
                    f"Weighted route selected {upgrade.name} at {odds[chosen]:.0%} eligible odds.")
    price_source = ("worker price evidence" if decision.upgrade_id in facts.prices
                    else "catalog estimate" if decision.price is not None else "unknown")
    status = ("blocked" if decision.state in {"needs_operator", "save_coins"} else
              "observed" if facts.screen == "workshop" and
              price_source == "worker price evidence" else "projected")
    rejected = [f"{uid}: blocked by Never Buy" for uid in sorted(route.workshop.banned_upgrade_ids)]
    rejected.extend(
        f"{child}: blocked by Never Buy ({upgrade.id})"
        for upgrade in upgrades.CATALOG if upgrade.unlock and upgrade.id in route.workshop.banned_upgrade_ids
        for child in upgrade.unlocks
    )
    trace = DecisionTrace(route.workshop.id, decision.reason, age,
                          price_source, facts.variant, tuple(rejected), spend_ceiling,
                          eligible_odds=odds, draw_gate=draw_gate)
    return RouteEvaluation(facts.account_id, route.revision, status, decision,
                           trace, facts.observed_at, selected_pending)


def _weighted_candidates(route: EffectiveRoute, facts: RouteFacts,
                         spend_ceiling: int) -> list[tuple[str, int]]:
    """Priced, affordable, unlocked route priorities in their listed order."""
    excluded = _ban_closure(route.workshop.banned_upgrade_ids)
    owned = {uid for uid, count in facts.purchases.items() if count > 0}
    groups = _owned_groups(facts.purchases, facts.values)
    prerequisites = builds.prerequisites()
    candidates: list[tuple[str, int]] = []
    for uid in route.workshop.priority_ids:
        if uid in excluded:
            continue
        if not available(uid, groups):
            continue
        gate = prerequisites.get(uid)
        if gate is not None and gate not in owned and gate not in groups:
            continue
        upgrade = upgrades.by_id(uid)
        if upgrade is None or (upgrade.unlock and uid in owned):
            continue
        price = facts.prices.get(uid)
        if type(price) is not int or price < 0 or price > spend_ceiling or price > facts.wallet_coins:
            continue
        candidates.append((uid, route.workshop.weights.get(uid, 1)))
    return candidates


def _unpriced_priorities(route: EffectiveRoute, facts: RouteFacts,
                         before: int) -> list[str]:
    """Read unknown higher priorities before treating them as unaffordable."""
    excluded = _ban_closure(route.workshop.banned_upgrade_ids)
    owned = {uid for uid, count in facts.purchases.items() if count > 0}
    groups = _owned_groups(facts.purchases, facts.values)
    prerequisites = builds.prerequisites()
    missing = []
    for uid in route.workshop.priority_ids[:before]:
        upgrade = upgrades.by_id(uid)
        gate = prerequisites.get(uid)
        if (uid in excluded or not available(uid, groups)
                or (gate is not None and gate not in owned and gate not in groups)
                or upgrade is None or (upgrade.unlock and uid in owned)):
            continue
        price = facts.prices.get(uid)
        evidence = facts.price_evidence.get(uid)
        source = evidence.get("source") if evidence is not None else None
        observed_at = evidence.get("observed_at") if evidence is not None else None
        verified = (source == "observed" and type(observed_at) in {int, float}
                    and 0 <= observed_at <= facts.now)
        fixed_unlock = source == "catalog_estimate" and upgrade.unlock
        if type(price) is not int or price < 0 or not (verified or fixed_unlock):
            missing.append(uid)
    return missing
