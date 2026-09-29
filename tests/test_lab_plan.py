"""Slot-one Game Speed decisions use observed state rather than assumptions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config

from lab_screen import LabHomeReading, LabPickerReading
from labs import LabEntry, LabJob


def test_per_slot_action_requires_confirmed_idle_and_conservative_coins() -> None:
    from dataclasses import replace
    from fleet.resource_blocks import LabAction, LabPlan, SlotNext, SlotNow, SlotPlan, choose_lab_action
    from lab_runtime import LabJobRecord, LabRuntimeSnapshot, LabScope

    scope = LabScope("a", "lease", "worker")
    empty = LabJobRecord(scope, 1, state="idle", confirmed=True, evidence_status="verified",
                         observed_at=1000., generation="idle-1", frame_digest="frame")
    plan = LabPlan(12000, 0, (SlotPlan(1, SlotNow("idle", read_at=1000.),
        SlotNext("labs.game-speed", "Game Speed", 3, 12000, 35280), True, True, (), None,
        {"observe": True, "plan": True, "execute": True}),),
        gems=None, strategy_revision=9, account_id="a", scope=scope,
        evaluated_at=1000.)  # type: ignore[arg-type]
    runtime = LabRuntimeSnapshot(scope, (empty,))
    assert choose_lab_action(plan, runtime, available_coins=11999) is None
    action = choose_lab_action(plan, runtime, available_coins=12000)
    assert action == LabAction(1, "labs.game-speed", 3, "start", 9, "Confirmed idle slot and affordable research")
    assert choose_lab_action(plan, replace(runtime, slots=(replace(empty, state="researching"),)),
                             available_coins=12000) is None
    assert choose_lab_action(plan, LabRuntimeSnapshot(LabScope("a"), (empty,)),
                             available_coins=12000) is None
    assert choose_lab_action(replace(plan, account_id="other"), runtime,
                             available_coins=12000) is None
    stale = replace(plan, slots=(replace(plan.slots[0], now=SlotNow("idle", read_at=900.)),))
    assert choose_lab_action(stale, runtime, available_coins=12000) is None
    assert choose_lab_action(replace(plan, slots=(replace(plan.slots[0], covered=False),)),
                             runtime, available_coins=12000) is None
    other_scope = LabScope("a", "lease-new", "worker", epoch=1)
    other_record = replace(empty, scope=other_scope)
    assert choose_lab_action(plan, LabRuntimeSnapshot(other_scope, (other_record,)),
                             available_coins=12000) is None
    assert choose_lab_action(replace(plan, account_id=None), runtime, available_coins=12000) is None
    assert choose_lab_action(replace(plan, scope=None), runtime, available_coins=12000) is None
    no_cap = replace(plan, slots=(replace(plan.slots[0], capabilities=None),))
    assert choose_lab_action(no_cap, runtime, available_coins=12000) is None
    future = replace(empty, observed_at=1001.)
    future_slot = replace(plan.slots[0], now=SlotNow("idle", read_at=1001.))
    assert choose_lab_action(replace(plan, slots=(future_slot,)),
                             replace(runtime, slots=(future,)), available_coins=12000) is None
    unknown = replace(empty, observed_at=None)
    unknown_slot = replace(plan.slots[0], now=SlotNow("idle", read_at=None))
    assert choose_lab_action(replace(plan, slots=(unknown_slot,)),
                             replace(runtime, slots=(unknown,)), available_coins=12000) is None


def idle() -> LabHomeReading:
    return LabHomeReading(True, "idle", None, (540, 450))


def row(*, level: int = 1, maximum: int | None = None,
        cost: float = 300., balance: int | None = 400,
        status: str = "available", point: tuple[int, int] | None = (291, 711)) -> LabPickerReading:
    return LabPickerReading(True, LabEntry("labs.game-speed", "Game Speed Lv.1",
                                           level, maximum, cost, 599., status,
                                           .99, (96, 615, 290, 40)), balance, point)


def test_idle_affordable_game_speed_starts_even_without_a_visible_maximum() -> None:
    from lab_plan import decide

    decision = decide(idle(), row())
    assert decision.kind == "start"
    assert decision.price == 300


def test_busy_slot_one_preserves_its_job() -> None:
    from lab_plan import decide

    job = LabJob(1, "labs.coins-kill-bonus", "Coins / Kill Bonus Lv.87",
                 5000., 100., None, "unknown", "researching", .99,
                 (31, 345, 439, 37))
    decision = decide(LabHomeReading(True, "researching", job, None), row())
    assert decision.kind == "wait_running"
    assert decision.job_completes_at == 5000.


def test_unaffordable_game_speed_reserves_slot_one_without_blocking_workshop() -> None:
    from lab_plan import decide

    decision = decide(idle(), row(balance=122, status="unavailable", point=None))
    assert decision.kind == "wait_coins"
    assert decision.price == 300
    assert decision.wallet_coins == 122


def test_level_two_picker_proves_the_first_speed_research_completed() -> None:
    from lab_plan import decide

    decision = decide(idle(), row(level=2, cost=2500, balance=835,
                                  status="unavailable", point=None))

    assert decision.kind == "wait_coins"
    assert getattr(decision, "game_speed_level", None) == 2


def test_maxed_game_speed_ends_slot_one_policy() -> None:
    from lab_plan import decide

    assert decide(idle(), row(level=7, maximum=7, status="maxed", point=None)).kind == "done"


def test_unreadable_or_ambiguous_picker_cannot_start_research() -> None:
    from lab_plan import decide

    assert decide(idle(), LabPickerReading(True, None, 400, None)).kind == "unknown"
    assert decide(idle(), row(balance=None, point=None)).kind == "unknown"
    assert decide(idle(), row(balance=400, point=None)).kind == "unknown"
    assert decide(LabHomeReading(True, "locked", None, None), row()).kind == "wait_unlock"


def test_account_bound_lab_cadence_survives_restart(tmp_path: Path) -> None:
    from lab_plan import LabCadence, decide

    root = tmp_path / "worker"
    first = LabCadence(root, "ACCOUNT-A")
    assert first.due(now=1000.)
    first.note(decide(idle(), row(balance=122, status="unavailable", point=None)), now=1000.)
    assert not LabCadence(root, "ACCOUNT-A").due(now=1100.)
    assert not LabCadence(root, "ACCOUNT-A").due(now=1300.)
    assert LabCadence(root, "ACCOUNT-A").due(now=4600.)
    assert LabCadence(root, "ACCOUNT-B").due(now=1100.)


def test_completed_lab_stays_done_after_restart(tmp_path: Path) -> None:
    from lab_plan import LabCadence, decide

    root = tmp_path / "worker"
    LabCadence(root, "ACCOUNT-A").note(
        decide(idle(), row(level=7, maximum=7, status="maxed", point=None)), now=1000.)
    assert not LabCadence(root, "ACCOUNT-A").due(now=100_000.)


def test_confirmed_next_level_unlocks_x2_speed_after_restart(tmp_path: Path) -> None:
    from lab_plan import LabCadence, decide

    root = tmp_path / "worker"
    first = LabCadence(root, "ACCOUNT-A")
    first.note(decide(idle(), row(level=2, cost=2500, balance=835,
                                  status="unavailable", point=None)), now=1000.)
    resumed = LabCadence(root, "ACCOUNT-A")

    assert getattr(resumed, "speed_target", lambda: None)() == 2.0
    assert LabCadence(root, "ACCOUNT-B").speed_target() == 1.5
    resumed.note(decide(LabHomeReading(True, "researching", LabJob(
        1, "labs.game-speed", "Game Speed Lv.2", 5000., 3000., None,
        "unknown", "researching", .99, (96, 615, 290, 40)), None),
        None), now=1100.)
    assert resumed.speed_target() == 2.0


@pytest.mark.parametrize(("level", "maxed", "ceiling"), [
    (1, False, 1.5),  # "Game Speed Lv.1": nothing researched yet
    (3, False, 2.5),  # "Lv.3" is next, so two researches are done
    (7, False, 4.5),
    (7, True, 5.0),   # every research done: the row shows the last level
])
def test_speed_ceiling_follows_completed_game_speed_research(
    tmp_path: Path, level: int, maxed: bool, ceiling: float,
) -> None:
    from lab_plan import LabCadence, decide

    cadence = LabCadence(tmp_path / "worker", "ACCOUNT-A")
    cadence.note(decide(idle(), row(level=level, maximum=level if maxed else None, cost=12000., balance=4000,
                                    status="maxed" if maxed else "unavailable",
                                    point=None)), now=1000.)
    speeds = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
    with patch.object(config, "TARGET_SPEEDS", speeds):
        assert cadence.speed_target() == ceiling
    # A value the widget cannot read is never the target.
    assert cadence.speed_target() == min(ceiling, max(config.TARGET_SPEEDS))


def test_legacy_lab_record_gets_one_new_level_check(tmp_path: Path) -> None:
    import json
    from lab_plan import LabCadence

    root = tmp_path / "worker"
    root.mkdir()
    (root / "lab-slot1-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "kind": "wait_coins", "next_check_at": 9999.,
    }))

    assert LabCadence(root, "ACCOUNT-A").due(now=1000.)


def test_known_research_price_waits_for_coins_without_reopening_labs(tmp_path: Path) -> None:
    from lab_plan import LabCadence, decide

    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    cadence.note(decide(idle(), row(balance=122, status="unavailable", point=None)), 1000.)
    assert not cadence.due(2000., wallet_coins=299)
    assert cadence.due(2000., wallet_coins=300)
    assert not cadence.due(2000.)  # Game Over has no fresh menu wallet.
    assert cadence.due(5000.)  # Infrequent recovery check for unreadable wallets.


def test_slot_two_record_is_account_bound(tmp_path: Path) -> None:
    from lab_plan import LabCadence

    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    assert cadence.slot_due(2, 1000.)
    cadence.note_slots({2: "locked"}, 65, 1000.)
    assert not LabCadence(tmp_path, "ACCOUNT-A").slot_due(2, 1100., wallet_gems=99, min_gems=100)
    assert LabCadence(tmp_path, "ACCOUNT-A").slot_due(2, 1100., wallet_gems=100, min_gems=100)
    cadence.note_slots({2: "locked"}, 100, 1150.)
    assert not cadence.slot_due(2, 1200., wallet_gems=100, min_gems=100)
    assert LabCadence(tmp_path, "ACCOUNT-B").slot_due(2, 1100.)
    cadence.note_slots({2: "owned"}, 19, 1200.)
    assert cadence.slot_owned(2)
    assert not cadence.slot_due(2, 100_000.)


def test_running_research_records_when_it_completes(tmp_path: Path) -> None:
    from lab_plan import LabCadence, LabDecision

    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    cadence.note(LabDecision("wait_running", job_completes_at=5000., game_speed_level=3), now=1000.)
    record, _ = cadence.route_observation()
    assert record is not None and record["job_completes_at"] == 5000.
    cadence.note(LabDecision("wait_coins", price=12000, wallet_coins=10, game_speed_level=3), now=6000.)
    record, _ = cadence.route_observation()
    assert record is not None and record["job_completes_at"] is None


def test_slot_two_check_honours_a_raised_gem_floor(tmp_path: Path) -> None:
    from lab_plan import LabCadence, LabVisitOptions

    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    cadence.note_slots({2: "locked"}, 65, 1000.)
    assert not cadence.slot_due(2, 1100., wallet_gems=120, min_gems=150)
    assert cadence.slot_due(2, 1100., wallet_gems=150, min_gems=150)
    assert cadence.slot_due(2, 1100., wallet_gems=100, min_gems=100)
    assert LabVisitOptions() == LabVisitOptions(start_research=True, unlock_slots=(), keep_gems=0)


def test_lab_slots_file_records_each_slot_and_reads_the_old_slot_two_file_once(tmp_path: Path) -> None:
    import json
    from lab_plan import LabCadence

    (tmp_path / "lab-slot2-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "status": "owned", "wallet_gems": 19, "observed_at": 900.}))
    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    assert cadence.slot_records() == {2: {"status": "owned", "wallet_gems": 19, "observed_at": 900.}}
    assert cadence.slot_owned(2) and not cadence.slot_owned(3)
    cadence.note_slots({3: "locked"}, 120, 1000.)
    assert json.loads((tmp_path / "lab-slots.json").read_text()) == {
        "account_id": "ACCOUNT-A", "slots": {
            "2": {"status": "owned", "wallet_gems": 19, "observed_at": 900.},
            "3": {"status": "locked", "wallet_gems": 120, "observed_at": 1000.}}}
    (tmp_path / "lab-slot2-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "status": "locked", "observed_at": 2000.}))
    assert LabCadence(tmp_path, "ACCOUNT-A").slot_owned(2)  # the old file is never read again
    assert LabCadence(tmp_path, "ACCOUNT-B").slot_records() == {}


def test_slot_due_waits_for_gems_then_rechecks_hourly(tmp_path: Path) -> None:
    from lab_plan import LabCadence

    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    assert cadence.slot_due(3, 1000., wallet_gems=10, min_gems=400)  # never read
    cadence.note_slots({3: "locked"}, 120, 1000.)
    assert not cadence.slot_due(3, 1100., wallet_gems=399, min_gems=400)
    assert cadence.slot_due(3, 1100., wallet_gems=400, min_gems=400)
    cadence.note_slots({3: "locked"}, 400, 1150.)
    assert not cadence.slot_due(3, 1200., wallet_gems=400, min_gems=400)
    assert cadence.slot_due(3, 1150. + 3600, wallet_gems=400, min_gems=400)
    cadence.note_slots({3: "owned"}, 0, 5000.)
    assert not cadence.slot_due(3, 100_000.)


def test_note_slots_ignores_unknown_slots_and_statuses(tmp_path: Path) -> None:
    from lab_plan import LabCadence

    LabCadence(tmp_path, "ACCOUNT-A").note_slots({1: "owned", 6: "locked", 2: "unknown"}, 5, 1.)
    assert not (tmp_path / "lab-slots.json").exists()


def test_slot_records_never_resurrects_legacy_once_lab_slots_json_exists_for_another_account(
        tmp_path: Path) -> None:
    import json
    from lab_plan import LabCadence

    (tmp_path / "lab-slot2-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "status": "locked", "observed_at": 900.}))
    LabCadence(tmp_path, "ACCOUNT-A").note_slots({2: "owned"}, 19, 1000.)
    # A different account's cadence overwrites the shared lab-slots.json file.
    LabCadence(tmp_path, "ACCOUNT-B").note_slots({2: "owned"}, 5, 2000.)
    # lab-slots.json now belongs to ACCOUNT-B: A must see it as empty, never the
    # stale ACCOUNT-A legacy "locked" record.
    assert LabCadence(tmp_path, "ACCOUNT-A").slot_records() == {}


def test_slot_records_never_resurrects_legacy_once_lab_slots_json_is_corrupt(tmp_path: Path) -> None:
    import json
    from lab_plan import LabCadence

    (tmp_path / "lab-slot2-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "status": "locked", "observed_at": 900.}))
    LabCadence(tmp_path, "ACCOUNT-A").note_slots({2: "owned"}, 19, 1000.)
    (tmp_path / "lab-slots.json").write_text("{not valid json")
    assert LabCadence(tmp_path, "ACCOUNT-A").slot_records() == {}
