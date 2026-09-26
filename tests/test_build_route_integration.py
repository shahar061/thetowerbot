"""A published route changes a verified worker without a process restart."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import db
from account_state import AccountState
from fleet.build_route import RouteDocument, RouteRules
from fleet.build_route_eval import RouteFacts
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.build_route_store import BuildRouteStore
from fleet.reroll_progress import RerollProgress
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
    progress._history = lambda: (1, {})  # type: ignore[method-assign]
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


def _rules_route(root: Path, rules: dict, expected: int = 0) -> RouteDocument:
    raw = RouteDocument.compatibility().to_dict()
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
    progress.route_facts = _facts("visit-2")  # type: ignore[method-assign]
    progress.shopping_policy(base)
    assert progress._route_evaluation.trace.spend_ceiling == 640
    progress.note_lab_coin_debit()
    assert progress.coin_jar.amount() == 0


def test_the_jar_lowers_the_live_workshop_visit_budget(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "save_pct", "pct": 20}}})
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
    progress.note_lab_slot2("owned", 200)
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    base = replace(base, enabled=True, workshop=(), cards=replace(base.cards, enabled=True))
    assert progress.shopping_policy(base).coin_budget == 50  # the tutorial grant is never skipped
    _bought_once(progress)
    paused = progress.shopping_policy(base)
    # Only Workshop waits; card gem buys continue in the same visit.
    assert paused.enabled and paused.workshop == () and paused.cards.enabled
    progress.note_lab_observation(LabDecision("wait_running", job_completes_at=time.time() + 3600,
                                              game_speed_level=2))
    progress.workshop_worthwhile = lambda: True  # type: ignore[method-assign]
    assert progress.shopping_policy(base).workshop != ()


def test_paused_workshop_publishes_a_paused_plan_not_the_unrunnable_buy(tmp_path: Path) -> None:
    """While labs_first pauses Workshop, the published reroll-plan.json must
    say so - not describe a buy that `workshop=()` guarantees never runs."""
    progress = _progress(tmp_path)
    published = []
    progress._publish = lambda decision: published.append(decision)  # type: ignore[method-assign]
    _rules_route(tmp_path, {"coins": {"lab_share": {"mode": "labs_first"}}})
    _game_speed_waits(progress)
    progress.note_lab_slot2("owned", 200)
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
    progress.note_lab_slot2("owned", 200, now=1000.)
    progress.route_facts = _facts("visit-1")  # type: ignore[method-assign]
    base = Strategy.from_config().shopping
    base = replace(base, cards=replace(base.cards, enabled=True, gem_floor=0))
    assert progress.shopping_policy(base).cards.gem_floor == 60


def test_auto_start_and_auto_unlock_switches_gate_the_lab_visit(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    progress.note_lab_unlocked("labs_tab", now=999.)
    progress.note_lab_observation(LabDecision("wait_coins", price=300, wallet_coins=100,
                                              game_speed_level=1), now=1000.)
    progress.note_lab_slot2("locked", 65, now=1000.)
    assert progress.lab_visit_options() == LabVisitOptions()
    _rules_route(tmp_path, {"labs": {"auto_start": False}, "gems": {"auto_unlock_lab_slots": False}})
    assert not progress.lab_due(now=1100., wallet_coins=5000, wallet_gems=500)
    assert progress.lab_visit_options() == LabVisitOptions(start_research=False, unlock_slot2=False)
    _rules_route(tmp_path, {"gems": {"keep": 50}}, expected=1)
    assert not progress.lab_due(now=1100., wallet_coins=100, wallet_gems=120)
    assert progress.lab_due(now=1100., wallet_coins=100, wallet_gems=150)
    assert progress.lab_visit_options().min_gems == 150
