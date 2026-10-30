from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import config
from account_state import AccountState
from fleet.build_route import RouteDocument
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.build_route_store import BuildRouteStore, RouteUnavailable
from fleet.reroll_progress import RerollProgress
from policy import AutopilotPolicy, PolicyError
from tests.test_autopilot import parts
from tests.test_build_route_integration import _registered


def progress_at(root: Path) -> RerollProgress:
    worker_root = _registered(root, "Air_38", "account-a")
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["battle"] = {"mode": "phases", "branches": [
        {"id": "fallback", "min_best_tier_1_wave": None, "phases": [
            {"id": "all", "start_wave": 1, "end_wave": None,
             "priority_ids": ["health"]}]}]}
    BuildRouteStore(root).publish(RouteDocument.from_dict(raw), 0, "operator")
    progress = RerollProgress(worker_root, "account-a", AccountState())
    progress.route_runtime = BuildRouteRuntime(root, "Air_38", "account-a")
    return progress


def test_runtime_hold_is_waiting_while_user_disabled_is_off() -> None:
    bot, device, frame, observation, policy = parts()
    held = replace(policy, enabled=False, hold_reason="Waiting for a readable wave")
    assert not bot.step(frame, device, held, cash=100, observation=observation)
    assert bot.state.snapshot()["phase"] == "waiting"
    assert bot.state.snapshot()["reason"] == held.hold_reason
    assert device.actions == []
    assert not bot.step(frame, device, replace(policy, enabled=False), cash=100,
                        observation=replace(observation, observed_at=101))
    assert bot.state.snapshot()["reason"] == "Autopilot is off"


def test_runtime_hold_reason_cannot_be_persisted() -> None:
    policy = AutopilotPolicy(hold_reason="Waiting for readable battle cash")
    assert "hold_reason" not in policy.to_dict()
    with pytest.raises(PolicyError, match="unknown autopilot field"):
        AutopilotPolicy.from_dict({"hold_reason": policy.hold_reason})


@pytest.mark.parametrize("missing,reason", [
    ("wave", "Waiting for a readable wave"),
    ("cash", "Waiting for readable battle cash"),
    ("run_id", "Waiting for a verified run"),
])
@pytest.mark.parametrize("push_tier", [None, 2])
def test_missing_battle_facts_have_specific_hold_reason(
    tmp_path: Path, missing: str, reason: str, push_tier: int | None,
) -> None:
    progress = progress_at(tmp_path)
    facts = {"run_id": 7, "wave": 5, "cash": 100}
    facts[missing] = None
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), **facts, push_tier=push_tier)
    assert not policy.enabled
    assert policy.hold_reason == reason


def test_receipt_fence_has_runtime_hold_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    progress = progress_at(tmp_path)
    monkeypatch.setattr(config, "BATTLE_BURST_ENABLED", False)
    progress._battle_receipt_fence = (7, 1)
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), run_id=7, wave=5, cash=100)
    assert not policy.enabled
    assert policy.hold_reason == "Waiting for the confirmed purchase counter to refresh"


def test_account_binding_hold_remains_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    progress = progress_at(tmp_path)
    monkeypatch.setattr(progress, "_registration", lambda: None)
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), run_id=7, wave=5, cash=100)
    assert not policy.enabled
    assert policy.hold_reason == "Waiting for verified worker account binding"


def test_unavailable_route_reports_its_runtime_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    progress = progress_at(tmp_path)
    def unavailable() -> RouteDocument:
        raise RouteUnavailable("route unreadable")
    monkeypatch.setattr(progress.route_runtime, "current", unavailable)
    policy = progress.battle_policy(AutopilotPolicy(enabled=True), run_id=7, wave=5, cash=100)
    assert not policy.enabled
    assert policy.hold_reason == "Battle plan unavailable: route unreadable"
