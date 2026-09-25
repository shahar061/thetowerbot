"""A published route changes a verified worker without a process restart."""

from __future__ import annotations

import json
import time
from pathlib import Path

import db
from account_state import AccountState
from fleet.build_route import RouteDocument
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
