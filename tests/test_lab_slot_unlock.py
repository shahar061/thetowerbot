"""The bot's own lab-slot unlock: rehearsal, canary tap and the three post-tap outcomes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
import db
import events
import ocr
import transactions
from evidence_scope import BalanceInterval
from lab_plan import LabVisitOptions
from lab_runtime import LabRuntime
from lab_screen import read_slots
from lab_unlock_rollout import LabUnlockRollout
from lab_visit import LabVisit
from tests.test_lab_transactions import authority
from tests.test_lab_visit import Device, boxes, frame
from tests.test_labs_view import _registered
import vision

IMAGE = "menu_labs_slot1_idle"
PRICE_POINT = (586, 906)


def locked_boxes(gems: str | None = "150", price: str = "100") -> tuple[ocr.TextBox, ...]:
    """Lab 1 idle, Lab 2 locked for `price` gems, `gems` in the header (None drops it)."""
    result = []
    for box in boxes(IMAGE):
        if box.text == "65":
            if gems is not None:
                result.append(ocr.TextBox(gems, box.confidence, box.rect))
        elif box.text == "100":
            result.append(ocr.TextBox(price, box.confidence, box.rect))
        else:
            result.append(box)
    return tuple(result)


def owned_boxes(gems: str = "50") -> tuple[ocr.TextBox, ...]:
    """Lab 2 owned and idle; Lab 3 is the next locked tile."""
    kept = tuple(ocr.TextBox(gems, box.confidence, box.rect) if box.text == "65" else box
                 for box in boxes(IMAGE) if box.text not in {"Unlock Znd lab", "100"})
    return kept + (ocr.TextBox("Lab Offline", .99, config.Rect(394, 832, 291, 52)),
                   ocr.TextBox("Lab 3", .99, config.Rect(25, 1046, 97, 39)),
                   ocr.TextBox("Unlock 3rd lab", .99, config.Rect(351, 1174, 378, 51)),
                   ocr.TextBox("400", .99, config.Rect(536, 1271, 101, 53)))


def three_owned_boxes(gems: str = "100") -> tuple[ocr.TextBox, ...]:
    """Labs 2 and 3 owned and idle; Lab 4 is the next locked tile."""
    two = tuple(box for box in owned_boxes(gems) if box.text not in {"Unlock 3rd lab", "400"})
    return two + (ocr.TextBox("Lab Offline", .99, config.Rect(395, 1225, 290, 48)),
                  ocr.TextBox("Lab 4", .99, config.Rect(26, 1437, 100, 38)),
                  ocr.TextBox("Unlock 4th lab", .99, config.Rect(351, 1566, 378, 51)))


def dialog_boxes() -> tuple[ocr.TextBox, ...]:
    """A post-tap frame no Labs reader understands, standing in for a gem dialog."""
    return (ocr.TextBox("150", .99, config.Rect(439, 26, 75, 51)),
            ocr.TextBox("Unlock this lab?", .99, config.Rect(300, 900, 480, 60)))


def promote_canary(rollout: LabUnlockRollout, worker: str = "Air_1") -> None:
    rollout.note_dry_run(2, worker, 100, 150, 0., account_id="account-a")
    rollout.note_dry_run(2, worker, 100, 150, 700., account_id="account-a")


class UnlockHarness:
    def __init__(self, fleet: Path, monkeypatch: pytest.MonkeyPatch, worker: str = "Air_1") -> None:
        self.root = fleet / "workers" / worker
        self.root.mkdir(parents=True, exist_ok=True)
        self.account, self.journal, self.scope = authority(self.root)
        self.rollout = LabUnlockRollout(fleet)
        self.time = 10.
        self.events: list[events.Event] = []
        self.device = Device()
        self.runtime = LabRuntime(self.root, 'account-a', lease_id='lease', generation='generation')
        for stamp in (8., 9.):
            self.runtime.observe(read_slots(frame(IMAGE), locked_boxes(), observed_at=stamp))
        self.visit = LabVisit(vision.TemplateCache(Path('templates')), journal=self.journal,
                              account_state=self.account, runtime=self.runtime,
                              wall_clock=lambda: self.time, event_sink=self.events.append,
                              rollout=self.rollout, worker=worker, evidence_dir=self.root / "evidence")
        monkeypatch.setattr('lab_visit.tap', lambda device, x, y: device.taps.append((x, y)))

    def open(self, options: LabVisitOptions = LabVisitOptions(start_research=False,
                                                              unlock_slots=(2,))) -> None:
        assert self.visit.request(options)

    def scan(self, text: tuple[ocr.TextBox, ...], name: str = IMAGE):
        self.time += 1
        return self.visit.advance(frame(name), text, self.device, self.time,
                                  observed_at=self.time, capture_scope=self.scope)

    def leave(self, text: tuple[ocr.TextBox, ...]) -> str | None:
        """Scan Labs until the visit taps the unlock or Battle; return that tap's name."""
        for _ in range(6):
            self.scan(text)
            tap = self.visit.last_tap[0] if self.visit.last_tap else None
            if tap in {"unlock_lab_slot_2", "return_to_battle"}:
                return tap
        return None

    def finish(self):
        return self.scan((), "menu_main_labs_unlocked")

    def of(self, kind: type) -> list:
        return [event for event in self.events if isinstance(event, kind)]

    def transactions(self) -> int:
        with db.reader(self.journal.path) as conn:
            return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def test_a_rehearsal_never_taps_and_never_prepares_a_transaction(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    result = h.finish()
    assert result is not None and result.slot_status == ((2, "locked"),)
    assert PRICE_POINT not in h.device.taps and h.transactions() == 0
    state = h.rollout.slot(2)
    assert state.stage == "dry_run"
    assert [(run.worker, run.price, run.gems) for run in state.dry_runs] == [("Air_1", 100, 150)]
    assert [(e.slot, e.price, e.gems) for e in h.of(events.LabUnlockRehearsed)] == [(2, 100, 150)]


def test_a_second_rehearsal_ten_minutes_later_promotes_the_canary(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.open()
    h.leave(locked_boxes())
    h.finish()
    h.time = 800.
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    assert h.rollout.slot(2).canary_worker == "Air_1"
    assert [e.stage for e in h.of(events.LabUnlockPromoted)] == ["canary"]
    assert h.transactions() == 0


def test_the_canary_taps_once_and_a_proven_unlock_promotes_the_slot_to_fleet(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    assert h.leave(locked_boxes()) == "unlock_lab_slot_2"
    (txn,) = h.journal.open_transactions()
    assert (txn.operation, txn.before["slot"], txn.price, txn.item) == ("lab_unlock", 2, 100, "Lab 2")
    h.scan(owned_boxes())
    h.scan(owned_boxes())
    assert h.journal.open_transactions() == ()
    assert h.leave(owned_boxes()) == "return_to_battle"
    result = h.finish()
    assert (result.unlocked_slot, result.observed_gem_spend) == (2, 100)
    assert result.slot_status == ((2, "owned"), (3, "locked"))
    assert h.device.taps.count(PRICE_POINT) == 1
    state = h.rollout.slot(2)
    assert state.stage == "fleet" and state.unlock["transaction_key"] == txn.key
    assert [(e.slot, e.price, e.gems_before, e.gems_after)
            for e in h.of(events.LabSlotUnlocked)] == [(2, 100, 150, 50)]
    assert [e.stage for e in h.of(events.LabUnlockPromoted)] == ["fleet"]
    with db.reader(h.journal.path) as conn:
        assert [tuple(row) for row in conn.execute(
            "SELECT item, delta FROM ledger WHERE kind='LAB'")] == [("Lab slot 2", -100)]


def test_one_transitional_frame_after_the_tap_does_not_halt(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    h.leave(locked_boxes())
    h.scan(dialog_boxes())
    h.scan(owned_boxes())
    h.scan(owned_boxes())
    assert h.journal.open_transactions() == ()
    assert h.rollout.slot(2).stage == "fleet" and not h.of(events.LabUnlockHalted)


def test_a_tap_that_did_not_land_is_not_charged_and_a_second_miss_halts(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    for _ in range(2):
        h.open()
        assert h.leave(locked_boxes()) == "unlock_lab_slot_2"
        h.scan(locked_boxes())
        h.scan(locked_boxes())
        assert h.journal.open_transactions() == ()
        assert h.leave(locked_boxes()) == "return_to_battle"
        assert h.finish().reason == "unlock_not_landed"
    assert h.rollout.slot(2).stage == "halted"
    assert [e.reason for e in h.of(events.LabUnlockHalted)] == ["The canary's unlock tap did not land twice"]
    assert not h.of(events.LabSlotUnlocked)
    with db.reader(h.journal.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger WHERE kind='LAB'").fetchone()[0] == 0


def test_an_unreadable_post_tap_screen_halts_the_canary_and_keeps_the_hold(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    h.leave(locked_boxes())
    results = [h.scan(dialog_boxes()) for _ in range(3)]
    assert results[-1] is not None and results[-1].reason == "lab_unlock_uncertain"
    assert h.visit.recovery_status == "lab_unlock_uncertain"
    assert h.journal.open_transactions()[0].stage is transactions.Stage.ACTED
    state = h.rollout.slot(2)
    assert (state.stage, state.halted_reason) == ("halted", "post-tap screen was not understood")
    assert len(state.evidence) == 4 and all(Path(path).is_file() for path in state.evidence)
    assert Path(state.evidence[0]).name.startswith("lab-unlock-slot2-")
    assert [e.slot for e in h.of(events.LabUnlockHalted)] == [2]


def test_a_visit_that_times_out_before_the_tap_settles_halts_the_canary(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    h.leave(locked_boxes())
    h.time += 100
    result = h.scan(dialog_boxes())
    assert result is not None and result.reason == "lab_unlock_uncertain"
    state = h.rollout.slot(2)
    assert (state.stage, state.halted_reason) == ("halted", "the visit timed out before the tap settled")
    assert len(state.evidence) == 1 and h.journal.open_transactions()


def test_a_debit_that_does_not_match_the_price_halts_the_canary(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    h.leave(locked_boxes())
    h.scan(owned_boxes(gems="40"))
    result = h.scan(owned_boxes(gems="40"))
    assert result is not None and result.reason == "lab_unlock_uncertain"
    assert h.rollout.slot(2).halted_reason == "gem debit did not match the price"
    assert h.journal.open_transactions()


def test_a_fleet_stage_slot_unlocks_on_a_second_worker(tmp_path, monkeypatch) -> None:
    first = UnlockHarness(tmp_path, monkeypatch, "Air_1")
    promote_canary(first.rollout)
    first.open()
    first.leave(locked_boxes())
    first.scan(owned_boxes())
    first.scan(owned_boxes())
    assert first.rollout.slot(2).stage == "fleet"
    second = UnlockHarness(tmp_path, monkeypatch, "Air_2")
    second.open()
    assert second.leave(locked_boxes()) == "unlock_lab_slot_2"
    second.scan(owned_boxes())
    second.scan(owned_boxes())
    assert second.journal.open_transactions() == ()
    assert second.rollout.slot(2).unlock["worker"] == "Air_1"
    assert not second.of(events.LabUnlockPromoted)


def test_another_workers_canary_holds_this_worker_back(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch, "Air_2")
    promote_canary(h.rollout, "Air_1")
    _registered(tmp_path, "Air_1", "account-a")
    (tmp_path / "reroll-pool.json").write_text(json.dumps(
        [{"name": "Air_1", "endpoint": "127.0.0.1:5555", "lease_id": "lease"}]))
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    assert [(e.action, e.reason) for e in h.of(events.Skipped)] == [("lab_unlock", "waiting_for_canary")]
    assert h.rollout.slot(2).canary_worker == "Air_1" and h.transactions() == 0


def test_a_canary_that_left_the_pool_is_released_and_this_worker_rehearses(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch, "Air_2")
    promote_canary(h.rollout, "Air_1")
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    state = h.rollout.slot(2)
    assert state.stage == "dry_run" and [run.worker for run in state.dry_runs] == ["Air_2"]


def test_a_halted_slot_is_skipped_with_its_reason(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.rollout.halt(2, "operator check")
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    assert [e.reason for e in h.of(events.Skipped)] == ["slot_halted"]


def test_a_rehearsed_price_off_the_catalog_halts_without_rehearsing(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.open()
    h.leave(locked_boxes(price="120"))
    assert h.rollout.slot(2).stage == "halted"
    assert not h.of(events.LabUnlockRehearsed) and len(h.of(events.LabUnlockHalted)) == 1


@pytest.mark.parametrize("options,text", [
    (LabVisitOptions(start_research=False), locked_boxes()),                    # auto-unlock off
    (LabVisitOptions(start_research=False, unlock_slots=(2,)), locked_boxes(gems="99")),  # short
])
def test_no_rehearsal_without_the_switch_or_the_gems(tmp_path, monkeypatch, options, text) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.open(options)
    assert h.leave(text) == "return_to_battle"
    assert h.rollout.slot(2).dry_runs == () and h.transactions() == 0


def doubtful_gems() -> tuple[ocr.TextBox, ...]:
    """The gem header read below the trust floor."""
    return tuple(ocr.TextBox(box.text, .5, box.rect) if box.text == "150" else box
                 for box in locked_boxes())


@pytest.mark.parametrize("stage", ["dry_run", "canary"])
@pytest.mark.parametrize("text", [locked_boxes(gems=None), doubtful_gems()], ids=["missing", "low_confidence"])
def test_an_unreadable_gem_balance_never_rehearses_or_taps(tmp_path, monkeypatch, stage, text) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    if stage == "canary":
        promote_canary(h.rollout)
    h.open()
    assert h.leave(text) == "return_to_battle"
    assert PRICE_POINT not in h.device.taps and h.transactions() == 0
    assert h.rollout.slot(2).stage == stage and not h.of(events.LabUnlockRehearsed)
    assert len(h.rollout.slot(2).dry_runs) == (2 if stage == "canary" else 0)


@pytest.mark.parametrize("gems,rehearsed", [("1.4K", False), ("1.41K", True)])
def test_an_abbreviated_gem_header_is_judged_by_its_lower_bound(tmp_path, monkeypatch, gems, rehearsed) -> None:
    # 1.4K may stand for 1390, which does not cover 100 gems plus a 1300 reserve.
    h = UnlockHarness(tmp_path, monkeypatch)
    h.open(LabVisitOptions(start_research=False, unlock_slots=(2,), keep_gems=1300))
    assert h.leave(locked_boxes(gems=gems)) == "return_to_battle"
    assert bool(h.of(events.LabUnlockRehearsed)) is rehearsed
    assert h.transactions() == 0


def test_a_canary_reassigned_to_another_account_releases_instead_of_tapping(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    for at in (0., 700.):
        h.rollout.note_dry_run(2, "Air_1", 100, 150, at, account_id="account-b")
    assert h.rollout.slot(2).canary_worker == "Air_1"
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    assert PRICE_POINT not in h.device.taps and h.transactions() == 0
    state = h.rollout.slot(2)
    assert state.stage == "dry_run" and state.canary_worker is None
    assert [(run.worker, run.account_id) for run in state.dry_runs] == [("Air_1", "account-a")]


def test_a_stale_slot_record_corrects_itself_without_a_tap(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()  # the options still name slot 2
    assert h.leave(owned_boxes(gems="500")) == "return_to_battle"
    assert h.finish().slot_status == ((2, "owned"), (3, "locked"))
    assert h.transactions() == 0 and h.rollout.slot(3).dry_runs == ()


def test_a_restart_settles_an_open_lab_three_unlock_from_the_next_labs_read(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    intent = transactions.Intent(item='Lab 3', category='LABS', currency='gems', price=400,
        wallet_before=500, ts=10., operation='lab_unlock',
        before={'slot': 3, 'research_id': None, 'source_level': None, 'target_level': None,
                'evidence_digest': 'before'})
    balance = BalanceInterval.from_reading('gems', 500, h.scope, 10., 'before')
    txn = h.journal.prepare(intent, scope=h.scope, balance=balance)
    h.journal.record_action(txn.key, at=10.5)
    h.open(LabVisitOptions(start_research=False))
    for _ in range(3):
        h.scan(three_owned_boxes())
    assert h.journal.open_transactions() == ()
    assert [(e.slot, e.price, e.gems_before, e.gems_after)
            for e in h.of(events.LabSlotUnlocked)] == [(3, 400, 500, 100)]
