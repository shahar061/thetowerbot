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
        self.coins: int | None = None   # overrides the home wallet read when set
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
                           coin_balance=self.coins if self.coins is not None
                           else 583 if self.started else 613)

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


def rush_over(point: tuple[int, int]):
    """A Rush button whose box covers `point`."""
    from ocr import TextBox
    import config
    return TextBox("Rush", .99, config.Rect(point[0] - 40, point[1] - 20, 80, 40))


def test_a_rush_box_over_research_refuses_before_any_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch)
    h.visit.cancel("new request")
    assert h.visit.request(act())
    for name in FRAMES[:-1]:
        h.scan(name)
    name = FRAMES[-1]
    research = lab_screen.read_confirmation(frame(name), boxes(name)).research_point
    h.time += 1
    result = h.visit.advance(frame(name), boxes(name) + (rush_over(research),), h.device, h.time,
                             observed_at=h.time, capture_scope=h.scope)
    assert result is not None and result.reason == 'unsafe_tap_target'
    assert h.journal.open_transactions() == ()
    assert research not in h.device.taps


def test_a_rush_box_over_the_unlock_price_refuses_before_any_spend(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_lab_slot_unlock import PRICE_POINT, UnlockHarness, locked_boxes, promote_canary
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    results = [h.scan(locked_boxes() + (rush_over(PRICE_POINT),)) for _ in range(6)]
    assert [r.reason for r in results if r is not None] == ['unsafe_tap_target']
    assert h.transactions() == 0
    assert PRICE_POINT not in h.device.taps


def test_the_visit_cap_after_a_start_tap_halts_the_canary_with_evidence(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="canary")
    h.visit.cancel("new request")
    assert h.visit.request(act())
    h.walk()
    assert h.journal.open_transactions()
    h.time += 200                                # past the visit's time cap, well before 8 scans
    h.scan('menu_labs_game_speed_picker')
    record = h.starter.state().rollout("start:2")
    assert record.stage == "halted" and record.halted_reason == "Start was not proven"
    assert record.evidence and all(Path(p).name.startswith("lab-start-slot2-") for p in record.evidence)
    assert h.journal.open_transactions()
    assert not h.visit.active and h.visit._outcome.reason == 'lab_start_uncertain'


def start_then_read_idle(h: StartHarness, scans: int = 3, coins: int | None = None):
    """Tap Research, then read slot 2 idle `scans` times, one second apart, the wallet at `coins`."""
    h.visit.cancel("new request")
    assert h.visit.request(act())
    h.walk()
    (txn,) = h.journal.open_transactions()
    h.coins = coins
    for _ in range(scans):
        h.scan('menu_labs_active')
    return txn


def test_an_unlanded_canary_start_is_not_charged_and_a_second_miss_halts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="canary")
    txn = start_then_read_idle(h)
    assert h.journal.open_transactions() == ()
    assert h.visit._outcome.reason == 'unclaimed_dispatch_refuted'
    # The refutation names slot 2, so TowerBot never writes it over Lab 1's cadence.
    assert (h.visit._outcome.decision.slot, h.visit._outcome.decision.research_id) == (2, ATTACK)
    record = h.starter.state().rollout("start:2")
    assert record.stage == "canary"
    assert (record.outcome["outcome"], record.outcome["transaction_key"]) == ("not_charged", txn.key)
    start_then_read_idle(h)
    record = h.starter.state().rollout("start:2")
    assert record.stage == "halted" and record.halted_reason == "The canary's start tap did not land twice"
    assert [e.key for e in h.of(events.LabStarterHalted)] == ["start:2"]


def test_one_idle_read_then_a_running_read_is_bought_not_refuted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch)
    start_then_read_idle(h, scans=2)             # the second read qualifies; one is not enough
    assert h.journal.open_transactions()
    h.started = True
    for _ in range(3):
        h.scan('menu_labs_active')
    assert h.journal.open_transactions() == ()
    assert h.visit._outcome.reason == 'research_confirmed'


def test_idle_reads_with_a_moved_wallet_are_not_refuted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch)
    start_then_read_idle(h, scans=4, coins=590)
    assert h.journal.open_transactions()
    assert h.visit._outcome is None or h.visit._outcome.reason != 'unclaimed_dispatch_refuted'


@pytest.mark.parametrize("change", [
    dict(observed_at=12.5),                 # under two seconds after the tap
    dict(wallet_after=313),                 # the coins moved
    dict(effect_changed=None),              # the slot was not read
    dict(slot=2),                           # another slot
    dict(research_id='labs.attack-speed'),  # another research
    dict(target_level=2),                   # another level
    dict(currency='gems'),                  # another wallet
])
def test_an_unlanded_start_needs_complete_proof(tmp_path: Path, change: dict) -> None:
    from transactions import RecoveryEvidence, Verdict
    from tests.test_lab_transactions import authority, prepared
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope)                    # Game Speed, 300 coins from 613, acted at 11
    proof = RecoveryEvidence(category='LABS', currency='coins', wallet_after=613, effect_changed=False,
                             observed_at=13., frame_digest='after', scope=scope, operation='lab_start',
                             slot=1, research_id='labs.game-speed', target_level=1)
    assert journal.refute_unlanded_start(txn.key, replace(proof, **change), now=13.).verdict == Verdict.UNPROVEN
    assert journal.refute_unlanded_unlock(txn.key, proof, now=13.).verdict == Verdict.UNPROVEN
    assert journal.open_transactions()[0].key == txn.key
    outcome = journal.refute_unlanded_start(txn.key, proof, now=13.)
    assert (outcome.verdict, outcome.spent) == (Verdict.REFUTED, 0)
    assert journal.open_transactions() == ()


def test_a_running_selected_slot_reads_as_that_slot_not_lab_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch)
    job = LabJob(2, ATTACK, 'Attack Speed Lv.1', 5000., 100, None, 'unknown', 'researching', .99,
                 (100, 300, 200, 50), source_level=0, target_level=1)
    monkeypatch.setattr(lab_visit, 'read_selected_home', lambda image, text, *, slot, observed_at:
                        lab_screen.LabHomeReading(True, 'researching', job, None, 613))
    h.visit.cancel("new request")
    assert h.visit.request(act())
    for name in ('menu_labs_active', 'menu_labs_active'):
        h.scan(name)
    outcome = h.visit._outcome
    assert outcome.decision.kind == 'wait_running'
    assert (outcome.decision.slot, outcome.decision.research_id) == (2, ATTACK)


def test_a_canary_whose_worker_left_the_pool_is_released_so_another_worker_rehearses(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="dry_run")
    for at in (1., 700.):
        h.starter.note_start_dry_run(2, "Air_9", "account-z", ATTACK, 1, 30, 15., at)
    assert h.starter.state().rollout("start:2").canary_worker == "Air_9"   # no pool file: Air_9 left
    h.visit.cancel("new request")
    assert h.visit.request(act("rehearse"))
    assert h.starter.state().rollout("start:2").stage == "dry_run"
    h.walk()
    assert h.visit._outcome.reason == "research_rehearsed"
    record = h.starter.state().rollout("start:2")
    assert record.stage == "dry_run" and record.rehearsals("Air_1", "account-a") == 1


def test_this_workers_canary_on_another_account_is_released(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="dry_run")
    for at in (1., 700.):
        h.starter.note_start_dry_run(2, "Air_1", "account-b", ATTACK, 1, 30, 15., at)
    h.visit.cancel("new request")
    assert not h.visit.request(act())          # promoted on account-b: no start on account-a
    assert h.visit.recovery_status == "starter_gate_refused"
    assert h.starter.state().rollout("start:2").stage == "dry_run"
    assert h.visit.request(act("rehearse"))


def test_this_workers_canary_on_its_own_account_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="canary")
    h.visit.cancel("new request")
    assert h.visit.request(act())
    record = h.starter.state().rollout("start:2")
    assert (record.stage, record.canary_worker, record.canary_account) == ("canary", "Air_1", "account-a")


def test_the_planner_sweep_releases_an_absent_canary_at_most_once_a_minute(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = StartHarness(tmp_path, monkeypatch, stage="dry_run")
    for at in (1., 700.):
        h.starter.note_start_dry_run(3, "Air_9", "account-z", ATTACK, 1, 30, 15., at)
    h.visit.sweep_stale_canaries(1000.)
    assert h.starter.state().rollout("start:3").stage == "dry_run"
    for at in (1., 700.):
        h.starter.note_start_dry_run(3, "Air_9", "account-z", ATTACK, 1, 30, 15., at)
    h.visit.sweep_stale_canaries(1030.)        # throttled
    assert h.starter.state().rollout("start:3").stage == "canary"
    h.visit.sweep_stale_canaries(1061.)
    assert h.starter.state().rollout("start:3").stage == "dry_run"
