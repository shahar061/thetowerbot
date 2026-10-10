"""Periodic push scheduling survives restarts and cannot leak farm spending."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import config
import ocr
import tier_select
import vision
from policy import AutopilotPolicy, UpgradeRule, choose


def scheduler(path: Path | None = None, account: str = "account-a") -> Any:
    return importlib.import_module("push_runs").PushRuns(path, account, every=10)


def complete_farms(push: Any, count: int = 10) -> None:
    for run in range(1, count + 1):
        push.started(run, "farm")
        push.ended(abandoned=False)


def test_push_due_only_after_ten_completed_farms() -> None:
    push = scheduler()
    complete_farms(push, 9)
    assert not push.needs_home
    push.started(10, "farm")
    push.ended(abandoned=True)
    push.started(11, "milestone")
    push.ended(abandoned=False)
    assert push.snapshot()["farms_remaining"] == 1
    push.started(12, "farm")
    push.ended(abandoned=False)
    assert push.needs_home
    assert push.menu(2, True) == "next"
    assert push.menu(3, True) == "next"
    assert push.menu(4, False) == "start"
    push.started(13, "farm")
    assert push.active
    push.ended(abandoned=False)
    assert push.menu(4, False) == "previous"
    assert push.menu(3, True) == "previous"
    assert push.menu(2, True) == "start"
    assert push.snapshot()["farms_remaining"] == 10


def test_restart_keeps_push_and_returns_without_counting_it_as_farming(tmp_path: Path) -> None:
    path = tmp_path / "push.json"
    push = scheduler(path)
    complete_farms(push)
    push.menu(1, True)
    push = scheduler(path)
    assert push.menu(2, False) == "start"
    push.started(11, "farm")
    push = scheduler(path)
    push.started(12, "farm")  # RunTracker assigns a new ID to a resumed run.
    assert push.active
    push.ended(abandoned=False)
    push.ended(abandoned=False)  # Repeated terminal frames cannot count twice.
    push = scheduler(path)
    assert push.menu(2, False) == "previous"
    assert push.menu(1, True) == "start"
    assert push.snapshot()["farms_remaining"] == 10


def test_account_change_cannot_inherit_a_push(tmp_path: Path) -> None:
    path = tmp_path / "push.json"
    complete_farms(scheduler(path))
    other = scheduler(path, "account-b")
    assert not other.needs_home
    assert other.snapshot()["farms_remaining"] == 10


def test_unreadable_state_blocks_automatic_tier_changes(tmp_path: Path) -> None:
    path = tmp_path / "push.json"
    path.write_text("broken")
    push = scheduler(path)
    assert push.menu(2, True) == "hold"
    assert push.snapshot()["blocker"]


def test_push_policy_uses_combat_upgrades_and_preserves_safety_gates() -> None:
    module = importlib.import_module("push_runs")
    farm = AutopilotPolicy(enabled=True, rules=(UpgradeRule("cash_bonus"),))
    policy = module.push_policy(farm, tier=4)
    ids = {rule.upgrade_id for rule in policy.effective_rules()}
    assert {"health", "defense_percent", "thorns", "attack_speed", "lifesteal", "orbs"} <= ids
    assert not any("cash" in uid or "coin" in uid for uid in ids)
    assert "defense_absolute" not in ids
    assert "defense_absolute" in {rule.upgrade_id for rule in module.push_policy(farm, tier=1).rules}
    rows = {uid: {"value": 1, "price": 1, "status": "available"} for uid in ids | {"cash_bonus"}}
    assert choose(policy, rows, {"cash": 100, "wave": 1}).upgrade_id in ids
    assert not module.push_policy(AutopilotPolicy(enabled=False), tier=4).enabled
    assert module.push_policy(AutopilotPolicy(observe_only=True), tier=4).observe_only


def test_resume_and_abandoned_push_restore_farming_tier(tmp_path: Path) -> None:
    push = scheduler(tmp_path / "push.json")
    complete_farms(push)
    assert push.menu(1, False) == "start"  # Highest unlocked tier may be Tier 1.
    push.started(11, "farm")
    push = scheduler(tmp_path / "push.json")
    push.ended(abandoned=True)
    assert push.menu(1, False) == "start"
    assert not push.active


def test_tier_panel_reads_recorded_tier_and_both_arrow_states() -> None:
    root = Path(__file__).parent / "fixtures"
    frame = cv2.imread(str(root / "menu_main_events_badge_tier_next.png"))
    boxes = [ocr.TextBox(item["text"], item["confidence"], config.Rect(*item["rect"]))
             for item in json.loads((root / "ocr/menu_main_events_badge_tier_next.json").read_text())]
    panel = tier_select.read_panel(frame, vision.TemplateCache(config.TEMPLATE_DIR), boxes)
    assert panel is not None and panel.tier == 1
    assert panel.next.available
    assert panel.previous is not None and not panel.previous.available
    wrong = [ocr.TextBox("Tier 9", .99, config.Rect(450, 100, 120, 40))]
    assert tier_select.read_panel(frame, vision.TemplateCache(config.TEMPLATE_DIR), wrong) is None
    duplicate = boxes + [ocr.TextBox("Tier 2", .99, config.Rect(479, 1299, 123, 46))]
    assert tier_select.read_panel(frame, vision.TemplateCache(config.TEMPLATE_DIR), duplicate) is None


def test_bot_selects_then_restores_tier_and_suppresses_permanent_promotion(monkeypatch) -> None:
    import screens
    from strategy import Shopping
    from tests.conftest import _shopping_bot
    bot = _shopping_bot("menu_main_events_badge_tier_next", state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=True)
    complete_farms(bot.push_runs)
    monkeypatch.setattr("tower_bot.jitter.point", lambda x, y, *args: (x, y))
    arrow = tier_select.NextTier(True, (690, 1320), 1.)
    left = tier_select.NextTier(True, (390, 1320), 1.)
    monkeypatch.setattr(tier_select, "read_panel", lambda *args: tier_select.TierPanel(1, arrow, left))
    assert bot._advance_push(bot.controls.snapshot(), ())
    assert len(bot.device.taps) == 1
    bot._tier_tap_at = float("-inf")
    monkeypatch.setattr(tier_select, "read_panel", lambda *args: tier_select.TierPanel(
        2, tier_select.NextTier(False, arrow.point, 1.), left))
    assert not bot._advance_push(bot.controls.snapshot(), ())
    assert bot.push_runs.state.phase == "ready"
    bot.push_runs.started(11, "farm")
    assert bot.run_identity(bot.controls.snapshot()).purpose == "milestone"
    bot.push_runs.ended(abandoned=False)
    assert bot._advance_push(bot.controls.snapshot(), ())
    assert bot.device.taps[-1] == left.point


def test_bot_does_not_navigate_a_suspended_run(monkeypatch) -> None:
    import screens
    from strategy import Shopping
    from tests.conftest import _shopping_bot
    bot = _shopping_bot("main_menu_resume", state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=True)
    complete_farms(bot.push_runs)
    monkeypatch.setattr(tier_select, "read_panel", lambda *args: (_ for _ in ()).throw(AssertionError()))
    assert not bot._advance_push(bot.controls.snapshot(), ())
    assert not bot.device.taps


def test_push_overrides_route_economy_but_requires_account_binding(monkeypatch, tmp_path: Path) -> None:
    from fleet.reroll_progress import RerollProgress
    progress = RerollProgress.__new__(RerollProgress)
    progress.root = tmp_path
    progress.account_id = "account-a"
    progress.route_runtime = None
    progress.target_context = None
    progress._history = lambda: (1, {})
    registration = SimpleNamespace(account_id="account-a", db_path=tmp_path / "bot.db")
    monkeypatch.setattr("web.account_catalog.registered_worker", lambda root: registration)
    monkeypatch.setattr("db.bound_account", lambda path: "account-a")
    base = AutopilotPolicy(rules=(UpgradeRule("cash_bonus"),))
    result = progress.battle_policy(base, {}, push_tier=4, run_id=11, wave=1, cash=100)
    assert result.enabled
    assert "cash_bonus" not in {rule.upgrade_id for rule in result.rules}
    registration.account_id = "another-account"
    assert not progress.battle_policy(base, {}, push_tier=4, run_id=11, wave=1, cash=100).enabled


def test_death_after_start_tap_but_before_run_observation_still_returns(tmp_path: Path) -> None:
    path = tmp_path / "push.json"
    push = scheduler(path)
    complete_farms(push)
    push.menu(1, True)
    push.menu(2, False)
    push = scheduler(path)  # Crashed immediately after tapping BATTLE.
    push.ended(abandoned=False)
    assert push.menu(2, False) == "previous"


def test_push_policy_balances_price_and_prioritizes_urgent_health() -> None:
    module = importlib.import_module("push_runs")
    base = AutopilotPolicy(enabled=True)
    rows = {"health": {"value": 1, "price": 50, "status": "available"},
            "attack_speed": {"value": 1, "price": 10, "status": "available"},
            "lifesteal": {"status": "locked"}}
    policy = module.push_policy(base, tier=4, rows=rows)
    assert choose(policy, rows, {"cash": 100}).upgrade_id == "attack_speed"
    urgent = module.push_policy(base, tier=4, rows=rows, combat={"health": 10, "max_health": 100})
    assert choose(urgent, rows, {"cash": 100}).upgrade_id == "health"
    assert not next(rule for rule in policy.rules if rule.upgrade_id == "lifesteal").enabled
    assert [rule.upgrade_id for rule in urgent.rules] == ["health"]


def test_persisted_push_without_target_cannot_fall_back_to_farming(tmp_path: Path) -> None:
    path = tmp_path / "push.json"
    push = scheduler(path)
    complete_farms(push)
    push.menu(1, False)
    push.started(11, "farm")
    raw = json.loads(path.read_text())
    raw["target_tier"] = None
    path.write_text(json.dumps(raw))
    restored = scheduler(path)
    assert restored.snapshot()["blocker"]
    assert not restored.active


def test_due_push_and_finished_push_detour_home_instead_of_retry(bot_on_game_over) -> None:
    import events
    from strategy import Shopping
    bot = bot_on_game_over(Shopping())
    complete_farms(bot.push_runs)
    bot.run_once()
    assert [event.target for event in bot.bus.published if isinstance(event, events.Navigated)] == ["HOME"]


def test_ambiguous_left_chevrons_cannot_authorize_return() -> None:
    root = Path(__file__).parent / "fixtures"
    frame = cv2.imread(str(root / "menu_main_events_badge_tier_next.png"))
    boxes = [ocr.TextBox(item["text"], item["confidence"], config.Rect(*item["rect"]))
             for item in json.loads((root / "ocr/menu_main_events_badge_tier_next.json").read_text())]
    templates = vision.TemplateCache(config.TEMPLATE_DIR)
    left = cv2.flip(templates.get(tier_select.NEXT_TEMPLATE), 1)
    height, width = left.shape[:2]
    frame[1300:1300 + height, 260:260 + width] = left
    panel = tier_select.read_panel(frame, templates, boxes)
    assert panel is not None
    assert panel.previous is None


def test_legacy_fleet_milestone_label_counts_normal_runs_as_farming() -> None:
    import dataclasses
    import events
    import screens
    from strategy import Shopping
    from tests.conftest import _shopping_bot
    bot = _shopping_bot("in_run_lit", state=screens.ScreenState.IN_RUN,
                        policy=Shopping(), auto_navigate=True)
    bot.reroll_progress = SimpleNamespace()
    strategy = dataclasses.replace(bot.controls.snapshot().strategy,
        autopilot=AutopilotPolicy(enabled=True, preset="turtle", purpose="milestone"))
    bot.controls.replace(strategy)
    event = bot._start_run(events.RunStarted(run_id=1), bot.controls.snapshot())
    assert event.purpose == "farm"
    assert bot.run_identity(bot.controls.snapshot()).purpose == "farm"
    bot.push_runs.ended(abandoned=False)
    assert bot.push_runs.snapshot()["farms_remaining"] == 9


def test_active_push_keeps_combat_policy_when_a_suspended_battle_resumes() -> None:
    import events
    import screens
    from strategy import Shopping
    from tests.conftest import _shopping_bot
    bot = _shopping_bot("main_menu_resume", state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=True)
    complete_farms(bot.push_runs)
    bot.push_runs.menu(1, True)
    bot.push_runs.menu(2, False)
    bot.push_runs.started(11, "farm")
    bot._finish_run(events.RunEnded(run_id=11, duration=100, abandoned=True))
    assert bot.push_runs.active
    resumed = bot._start_run(events.RunStarted(run_id=12), bot.controls.snapshot())
    assert resumed.purpose == "milestone"
    assert bot.push_runs.active
    bot._finish_run(events.RunEnded(run_id=12, duration=100, abandoned=False))
    assert bot.push_runs.state.phase == "returning"


def test_fleet_push_status_requires_matching_account() -> None:
    module = importlib.import_module("push_runs")
    push = scheduler()
    payload = push.snapshot()
    assert module.validated_snapshot(payload, "account-a") == payload
    assert module.validated_snapshot(payload, "account-b") is None
    assert module.validated_snapshot({**payload, "farms_remaining": -1}, "account-a") is None


def test_live_cadence_edit_preserves_progress_and_scheduled_push() -> None:
    import screens
    from strategy import Shopping
    from tests.conftest import _shopping_bot
    bot = _shopping_bot("menu_main_events_badge_tier_next", state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=False)
    complete_farms(bot.push_runs, 2)
    bot.controls.apply({"push_every_farm_runs": 3})
    bot.run_once()
    assert bot.push_runs.every == 3
    assert bot.push_runs.state.farms == 2
    bot.push_runs.started(3, "farm")
    bot.push_runs.ended(abandoned=False)
    assert bot.push_runs.needs_home
    bot.controls.apply({"push_every_farm_runs": 0})
    bot.run_once()
    assert bot.push_runs.every == 0
    assert bot.push_runs.needs_home
    assert bot.push_runs.state.farms == 3
