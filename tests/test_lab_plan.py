"""Slot-one Game Speed decisions use observed state rather than assumptions."""

from __future__ import annotations

from pathlib import Path

from lab_screen import LabHomeReading, LabPickerReading
from labs import LabEntry, LabJob


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
    assert LabCadence(root, "ACCOUNT-A").due(now=1300.)
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


def test_legacy_lab_record_gets_one_new_level_check(tmp_path: Path) -> None:
    import json
    from lab_plan import LabCadence

    root = tmp_path / "worker"
    root.mkdir()
    (root / "lab-slot1-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "kind": "wait_coins", "next_check_at": 9999.,
    }))

    assert LabCadence(root, "ACCOUNT-A").due(now=1000.)
