"""Slot-one Game Speed decisions use observed state rather than assumptions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config

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


def test_slot_two_reservation_is_account_bound(tmp_path: Path) -> None:
    from lab_plan import LabCadence

    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    assert cadence.slot2_due(1000.)
    cadence.note_slot2("locked", 65, 1000.)
    assert not LabCadence(tmp_path, "ACCOUNT-A").slot2_due(1100., wallet_gems=99)
    assert LabCadence(tmp_path, "ACCOUNT-A").slot2_due(1100., wallet_gems=100)
    cadence.note_slot2("locked", 100, 1150.)
    assert not cadence.slot2_due(1200., wallet_gems=100)
    assert LabCadence(tmp_path, "ACCOUNT-B").slot2_due(1100.)
    cadence.note_slot2("owned", 19, 1200.)
    assert cadence.slot2_owned()
    assert not cadence.slot2_due(100_000.)


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
    from lab_plan import LAB2_GEMS, LabCadence, LabVisitOptions

    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    cadence.note_slot2("locked", 65, 1000.)
    assert not cadence.slot2_due(1100., wallet_gems=120, min_gems=150)
    assert cadence.slot2_due(1100., wallet_gems=150, min_gems=150)
    assert cadence.slot2_due(1100., wallet_gems=100)  # the default floor is unchanged
    assert LAB2_GEMS == 100
    assert LabVisitOptions() == LabVisitOptions(start_research=True, unlock_slot2=True, min_gems=100)
