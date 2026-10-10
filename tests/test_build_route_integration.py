"""A published route changes a verified worker without a process restart."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import db
from account_state import AccountState
from fleet.build_route import RouteDocument, RouteRules
from fleet.build_route_eval import RouteFacts
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.build_route_store import BuildRouteStore
from fleet.reroll_progress import RerollProgress
from fleet.resource_blocks import template_gem_blocks
from lab_unlock_rollout import LabUnlockRollout
from strategy import Strategy
from policy import AutopilotPolicy


def _registered(root: Path, worker: str, account: str) -> Path:
    worker_root = root / "workers" / worker
    checkpoint = worker_root / "checkpoints" / ("a" * 32 + ".json")
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"worker_id": worker, "account_id": account,
                                      "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    (worker_root / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": worker, "account_id": account,
        "web_port": 8001, "binding": str(checkpoint),
        "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    db.bind_account(worker_root / "tower_bot.db", account)
    return worker_root


def _published(root: Path, expected: int) -> RouteDocument:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update({
        "mode": "priorities", "priority_ids": ["attack_speed", "damage"],
        "banned_upgrade_ids": ["damage"],
    })
    return BuildRouteStore(root).publish(RouteDocument.from_dict(raw), expected, "operator")


def test_new_revision_changes_next_buy_and_acknowledges_only_active_worker(tmp_path: Path) -> None:
    first_root = _registered(tmp_path, "Air_38", "account-a")
    _registered(tmp_path, "Air_39", "account-b")
    first = RerollProgress(first_root, "account-a", AccountState())
    first.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    first._publish = lambda decision: None  # type: ignore[method-assign]
    now = time.time()
    first.route_facts = lambda: RouteFacts("account-a", "Air_38", "main_menu", now, now,
        best_tier_1_wave=1, wallet_coins=100, prices={"damage": 10, "attack_speed": 12},
        utility_spent_coins=0, visit_id="visit-1")  # type: ignore[method-assign]
    saved = _published(tmp_path, 0)

    policy = first.shopping_policy(Strategy.from_config().shopping)

    assert policy.workshop[0].name == "Attack Speed"
    assert first.route_runtime.applied_revision() == saved.revision
    assert BuildRouteRuntime(tmp_path, "Air_39", "account-b").applied_revision() is None
    snapshot = json.loads((first_root / "build-route-facts.json").read_text())
    assert snapshot["account_id"] == "account-a"
    newer = BuildRouteStore(tmp_path).publish(saved, 1, "operator")
    assert newer.revision == 2
    assert first.route_changed_since_policy()
    assert first.route_runtime.applied_revision() == 1


def test_account_swap_and_corrupt_route_fail_closed(tmp_path: Path) -> None:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    _published(tmp_path, 0)
    record = json.loads((worker_root / "fleet-registration.json").read_text())
    record["account_id"] = "account-b"
    (worker_root / "fleet-registration.json").write_text(json.dumps(record))
    policy = progress.shopping_policy(Strategy.from_config().shopping)
    assert not policy.enabled
    assert progress.stop_reason == "Worker account binding changed"
    assert progress.route_runtime.applied_revision() is None
    (tmp_path / "build-route.json").write_text("{bad")
    assert not progress.shopping_policy(Strategy.from_config().shopping).enabled


def test_battle_phase_reaches_existing_autopilot_with_cash_cap(tmp_path: Path) -> None:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["battle"] = {"mode": "phases", "branches": [
        {"id": "fallback", "min_best_tier_1_wave": None, "phases": [
            {"id": "all", "start_wave": 1, "end_wave": None,
             "priority_ids": ["defense_absolute"]}]},
        {"id": "economy", "min_best_tier_1_wave": 50, "phases": [
            {"id": "first_ten", "start_wave": 1, "end_wave": 10,
             "priority_ids": ["cash_per_wave", "defense_absolute"],
             "cash_spend_limit_pct": 30, "emergency_survival": False}]},
    ]}
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "operator")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress._history = lambda: (50, {})  # type: ignore[method-assign]
    rows = {"cash_per_wave": {"status": "available", "value": 1,
                              "price": 20, "observed_at": time.time()}}
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), rows,
                                    run_id=7, wave=5, cash=100)
    assert policy.rules[0].upgrade_id == "cash_per_wave"
    assert policy.cash_spend_limit_pct == 30
    assert json.loads((worker_root / "build-route-battle.json").read_text())["trace"]["phase_id"] == "first_ten"
    assert progress.route_runtime.applied_revision() == 1


def test_battle_policy_observes_but_does_not_buy_without_route_decision(tmp_path: Path) -> None:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["battle"] = {"mode": "phases", "branches": [
        {"id": "fallback", "min_best_tier_1_wave": None, "phases": [
            {"id": "all", "start_wave": 1, "end_wave": None,
             "priority_ids": ["cash_per_wave", "defense_absolute"]}]}]}
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "operator")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress._history = lambda: (1, {"unlock_cash_bonuses": 1,  # type: ignore[method-assign]
                                     "unlock_defense_upgrades": 1})
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), {},
                                    run_id=7, wave=5, cash=100)
    assert policy.enabled and policy.observe_only
    assert [rule.upgrade_id for rule in policy.rules] == ["cash_per_wave", "defense_absolute"]


def test_battle_policy_only_allows_the_evaluated_upgrade(tmp_path: Path) -> None:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["battle"] = {"mode": "phases", "branches": [
        {"id": "fallback", "min_best_tier_1_wave": None, "phases": [
            {"id": "all", "start_wave": 1, "end_wave": None,
             "priority_ids": ["cash_per_wave", "defense_absolute"]}]}]}
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "operator")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress._history = lambda: (1, {})  # type: ignore[method-assign]
    now = time.time()
    rows = {"cash_per_wave": {"status": "available", "value": 1,
                               "price": 5, "observed_at": now},
            "defense_absolute": {"status": "available", "value": 1,
                                 "price": 5, "observed_at": now}}
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), rows,
                                    run_id=7, wave=5, cash=100)
    assert not policy.observe_only
    assert [rule.upgrade_id for rule in policy.rules] == ["cash_per_wave"]


def test_battle_blocks_send_autopilot_to_the_stale_priority_row(tmp_path: Path) -> None:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["battle"] = {"mode": "blocks", "blocks": [
        {"id": "eco", "type": "pool", "upgrade_ids": ["cash_bonus"], "selection": "priority"},
        {"id": "hp", "type": "pool", "upgrade_ids": ["health"], "selection": "priority"}]}
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "operator")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress._history = lambda: (1, {"unlock_cash_bonuses": 1})  # type: ignore[method-assign]
    now = time.time()
    rows = {"cash_bonus": {"status": "unknown", "value": None, "price": None, "observed_at": now - 90},
            "health": {"status": "available", "value": 1, "price": 5, "observed_at": now}}
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), rows,
                                    run_id=7, wave=5, cash=100)
    assert policy.observe_only
    assert [rule.upgrade_id for rule in policy.rules] == ["cash_bonus"]


def _battle_blocks(tmp_path: Path, blocks: list[dict]) -> RerollProgress:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["battle"] = {"mode": "blocks", "blocks": blocks}
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "operator")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress._history = lambda: (1, {})  # type: ignore[method-assign]
    return progress


def test_battle_blocks_skip_rows_still_locked_in_the_workshop(tmp_path: Path) -> None:
    # Defense Absolute and Defense % are only drawn in battle once Unlock
    # Defense Upgrades is bought. Asking for them sent the autopilot scrolling
    # for rows that never appear, and the coverage condition paused the whole
    # program on their missing values, so the run bought nothing at all.
    progress = _battle_blocks(tmp_path, [
        {"id": "emerg", "type": "condition", "field": "def_abs_coverage", "op": "lt", "value": 1.2,
         "then": [{"id": "emerg.buy", "type": "pool", "selection": "priority",
                   "upgrade_ids": ["defense_absolute"]}], "else": []},
        {"id": "core", "type": "pool", "selection": "priority",
         "upgrade_ids": ["defense_absolute", "damage"]}])
    now = time.time()
    rows = {"damage": {"status": "available", "value": 1, "price": 5, "observed_at": now},
            "defense_absolute": {"status": "unknown", "value": None, "price": None, "observed_at": now}}
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), rows, run_id=7, wave=5, cash=100)
    assert not policy.observe_only
    assert [rule.upgrade_id for rule in policy.rules] == ["damage"]


def test_battle_blocks_never_observe_rows_still_locked_in_the_workshop(tmp_path: Path) -> None:
    progress = _battle_blocks(tmp_path, [
        {"id": "thorns", "type": "pool", "selection": "priority", "upgrade_ids": ["thorns", "health"]},
        {"id": "end", "type": "wait"}])
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), {}, run_id=7, wave=5, cash=100)
    assert policy.observe_only
    assert [rule.upgrade_id for rule in policy.rules] == ["health"]
    progress._history = lambda: (1, {"unlock_thorns": 1})  # type: ignore[method-assign]
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), {}, run_id=7, wave=5, cash=100)
    assert [rule.upgrade_id for rule in policy.rules] == ["thorns", "health"]


def test_worker_projection_respects_published_never_buy(tmp_path: Path) -> None:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update({
        "mode": "priorities", "priority_ids": ["damage", "attack_speed"],
        "banned_upgrade_ids": ["damage"],
    })
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "operator")
    progress._history = lambda: (1, {})  # type: ignore[method-assign]
    progress._account_readings = lambda: ({}, None)  # type: ignore[method-assign]
    progress._publish(progress.decision())
    payload = json.loads((worker_root / "reroll-plan.json").read_text())
    assert all(step["upgrade_id"] != "damage" for step in payload["next_purchases"])


def test_first_menu_wallet_bootstraps_route_without_prior_workshop_visit(tmp_path: Path) -> None:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress.note_menu_wallet(75)
    facts = progress.route_facts()
    assert facts.wallet_coins == 75
    assert facts.observed_at is not None


def test_run_payouts_reopen_a_workshop_visit_the_cached_route_wallet_refused(tmp_path: Path) -> None:
    # The route decision is cached from the last menu visit. Without the run
    # projection the worker retries forever, never reaching the menu that
    # would refresh its wallet.
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress._publish = lambda decision: None  # type: ignore[method-assign]
    progress._utility_spent = lambda: 0  # type: ignore[method-assign]
    now = time.time()
    progress.route_facts = lambda: RouteFacts("account-a", "Air_38", "main_menu", now, now,
        best_tier_1_wave=1, wallet_coins=5, prices={"damage": 10, "attack_speed": 12},
        utility_spent_coins=0, visit_id="visit-1")  # type: ignore[method-assign]
    _published(tmp_path, 0)
    progress.shopping_policy(Strategy.from_config().shopping)
    evaluation = progress._route_evaluation
    assert evaluation is not None and evaluation.decision is not None
    progress._route_evaluation = replace(evaluation, decision=replace(
        evaluation.decision, state="save_coins", price=12, wallet_coins=5))
    progress.observe_prices({evaluation.decision.upgrade_id: 12}, 5)
    assert not progress.workshop_worthwhile()

    ended_at = time.time() + 1
    with db.connect(worker_root / "tower_bot.db") as connection:
        db.finish_run(connection, 1, started_at=ended_at - .5, ended_at=ended_at, wave=5, coins=10,
                      tier=1, abandoned=False, scan_count=0, tap_count=0)

    assert progress.workshop_worthwhile()


def test_a_waiting_route_is_reevaluated_on_run_payouts_not_by_the_legacy_planner(tmp_path: Path) -> None:
    # A blocks program that reaches its wait block decides nothing at the
    # menu. The death screen then asked the legacy planner instead, which
    # named a row the strategy never plans (Health) and gated its visit on
    # that row, so the worker retried past the coins the strategy needed.
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    published = []
    progress._publish = published.append  # type: ignore[method-assign]
    progress._utility_spent = lambda: 0  # type: ignore[method-assign]
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update({"mode": "blocks", "blocks": [
        {"id": "p", "type": "pool", "selection": "priority", "upgrade_ids": ["attack_speed"]},
        {"id": "w", "type": "wait"}]})
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "operator")
    progress.observe_prices({"attack_speed": 12}, 5)
    progress.note_menu_wallet(5)
    progress.shopping_policy(Strategy.from_config().shopping)
    evaluation = progress._route_evaluation
    assert evaluation is not None and evaluation.decision is None
    assert evaluation.trace.reason == "Wait block reached"
    published.clear()

    assert not progress.workshop_worthwhile(publish_estimate=True, detour=True)
    assert published == []

    ended_at = time.time() + 1
    with db.connect(worker_root / "tower_bot.db") as connection:
        db.finish_run(connection, 1, started_at=ended_at - .5, ended_at=ended_at, wave=5, coins=10,
                      tier=1, abandoned=False, scan_count=0, tap_count=0)

    assert progress.workshop_worthwhile(publish_estimate=True, detour=True)


def test_a_route_that_cannot_read_the_wallet_still_detours_home_to_read_it(tmp_path: Path) -> None:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress._publish = lambda decision: None  # type: ignore[method-assign]
    progress._utility_spent = lambda: 0  # type: ignore[method-assign]
    _published(tmp_path, 0)
    progress.shopping_policy(Strategy.from_config().shopping)
    evaluation = progress._route_evaluation
    assert evaluation is not None and evaluation.status == "unknown"

    for run_id in (1, 2):
        ended_at = time.time() + run_id
        with db.connect(worker_root / "tower_bot.db") as connection:
            db.finish_run(connection, run_id, started_at=ended_at - .5, ended_at=ended_at, wave=5,
                          coins=None, tier=1, abandoned=False, scan_count=0, tap_count=0)
        assert progress.workshop_worthwhile(detour=True) is (run_id == 1)


def test_a_skipped_visit_republishes_the_plan_with_the_run_payout_wallet(tmp_path: Path) -> None:
    # Retrying from GAME_OVER never reaches the menu that republishes the
    # plan, so the fleet card kept the last menu balance run after run.
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    published = []
    progress._publish = published.append  # type: ignore[method-assign]
    progress._utility_spent = lambda: 0  # type: ignore[method-assign]
    now = time.time()
    progress.route_facts = lambda: RouteFacts("account-a", "Air_38", "main_menu", now, now,
        best_tier_1_wave=1, wallet_coins=5, prices={"damage": 10, "attack_speed": 12},
        utility_spent_coins=0, visit_id="visit-1")  # type: ignore[method-assign]
    _published(tmp_path, 0)
    progress.shopping_policy(Strategy.from_config().shopping)
    evaluation = progress._route_evaluation
    assert evaluation is not None and evaluation.decision is not None
    progress._route_evaluation = replace(evaluation, decision=replace(
        evaluation.decision, state="save_coins", price=20, wallet_coins=5))
    progress.observe_prices({evaluation.decision.upgrade_id: 20}, 5)
    published.clear()

    assert not progress.workshop_worthwhile()
    assert published == [], "only the GAME_OVER caller may republish"

    ended_at = time.time() + 1
    with db.connect(worker_root / "tower_bot.db") as connection:
        db.finish_run(connection, 1, started_at=ended_at - .5, ended_at=ended_at, wave=5, coins=10,
                      tier=1, abandoned=False, scan_count=0, tap_count=0)

    assert not progress.workshop_worthwhile(publish_estimate=True)
    [plan] = published
    assert (plan.wallet_coins, plan.price, plan.state) == (15, 20, "save_coins")
    assert "15/20 coins" in plan.reason

    assert not progress.workshop_worthwhile(publish_estimate=True)
    assert len(published) == 1, "an unchanged estimate is not republished"


def test_a_weighted_draw_audit_row_does_not_erase_the_wallet(tmp_path: Path) -> None:
    # Confirming a weighted draw writes a ROUTE_DECISION ledger row with no
    # coin delta. Read as an unknown debit, it left the wallet None, so the
    # route refused every visit that no menu wallet read rescued.
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress.observe_prices({"damage": 10}, 580)
    with db.connect(worker_root / "tower_bot.db") as connection:
        connection.execute(
            "INSERT INTO ledger(ts,kind,item,category,currency,dry_run,reason) "
            "VALUES (?,'ROUTE_DECISION','Damage','ATTACK','coins',0,'draw')", (time.time() + 1,))
    assert progress.route_facts().wallet_coins == 580


def test_purchase_reason_names_a_random_draw_and_its_odds(tmp_path: Path) -> None:
    from fleet.build_route_eval import DecisionTrace, RouteEvaluation
    from fleet.reroll_planner import RerollDecision
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    decision = RerollDecision("account-a", "strategy", "goal", "buy", "coins_per_kill_bonus",
                              "Coins / Kill Bonus", "UTILITY", 126, 700, None, "Eligible pool")
    drawn = DecisionTrace("eco.stage1.pool", "Eligible pool",
                          eligible_odds={"coins_per_kill_bonus": .714, "cash_bonus": .286})
    progress._route_evaluation = RouteEvaluation("account-a", 1, "projected", decision, drawn, None)
    assert progress.purchase_reason("coins_per_kill_bonus") == "Random draw (71%) · Eligible pool"

    progress._route_evaluation = replace(progress._route_evaluation,
                                         trace=DecisionTrace("turtle.thorns", "Save for goal: buy Thorns"))
    assert progress.purchase_reason("coins_per_kill_bonus") == "Save for goal: buy Thorns"
    assert progress.purchase_reason("damage") is None


from lab_plan import LabDecision, LabVisitOptions


def _rules_route(root: Path, rules: dict, expected: int = 0,
                 priorities: bool = True) -> RouteDocument:
    raw = RouteDocument.compatibility().to_dict()
    if priorities:
        raw["baseline"]["workshop"].update({"mode": "priorities", "priority_ids": ["attack_speed", "damage"]})
    for section, values in rules.items():
        raw["baseline"]["rules"][section] = {**raw["baseline"]["rules"][section], **values}
    return BuildRouteStore(root).publish(RouteDocument.from_dict(raw), expected, "operator")


def _progress(tmp_path: Path) -> RerollProgress:
    worker_root = _registered(tmp_path, "Air_38", "account-a")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    progress._publish = lambda decision: None  # type: ignore[method-assign]
    return progress


def _facts(visit: str, wallet: int = 1000):
    now = time.time()
    return lambda: RouteFacts("account-a", "Air_38", "main_menu", now, now,
        best_tier_1_wave=1, wallet_coins=wallet, prices={"damage": 10, "attack_speed": 12},
        utility_spent_coins=0, visit_id=visit)


def _game_speed_waits(progress: RerollProgress, price: int = 2500) -> None:
    progress.note_lab_observation(LabDecision("wait_coins", price=price, wallet_coins=100,
                                              game_speed_level=2), now=time.time())


def _bought_once(progress: RerollProgress) -> None:
    with db.connect(progress.root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
                     "VALUES(1,'WORKSHOP_BUY','Damage','ATTACK','coins',-30,0,?)",
                     (json.dumps({"verdict": "bought"}),))


def test_a_route_without_rules_keeps_no_jar_and_the_whole_limit(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _rules_route(tmp_path, {})
    _game_speed_waits(progress)
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    progress.shopping_policy(Strategy.from_config().shopping)
    assert progress._route_evaluation.trace.spend_ceiling == 1000
    assert not (progress.root / "lab-coin-jar.json").exists()


def test_save_pct_holds_a_jar_that_workshop_cannot_spend(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "save_pct", "pct": 20}}})
    _game_speed_waits(progress)
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    progress.shopping_policy(base)
    assert progress._route_evaluation.trace.spend_ceiling == 800
    assert json.loads((progress.root / "lab-coin-jar.json").read_text())["amount"] == 200
    for _ in range(3):  # every main-menu scan recomputes the policy for the same visit
        progress.shopping_policy(base)
    assert progress._route_evaluation.trace.spend_ceiling == 800
    snapshot = json.loads((progress.root / "build-route-facts.json").read_text())
    assert snapshot["lab_coin_jar"] == 200
    progress.route_facts = _facts("visit-2", wallet=1500)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress._route_evaluation.trace.spend_ceiling == 1200  # 20% of the 500 earned
    progress.note_lab_coin_debit(2500)
    assert progress.coin_jar.amount() == 0


def test_save_pct_grows_across_runs_and_a_filler_spends_only_its_price(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "save_pct", "pct": 25}}})
    _game_speed_waits(progress, price=50_000)
    base = Strategy.from_config().shopping
    progress.route_facts = _facts("after-run:1", wallet=10_000)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 2_500
    assert progress._route_evaluation.trace.spend_ceiling == 7_500  # Workshop: wallet - jar
    progress.route_facts = _facts("after-run:1", wallet=2_500)  # type: ignore[method-assign]
    progress.shopping_policy(base)  # the scan after Workshop spent its 7,500
    progress.route_facts = _facts("after-run:2", wallet=12_500)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 5_000
    assert progress._route_evaluation.trace.spend_ceiling == 7_500
    progress.note_lab_coin_debit(71)  # a 71-coin filler started
    assert progress.coin_jar.amount() == 4_929
    progress.route_facts = _facts("after-run:2", wallet=12_429)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 4_929
    assert progress._route_evaluation.trace.spend_ceiling == 7_500


def test_the_jar_lowers_the_live_workshop_visit_budget(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    # The legacy planner's visit budget is the jar-lowered wallet ceiling. A
    # `priorities` route instead bounds each buy to its exact quote, which the
    # ceiling already admitted, so the jar never shows in that budget.
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "save_pct", "pct": 20}}},
                 priorities=False)
    _game_speed_waits(progress)
    _bought_once(progress)
    progress.workshop_worthwhile = lambda: True  # type: ignore[method-assign]
    progress.route_facts = _facts("visit-1", wallet=80)  # type: ignore[method-assign]
    base = replace(Strategy.from_config().shopping, coin_budget=None)
    # A starter buy is capped at 75 coins; the 16-coin jar leaves only 64 spendable.
    assert progress.shopping_policy(base).coin_budget == 64
    assert progress.coin_jar.amount() == 16


def test_labs_first_pauses_workshop_after_the_tutorial_grant(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "labs_first"}}})
    _game_speed_waits(progress)
    progress.note_lab_slots({2: "owned"}, 200)
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    base = replace(base, enabled=True, workshop=(), cards=replace(base.cards, enabled=True))
    assert progress.shopping_policy(base).coin_budget == 50  # the tutorial grant is never skipped
    _bought_once(progress)
    paused = progress.shopping_policy(base)
    # Only Workshop waits; card gem buys continue in the same visit.
    assert paused.enabled and paused.workshop == () and paused.cards.enabled
    assert progress.stop_reason is not None and progress.stop_reason.startswith("Workshop paused")
    progress.note_lab_observation(LabDecision("wait_running", job_completes_at=time.time() + 3600,
                                              game_speed_level=2))
    progress.workshop_worthwhile = lambda: True  # type: ignore[method-assign]
    assert progress.shopping_policy(base).workshop != ()
    assert progress.stop_reason is None


def test_paused_workshop_publishes_a_paused_plan_not_the_unrunnable_buy(tmp_path: Path) -> None:
    """While labs_first pauses Workshop, the published reroll-plan.json must
    say so - not describe a buy that `workshop=()` guarantees never runs."""
    progress = _progress(tmp_path)
    published = []
    progress._publish = lambda decision: published.append(decision)  # type: ignore[method-assign]
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "labs_first"}}})
    _game_speed_waits(progress)
    progress.note_lab_slots({2: "owned"}, 200)
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    base = replace(base, enabled=True, workshop=(), cards=replace(base.cards, enabled=True))
    progress.shopping_policy(base)  # the tutorial grant visit; not paused yet
    _bought_once(progress)
    published.clear()
    paused = progress.shopping_policy(base)
    assert paused.workshop == ()
    assert len(published) == 1
    decision = published[0]
    assert decision.state == "save_coins"
    assert "paused" in decision.reason.lower()
    assert "lab" in decision.reason.lower()


def test_resource_rules_falls_back_to_defaults_when_resolve_route_breaks(
        tmp_path: Path, monkeypatch) -> None:
    """A bad account override must not escape into the live main-menu loop;
    resource_rules fails closed to today's defaults, matching the existing
    RouteUnavailable fallback."""
    progress = _progress(tmp_path)
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "labs_first"}}})
    assert progress.resource_rules().coins.lab_share.mode == "labs_first"

    def _boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr("fleet.reroll_progress.resolve_route", _boom)
    assert progress.resource_rules() == RouteRules()


def test_gems_keep_raises_the_card_gem_floor(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _rules_route(tmp_path, {"gems": {"keep": 60}})
    progress.note_lab_slots({2: "owned"}, 200, now=1000.)
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    base = replace(base, cards=replace(base.cards, enabled=True, gem_floor=0))
    assert progress.shopping_policy(base).cards.gem_floor == 60


def test_auto_start_and_auto_unlock_switches_gate_the_lab_visit(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    progress.note_lab_unlocked("labs_tab", now=999.)
    progress.note_lab_observation(LabDecision("wait_coins", price=300, wallet_coins=100,
                                              game_speed_level=1), now=1000.)
    progress.note_lab_slots({2: "locked"}, 65, now=1000.)
    assert progress.lab_visit_options() == LabVisitOptions(unlock_slots=(2,))
    _rules_route(tmp_path, {"labs": {"auto_start": False}, "gems": {"auto_unlock_lab_slots": False}})
    assert not progress.lab_due(now=1100., wallet_coins=5000, wallet_gems=500)
    assert progress.lab_visit_options() == LabVisitOptions(start_research=False)
    _rules_route(tmp_path, {"gems": {"keep": 50}}, expected=1)
    assert not progress.lab_due(now=1100., wallet_coins=100, wallet_gems=120)
    assert progress.lab_due(now=1100., wallet_coins=100, wallet_gems=150)
    assert progress.lab_visit_options() == LabVisitOptions(unlock_slots=(2,), keep_gems=50)


def test_a_failed_lab_visit_holds_a_standing_slot_unlock_check(tmp_path: Path) -> None:
    """Gems crossed the unlock price since the saved 84-gem read, so the unlock
    check stays due until a visit rereads the strip. A visit that failed without
    reading must not re-arm on every menu pass."""
    progress = _progress(tmp_path)
    progress.note_lab_unlocked("labs_tab", now=999.)
    progress.note_lab_observation(LabDecision("wait_coins", price=2500, wallet_coins=2610,
                                              game_speed_level=3), now=1000.)
    progress.note_lab_slots({2: "locked"}, 84, now=1000.)
    assert progress.lab_due(now=1100., wallet_coins=100, wallet_gems=100)
    progress.note_lab_failure(now=1100.)
    assert not progress.lab_due(now=1103., wallet_coins=100, wallet_gems=100)
    assert not progress.lab_due(now=1399., wallet_coins=5000, wallet_gems=100)
    assert progress.lab_due(now=1400., wallet_coins=100, wallet_gems=100)


def _gem_blocks_route(root: Path, expected: int = 0) -> RouteDocument:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["gems"].update(mode="blocks", blocks=list(template_gem_blocks()))
    return BuildRouteStore(root).publish(RouteDocument.from_dict(raw), expected, "operator")


def test_lab_three_is_the_visit_unlock_once_lab_two_is_owned(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _gem_blocks_route(tmp_path)
    progress.note_lab_unlocked("labs_tab", now=999.)
    _game_speed_waits(progress)
    progress.note_lab_slots({2: "owned", 3: "locked"}, 120, now=1000.)
    assert progress.lab_visit_options() == LabVisitOptions(unlock_slots=(3,), keep_gems=0)
    assert not progress.lab_due(now=1100., wallet_coins=100, wallet_gems=399)
    assert progress.lab_due(now=1100., wallet_coins=100, wallet_gems=400)


def _jit_route(root: Path, expected: int = 0) -> RouteDocument:
    from fleet import resource_blocks as rb
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update({"mode": "priorities", "priority_ids": ["attack_speed", "damage"]})
    raw["baseline"]["labs"].update(mode="blocks", blocks=list(rb.template_lab_list()))
    raw["baseline"]["rules"] = rb.template_lab_list_rules()
    raw["baseline"]["rules"]["coins"]["lab_share"] = {"mode": "just_in_time", "pct": 25}
    return BuildRouteStore(root).publish(RouteDocument.from_dict(raw), expected, "operator")


def test_just_in_time_ceiling_is_the_saving_plans_workshop_budget(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _jit_route(tmp_path)
    _game_speed_waits(progress)  # slot 1 idle; Game Speed L2 costs 2,500 and starts now
    (progress.root / "lab-coin-jar.json").write_text(json.dumps(
        {"account_id": "account-a", "amount": 700, "visit_key": "old", "updated_at": 1.0}))
    jar_before = (progress.root / "lab-coin-jar.json").read_bytes()
    progress.route_facts = _facts("visit-1", wallet=3000)  # type: ignore[method-assign]
    progress.shopping_policy(Strategy.from_config().shopping)
    from fleet.build_route import resolve_route
    from fleet.lab_facts import persisted_lab_facts
    from fleet.resource_blocks import evaluate_lab_plan
    saving = evaluate_lab_plan(resolve_route(BuildRouteStore(tmp_path).read(), "Air_38", "account-a"),
                               persisted_lab_facts(progress.root, "account-a", now=time.time(), coins=3000,
                                                   gems=None, db_path=progress.root / "tower_bot.db")).saving
    # Holding only the (zero) reserve would leave the whole 3,000 spendable.
    assert (saving.reserve, saving.workshop_budget) == (0, 500)
    assert progress._route_evaluation.trace.spend_ceiling == saving.workshop_budget
    assert json.loads((progress.root / "build-route-facts.json").read_text())["lab_coin_jar"] == 2500
    # The leftover save_pct jar is neither grown nor reset in this mode.
    assert (progress.root / "lab-coin-jar.json").read_bytes() == jar_before


def test_just_in_time_pauses_workshop_when_the_reserve_takes_the_wallet(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    published = []
    progress._publish = lambda decision: published.append(decision)  # type: ignore[method-assign]
    _jit_route(tmp_path)
    _game_speed_waits(progress)
    _bought_once(progress)
    progress.route_facts = _facts("visit-1", wallet=1000)  # type: ignore[method-assign]
    paused = progress.shopping_policy(replace(Strategy.from_config().shopping, enabled=True))
    assert paused.enabled and paused.workshop == ()
    assert [(d.state, d.reason) for d in published] == [(
        "save_coins", "Workshop paused: saving coins for labs · "
                      "Reserve 1k; Workshop may spend 0")]


def test_published_gem_step_names_the_canary(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _rules_route(tmp_path, {})
    progress.note_lab_slots({2: "locked"}, 150, now=time.time())
    rollout = LabUnlockRollout(tmp_path)
    rollout.note_dry_run(2, "Air_38", 100, 150, 0., account_id="account-a")
    rollout.note_dry_run(2, "Air_38", 100, 150, 700., account_id="account-a")
    progress.resource_evaluation(500, 150)
    published = json.loads((tmp_path / "workers" / "Air_38" / "build-route-resources.json").read_text())
    assert published["gem_step"]["reason"] == "Canary: Air_38 unlocks slot 2 next visit"
    facts = json.loads((tmp_path / "workers" / "Air_38" / "build-route-resource-facts.json").read_text())
    assert facts["lab_slot_status"] == {"2": "locked"}


def _save_pct_template_route(root: Path, expected: int = 0) -> RouteDocument:
    """The real lab-list template with its own rules (save_pct 10)."""
    from fleet import resource_blocks as rb
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update({"mode": "priorities", "priority_ids": ["attack_speed", "damage"]})
    raw["baseline"]["labs"].update(mode="blocks", blocks=list(rb.template_lab_list()))
    raw["baseline"]["rules"] = rb.template_lab_list_rules()
    assert raw["baseline"]["rules"]["coins"]["lab_share"] == {"mode": "save_pct", "pct": 10}
    return BuildRouteStore(root).publish(RouteDocument.from_dict(raw), expected, "operator")


def _game_speed_l3_finished_and_a_filler_runs(progress: RerollProgress) -> None:
    """Game Speed L3 started (wait_running), finished; slot 1 now runs a filler.

    The slot-1 cadence is never written wait_coins again: a filler start keeps it.
    """
    progress.note_lab_observation(LabDecision("wait_running", job_completes_at=time.time() - 60,
                                              game_speed_level=3), now=time.time() - 7200)
    progress.note_other_lab_research(now=time.time())


def _jar(progress: RerollProgress) -> dict:
    return json.loads((progress.root / "lab-coin-jar.json").read_text())


def test_save_pct_saves_toward_the_plans_game_speed_while_a_filler_runs(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _save_pct_template_route(tmp_path)
    _game_speed_l3_finished_and_a_filler_runs(progress)
    base = Strategy.from_config().shopping
    progress.route_facts = _facts("after-run:1", wallet=10_000)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 1_000
    assert _jar(progress)["target"] == {"lab_id": "labs.game-speed", "level": 4}
    progress.route_facts = _facts("after-run:1", wallet=1_000)  # type: ignore[method-assign]
    progress.shopping_policy(base)  # the scan after Workshop spent its 9,000
    progress.route_facts = _facts("after-run:2", wallet=11_000)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 2_000
    assert progress._route_evaluation.trace.spend_ceiling == 9_000
    # Capped at Game Speed L4's 50,000 from the plan, however rich the run.
    progress.route_facts = _facts("after-run:3", wallet=1_000_000)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 50_000


def test_a_transient_unknown_cadence_keeps_the_jar(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _save_pct_template_route(tmp_path)
    progress.note_lab_failure(now=time.time())  # no saved observation: the cadence reads "unknown"
    (progress.root / "lab-coin-jar.json").write_text(json.dumps(
        {"account_id": "account-a", "amount": 5_000, "visit_key": "after-run:1", "updated_at": 1.0,
         "target": {"lab_id": "labs.game-speed", "level": 4}}))
    progress.route_facts = _facts("after-run:2", wallet=12_000)  # type: ignore[method-assign]
    progress.shopping_policy(Strategy.from_config().shopping)
    assert progress.coin_jar.amount() == 5_000
    assert progress._route_evaluation.trace.spend_ceiling == 7_000


def test_a_multi_start_visit_leaves_no_phantom_jar(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _save_pct_template_route(tmp_path)
    _game_speed_l3_finished_and_a_filler_runs(progress)
    base = Strategy.from_config().shopping
    (progress.root / "lab-coin-jar.json").write_text(json.dumps(
        {"account_id": "account-a", "amount": 50_000, "visit_key": "after-run:5", "updated_at": 1.0,
         "target": {"lab_id": "labs.game-speed", "level": 4}}))
    # The effective jar never exceeds the wallet it reads; the stored one keeps its savings.
    progress.route_facts = _facts("after-run:5", wallet=10_000)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress._route_evaluation.trace.spend_ceiling == 0
    assert json.loads((progress.root / "build-route-facts.json").read_text())["lab_coin_jar"] == 10_000
    assert progress.coin_jar.amount() == 50_000
    (progress.root / "lab-coin-jar.json").write_text(json.dumps(
        {"account_id": "account-a", "amount": 50_000, "visit_key": "after-run:5", "updated_at": 1.0,
         "target": {"lab_id": "labs.game-speed", "level": 4}}))
    # One visit started Game Speed L4, then two fillers: every start is debited.
    for spent in (50_000, 71, 1_350):
        progress.note_lab_coin_debit(spent)
    progress.note_lab_observation(LabDecision("wait_running", job_completes_at=time.time() + 120_000,
                                              game_speed_level=4), now=time.time())
    progress.route_facts = _facts("after-run:5", wallet=79)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 0
    assert progress._route_evaluation.trace.spend_ceiling == 79
    # The next run saves toward L5; L4 is running, so its old target never caps the jar.
    progress.route_facts = _facts("after-run:6", wallet=10_079)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 1_000  # 10% of the 10,000 earned
    assert _jar(progress)["target"] == {"lab_id": "labs.game-speed", "level": 5}
    assert progress._route_evaluation.trace.spend_ceiling == 9_079


def test_a_plan_without_a_target_empties_a_retired_jar_and_ignores_the_cadence(
        tmp_path: Path, monkeypatch: Any) -> None:
    import fleet.reroll_progress as reroll_progress
    progress = _progress(tmp_path)
    _save_pct_template_route(tmp_path)
    _game_speed_waits(progress, price=2_500)  # a cadence the plan overrides
    (progress.root / "lab-coin-jar.json").write_text(json.dumps(
        {"account_id": "account-a", "amount": 5_000, "visit_key": "after-run:1", "updated_at": 1.0,
         "target": {"lab_id": "labs.game-speed", "level": 7}}))
    base = Strategy.from_config().shopping
    # The plan exists but names nothing for slot 1; Game Speed L7 is not retired yet.
    monkeypatch.setattr(reroll_progress, "save_pct_target", lambda *a, **k: (None, lambda lab, level: False))
    progress.route_facts = _facts("after-run:2", wallet=12_000)  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 5_000  # kept, not regrown toward the cadence's 2,500
    # Game Speed maxed: the residual jar no longer holds Workshop coins.
    monkeypatch.setattr(reroll_progress, "save_pct_target", lambda *a, **k: (None, lambda lab, level: True))
    progress.shopping_policy(base)
    assert progress.coin_jar.amount() == 0
    assert progress._route_evaluation.trace.spend_ceiling == 12_000


def test_a_planner_error_falls_back_to_the_cadence(tmp_path: Path, monkeypatch: Any) -> None:
    import fleet.reroll_progress as reroll_progress
    progress = _progress(tmp_path)
    _save_pct_template_route(tmp_path)
    _game_speed_waits(progress, price=2_500)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("odd persisted lab data")
    monkeypatch.setattr(reroll_progress, "save_pct_target", broken)
    progress.route_facts = _facts("after-run:1", wallet=1_000)  # type: ignore[method-assign]
    progress.shopping_policy(Strategy.from_config().shopping)
    assert progress.coin_jar.amount() == 100
    assert progress._route_evaluation.trace.spend_ceiling == 900


def _plan(progress: RerollProgress) -> dict[str, Any]:
    return json.loads((progress.root / "build-route-workshop.json").read_text(encoding="utf-8"))


def test_workshop_visit_saves_the_plan_with_its_budget(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    progress._publish = lambda decision: None  # type: ignore[method-assign]
    saved = _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "when_affordable"}}})
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    progress.shopping_policy(base)
    assert _plan(progress)["override"] == "tutorial"
    _bought_once(progress)
    progress.route_facts = _facts("visit-2")  # type: ignore[method-assign]
    progress.shopping_policy(base)
    record = _plan(progress)
    assert record["account_id"] == "account-a"
    assert record["revision"] == saved.revision
    assert record["override"] is None
    assert record["strategy"]["mode"] == "priorities"
    assert record["budget"]["wallet"] == 1000
    assert record["budget"]["spend_limit_pct"] == 100
    assert record["budget"]["jar_kind"] == "lab_jar"
    assert record["budget"]["ceiling"] == record["evaluation"]["trace"]["spend_ceiling"]
    assert record["evaluation"]["decision"]["upgrade_id"] == "attack_speed"
    assert progress.purchase_plan_id("attack_speed") == record["id"]
    assert progress.purchase_plan_id("health") is None
    assert record["visit_id"] == "visit-2"
    assert record["upgrade_names"]["attack_speed"] == "Attack Speed"


def test_paused_workshop_saves_the_paused_decision(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    progress._publish = lambda decision: None  # type: ignore[method-assign]
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "labs_first"}}})
    _game_speed_waits(progress)
    progress.note_lab_slots({2: "owned"}, 200)
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    base = replace(base, enabled=True, workshop=(), cards=replace(base.cards, enabled=True))
    progress.shopping_policy(base)
    _bought_once(progress)
    progress.shopping_policy(base)
    record = _plan(progress)
    assert record["override"] == "workshop_paused"
    assert record["evaluation"]["decision"]["state"] == "save_coins"
    assert record["evaluation"]["decision"]["item"] is None


def test_failed_plan_write_never_stops_shopping(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    progress._publish = lambda decision: None  # type: ignore[method-assign]
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "when_affordable"}}})
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    progress.shopping_policy(base)
    _bought_once(progress)

    def _disk_full(record: dict[str, Any]) -> None:
        raise OSError("disk full")

    progress.route_runtime.publish_workshop_plan = _disk_full  # type: ignore[method-assign]
    progress.route_facts = _facts("visit-2")  # type: ignore[method-assign]
    policy = progress.shopping_policy(base)
    assert policy.workshop != ()
    assert progress.stop_reason is None


def test_a_plan_build_error_never_stops_shopping(tmp_path: Path, monkeypatch: Any) -> None:
    from types import SimpleNamespace
    from fleet import coin_share, reroll_progress

    progress = _progress(tmp_path)
    progress._publish = lambda decision: None  # type: ignore[method-assign]
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "when_affordable"}}})
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    progress.shopping_policy(base)
    _bought_once(progress)

    def _broken(effective: Any) -> int:
        raise AttributeError("plan field missing")

    # Only the plan record reads this helper through the progress module.
    proxy = SimpleNamespace(**{name: getattr(coin_share, name) for name in dir(coin_share)
                               if not name.startswith("__")})
    proxy.workshop_limit_pct = _broken
    monkeypatch.setattr(reroll_progress, "coin_share", proxy)
    progress.route_facts = _facts("visit-2")  # type: ignore[method-assign]
    policy = progress.shopping_policy(base)
    assert policy.workshop != ()
    assert progress.stop_reason is None


def test_workshop_plan_rejects_another_account(tmp_path: Path) -> None:
    runtime = BuildRouteRuntime(tmp_path, "Air_38", "account-a")
    with pytest.raises(ValueError):
        runtime.publish_workshop_plan({"account_id": "account-b"})
