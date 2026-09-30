"""Offline control-flow contracts for starting any lab; injected reads never calibrate."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from fleet.resource_blocks import LabAction
from lab_starter_rollout import LabStarterRollout
import events
import lab_screen
import lab_visit
import labs
from labs import LabJob
from tests.test_lab_transactions import LabHarness
from tests.test_lab_visit import boxes, frame

ATTACK = "labs.attack-speed"
FRAMES = ('menu_labs_active', 'menu_labs_active', 'menu_labs_game_speed_affordable',
          'menu_labs_game_speed_affordable', 'menu_labs_game_speed_confirmation',
          'menu_labs_game_speed_confirmation')


def act(operation: str = "start") -> LabAction:
    return LabAction(2, ATTACK, 1, operation, 7, "route next")


class StartHarness(LabHarness):
    """LabHarness plus a starter rollout, a worker id and the attack-speed injections
    that test_offline_general_executor_binds_the_selected_slot_research_and_account uses."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, *, stage: str = "fleet") -> None:
        super().__init__(root, monkeypatch)
        self.starter = LabStarterRollout(root / "fleet")
        self.visit.starter, self.visit.worker = self.starter, "Air_1"
        self.visit.evidence_dir = root / "evidence"
        self.started = False
        self.swipes: list[tuple] = []
        self.device.swipe = lambda *args: self.swipes.append(args)
        if stage in ("canary", "fleet"):
            self.starter.note_start_dry_run(2, "Air_1", "account-a", ATTACK, 1, 30, 15., 1.)
            self.starter.note_start_dry_run(2, "Air_1", "account-a", ATTACK, 1, 30, 15., 700.)
        if stage == "fleet":
            self.starter.note_start(2, "Air_1", "account-a", "seed", "bought", at=800.)
        slots, home, picker = lab_screen.read_slots, lab_screen.read_selected_home, lab_screen.read_selected_picker

        def observed_slots(image, text, *, observed_at):
            reading = slots(image, text, observed_at=observed_at)
            if self.started and reading.slots_owned == 5:
                reading = replace(reading, jobs=tuple(
                    LabJob(2, ATTACK, 'Attack Speed Lv.1', observed_at + 100, 100, None, 'unknown',
                           'researching', .99, job.rect, source_level=0, target_level=1)
                    if job.slot == 2 else job for job in reading.jobs))
            return reading

        def selected_home(image, text, *, slot, observed_at):
            return replace(home(image, text, slot=slot, observed_at=observed_at),
                           coin_balance=583 if self.started else 613)

        def selected_picker(image, text, *, research_id):
            reading = picker(image, text, research_id=research_id)
            if reading.entry is not None:
                return replace(reading, game_speed=replace(reading.entry, status='available'),
                               buy_point=(830, 1320))
            return reading

        monkeypatch.setattr(lab_visit, 'read_slots', observed_slots)
        monkeypatch.setattr(lab_visit, 'read_selected_home', selected_home)
        monkeypatch.setattr(lab_visit, 'read_selected_picker', selected_picker)
        self.visit.confirmation_reader = lambda image, text: replace(
            lab_screen.read_confirmation(image, text), name='Attack Speed Lv.1', price=30)

    def of(self, kind: type) -> list:
        return [event for event in self.events if isinstance(event, kind)]

    def walk(self) -> None:
        for name in FRAMES:
            self.scan(name)


def test_a_rehearsal_cancels_and_never_prepares_a_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="dry_run")
    h.visit.cancel("new request")
    assert h.visit.request(act("rehearse"))
    h.walk()
    assert h.journal.open_transactions() == ()
    assert h.visit._outcome.reason == "research_rehearsed"
    cancel = lab_screen.read_confirmation(frame('menu_labs_game_speed_confirmation'),
                                          boxes('menu_labs_game_speed_confirmation'))
    h.scan('menu_labs_game_speed_confirmation')
    assert cancel.cancel_point in h.device.taps and cancel.research_point not in h.device.taps
    assert [(e.slot, e.research_id, e.price) for e in h.of(events.LabStartRehearsed)] == [(2, ATTACK, 30)]
    assert h.starter.state().lab(ATTACK).status == "rehearsed"


def test_a_blocked_gate_refuses_the_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="canary")
    h.visit.worker = "Air_2"
    h.visit.cancel("new request")
    assert not h.visit.request(act())
    assert h.visit.recovery_status == "starter_gate_refused"


def test_a_start_at_fleet_stage_spends_once_and_is_proven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch)
    h.visit.cancel("new request")
    assert h.visit.request(act())
    h.walk()
    txn = h.journal.open_transactions()[0]
    assert (txn.before['slot'], txn.before['research_id'], txn.price) == (2, ATTACK, 30)
    h.started = True
    for _ in range(3):
        h.scan('menu_labs_active')
    assert h.journal.open_transactions() == ()
    assert h.visit._outcome.reason == 'research_confirmed'


def test_the_canarys_proven_start_promotes_the_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="canary")
    h.visit.cancel("new request")
    assert h.visit.request(act())
    h.walk()
    h.started = True
    for _ in range(3):
        h.scan('menu_labs_active')
    assert h.starter.state().rollout("start:2").stage == "fleet"
    assert [(e.key, e.stage) for e in h.of(events.LabStarterPromoted)] == [("start:2", "fleet")]


def test_an_unproven_canary_start_halts_with_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="canary")
    h.visit.cancel("new request")
    assert h.visit.request(act())
    h.walk()
    for _ in range(9):
        h.scan('menu_labs_game_speed_picker')   # neither the slot nor the wallet can be read
    record = h.starter.state().rollout("start:2")
    assert record.stage == "halted" and record.halted_reason == "Start was not proven"
    assert record.evidence and all(Path(p).name.startswith("lab-start-slot2-") for p in record.evidence)
    assert h.journal.open_transactions()          # the hold stays
    assert h.visit.recovery_status == "lab_start_uncertain"


def test_a_lab_missing_from_the_picker_is_a_miss_not_a_halt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="dry_run")
    monkeypatch.setattr(lab_visit, 'read_selected_picker',
                        lambda image, text, *, research_id: lab_screen.LabPickerReading(True, None, 613, None))
    h.visit.cancel("new request")
    assert h.visit.request(LabAction(2, "labs.labs-speed", 1, "rehearse", 7, "route next"))
    for name in ('menu_labs_active', 'menu_labs_active') + ('menu_labs_game_speed_picker',) * 12:
        h.scan(name)
    assert h.visit._outcome.reason == "research_not_found"
    assert h.starter.state().lab("labs.labs-speed").status == "missing"
    assert h.starter.state().rollout("start:2").stage == "dry_run"
    assert h.swipes, "the search must have scrolled before giving up"


def test_a_corrupt_starter_file_at_prepare_time_refuses_the_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch)
    h.visit.cancel("new request")
    assert h.visit.request(act())
    for name in FRAMES[:-1]:
        h.scan(name)
    (tmp_path / "fleet" / "lab-starter-rollout.json").write_text("{broken")
    h.scan(FRAMES[-1])
    assert h.journal.open_transactions() == ()
    assert h.visit._outcome.reason.startswith("lab_preparation_refused")


def test_a_tap_on_rush_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ocr import TextBox
    import config
    visit = LabHarness(tmp_path, monkeypatch).visit
    visit._boxes = (TextBox("Rush", .99, config.Rect(700, 280, 180, 50)),)
    assert not visit._safe((790, 300))
    assert not visit._safe((790, 420))   # the gem price under Rush
    assert visit._safe((300, 300))
