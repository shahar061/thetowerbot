"""Offline executor contracts; synthetic observations never calibrate a route."""
from dataclasses import replace
from pathlib import Path

import pytest

from fleet.resource_blocks import LabAction
from lab_visit import LabVisit
from lab_plan import LabVisitOptions
import lab_screen
import labs
import vision
from tests.test_lab_visit import frame, boxes, Device
from tests.test_lab_transactions import LabHarness


def action(slot: int = 1, research: str = 'labs.game-speed', level: int = 1) -> LabAction:
    return LabAction(slot, research, level, 'start', 7, 'route next')


def test_selected_row_reader_does_not_substitute_game_speed() -> None:
    image, text = frame('menu_labs_game_speed_affordable'), boxes('menu_labs_game_speed_affordable')
    selected = lab_screen.read_selected_picker(image, text, research_id='labs.attack-speed')
    assert selected.entry is not None
    assert selected.entry.concept_id == 'labs.attack-speed'
    assert selected.entry.raw_name == 'Attack Speed Lv.1'
    assert lab_screen.read_selected_picker(image, text, research_id='labs.missing').entry is None


def test_confirmation_exposes_semantic_identity_and_level() -> None:
    result = lab_screen.read_confirmation(frame('menu_labs_game_speed_confirmation'),
                                          boxes('menu_labs_game_speed_confirmation'))
    assert (result.research_id, result.target_level) == ('labs.game-speed', 1)


def test_selected_slot_reader_does_not_confuse_running_and_idle_slots() -> None:
    image, text = frame('menu_labs_active'), boxes('menu_labs_active')
    selected = lab_screen.read_selected_home(image, text, slot=2, observed_at=1000.)
    assert selected.slot_status == 'idle' and selected.slots_owned == 5
    assert selected.slot_point is not None
    running = lab_screen.read_selected_home(image, text, slot=5, observed_at=1000.)
    assert running.slot_status == 'researching' and running.slot_point is None
    assert running.job.slot == 5


def test_uncalibrated_action_is_retained_without_enabling_a_visit() -> None:
    visit = LabVisit(vision.TemplateCache(Path('templates')))
    requested = action(2, 'labs.attack-speed')
    assert not visit.request(requested)
    assert not visit.active
    assert visit.pending_action == requested
    assert visit.recovery_status == 'starter_gate_refused'
    matrix = labs.capabilities()
    assert not any(key.startswith('unlock_slot_') for key in matrix['route_gates'])
    assert matrix['route_gates']['in_battle_labs']['enabled'] is False
    assert matrix['route_gates']['in_battle_missions']['enabled'] is False
    assert matrix['route_gates']['game_speed_slot_1']['evidence'] == 'legacy_fixture_regression'


def test_action_level_mismatch_cannot_open_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    assert h.visit.request(action(level=2))
    for name in ('menu_labs_slot1_affordable', 'menu_labs_slot1_affordable',
                 'menu_labs_game_speed_affordable', 'menu_labs_game_speed_affordable'):
        h.scan(name)
    assert h.journal.open_transactions() == ()
    assert h.visit._outcome.reason == 'selected_research_mismatch'
    assert (291, 711) not in h.device.taps


def test_action_executes_one_semantic_purchase_and_returns_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    assert h.visit.request(action(), options=LabVisitOptions())
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    txn = h.journal.open_transactions()[0]
    assert txn.scope.account_id == 'account-a'
    assert txn.before['slot'] == 1 and txn.before['research_id'] == 'labs.game-speed'
    assert h.visit._purchase.strategy_revision == 7
    h.scan('menu_labs_game_speed_running')
    h.scan('menu_labs_game_speed_running')
    h.scan('menu_labs_game_speed_running')
    h.time += 1
    h.visit.advance(frame('menu_main_labs_unlocked'), (), h.device, h.time,
                    observed_at=h.time, capture_scope=h.scope)
    assert not h.visit.active
    assert h.runtime.snapshot().slots[0].transaction_id == txn.key
    point = lab_screen.read_confirmation(frame('menu_labs_game_speed_confirmation'),
                                         boxes('menu_labs_game_speed_confirmation')).research_point
    assert h.device.taps.count(point) == 1
    assert h.journal.open_transactions() == ()


def test_read_only_inspection_waits_for_confirmed_complete_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('observe')
    h.visit.request(options=LabVisitOptions(start_research=False))
    h.scan('menu_labs_slot1_affordable')
    assert h.visit._state == 'home'
    assert h.device.taps == []
    h.scan('menu_labs_slot1_affordable')
    assert h.visit._state == 'return'
    assert h.runtime.snapshot().slots[0].confirmed


def test_picker_has_its_own_bounded_observation_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.scan('menu_labs_slot1_affordable')
    h.scan('menu_labs_slot1_affordable')
    for _ in range(9):
        h.scan('menu_labs_slot1_affordable')
    assert h.visit._outcome.reason == 'picker_stage_timeout'
    assert h.journal.open_transactions() == ()


def test_unlock_needs_a_rollout_record_and_a_worker() -> None:
    from fleet.resource_blocks import gem_automated
    visit = LabVisit(vision.TemplateCache(Path('templates')))
    visit.request(LabVisitOptions(unlock_slots=(2,)))
    home = lab_screen.LabHomeReading(True, 'idle', None, None, gem_balance=150,
        next_locked=lab_screen.LockedSlot(2, 100, (586, 906), (0, 654, 1080, 396)))
    assert not visit._unlock_slot(home, frame('menu_labs_slot1_idle'), Device())
    assert not gem_automated({'type': 'unlock_lab_slot', 'slot': 2})


def test_selected_action_never_enters_labs_during_battle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    requested = action()
    h.visit.request(requested)
    h.time += 1
    outcome = h.visit.advance(frame('in_run_four_tabs'), (), h.device, h.time,
                              observed_at=h.time, capture_scope=h.scope)
    assert outcome is not None and outcome.reason == 'not_at_menu'
    assert h.device.taps == []
    assert h.visit.pending_action == requested


def test_offline_general_executor_binds_the_selected_slot_research_and_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Injected observations exercise control flow only; no calibration is saved."""
    from labs import LabJob
    import lab_visit
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    # Fleet-stage the starter rollout ONLY inside this offline unit test so the
    # gate opens; production reaches fleet stage through real dry runs.
    from lab_starter_rollout import LabStarterRollout
    starter = LabStarterRollout(tmp_path / 'fleet')
    for at in (1., 700.):
        starter.note_start_dry_run(2, 'Air_1', 'account-a', 'labs.attack-speed', 1, 30, 15., at)
    starter.note_start(2, 'Air_1', 'account-a', 'seed', 'bought', at=800.)
    h.visit.starter, h.visit.worker = starter, 'Air_1'
    assert h.visit.request(action(2, 'labs.attack-speed'))
    original_slots = lab_screen.read_slots
    original_home = lab_screen.read_selected_home
    started = [False]

    def observed_slots(image: object, text: tuple, *, observed_at: float) -> labs.LabsReading:
        reading = original_slots(image, text, observed_at=observed_at)
        if started[0] and reading.slots_owned == 5:
            reading = replace(reading, jobs=tuple(
                LabJob(2, 'labs.attack-speed', 'Attack Speed Lv.1', observed_at+100, 100,
                       None, 'unknown', 'researching', .99, job.rect, source_level=0, target_level=1)
                if job.slot == 2 else job for job in reading.jobs))
        return reading

    def home(image: object, text: tuple, *, slot: int, observed_at: float) -> lab_screen.LabHomeReading:
        return replace(original_home(image, text, slot=slot, observed_at=observed_at),
                       coin_balance=583 if started[0] else 613)

    monkeypatch.setattr(lab_visit, 'read_slots', observed_slots)
    monkeypatch.setattr(lab_visit, 'read_selected_home', home)
    original_picker = lab_screen.read_selected_picker

    def picker(image: object, text: tuple, *, research_id: str) -> lab_screen.LabPickerReading:
        reading = original_picker(image, text, research_id=research_id)
        if reading.entry is not None:
            return replace(reading, game_speed=replace(reading.entry, status='available'), buy_point=(830, 1320))
        return reading

    monkeypatch.setattr(lab_visit, 'read_selected_picker', picker)
    h.visit.confirmation_reader = lambda image, text: replace(lab_screen.read_confirmation(image, text),
        name='Attack Speed Lv.1', price=30)
    for name in ('menu_labs_active', 'menu_labs_active', 'menu_labs_game_speed_affordable',
                 'menu_labs_game_speed_affordable', 'menu_labs_game_speed_confirmation',
                 'menu_labs_game_speed_confirmation'):
        h.scan(name)
    txn = h.journal.open_transactions()[0]
    assert txn.scope.account_id == 'account-a'
    assert txn.before['slot'] == 2 and txn.before['research_id'] == 'labs.attack-speed'
    assert txn.before['target_level'] == 1 and txn.price == 30
    started[0] = True
    for _ in range(3):
        h.scan('menu_labs_active')
    assert h.journal.open_transactions() == ()
    assert h.runtime.snapshot().slots[1].transaction_id == txn.key
    assert h.visit._outcome.reason == 'research_confirmed'
    assert h.visit._outcome.observed_coin_spend == 30


@pytest.mark.parametrize('text', ['Game Speed Lv.1', 'GameSpeed Lv.1', 'Game  Speed Lv.1', 'game speed Lv.1'])
def test_research_identity_tolerates_ocr_spacing_like_the_legacy_filter(text: str) -> None:
    assert lab_screen._research_identity(text) == 'labs.game-speed'


def test_direct_start_visit_replans_after_a_proven_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    planned: list[LabAction | None] = [action(), None]
    calls: list[float] = []
    h.visit.plan_action = lambda at: calls.append(at) or (planned.pop(0) if planned else None)
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True))
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    assert len(h.journal.open_transactions()) == 1
    h.scan('menu_labs_game_speed_running')   # still pending
    assert h.visit._state == 'confirm'
    h.scan('menu_labs_game_speed_running')   # the journal proves the start
    assert h.journal.open_transactions() == ()
    # Proven: instead of returning to battle, the visit is back on the slot strip.
    assert h.visit.active and h.visit._state == 'home'
    assert h.visit._outcome.status == 'started' and h.visit._outcome.started_slots == (1,)
    assert h.visit.selected_action is None and h.visit.pending_action is None
    planned_before = len(calls)
    # The planner is asked again on the next fresh strip; it has nothing more,
    # so the visit returns with the accumulated result.
    h.scan('menu_labs_game_speed_running')
    assert len(calls) == planned_before + 1
    assert h.visit._state == 'return'
    h.scan('menu_labs_game_speed_running')
    h.time += 1
    result = h.visit.advance(frame('menu_main_labs_unlocked'), (), h.device, h.time,
                             observed_at=h.time, capture_scope=h.scope)
    assert not h.visit.active
    assert result is not None and result.status == 'started'
    assert result.reason == 'game_speed_confirmed' and result.started_slots == (1,)
    point = lab_screen.read_confirmation(frame('menu_labs_game_speed_confirmation'),
                                         boxes('menu_labs_game_speed_confirmation')).research_point
    assert h.device.taps.count(point) == 1


def test_without_direct_start_a_proven_start_still_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    assert h.visit.request(action(), options=LabVisitOptions())
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    for _ in range(3):
        h.scan('menu_labs_game_speed_running')
    assert h.visit._state == 'return'
    assert h.visit._outcome.started_slots == (1,)


def test_second_start_waits_for_first_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    calls: list[float] = []
    h.visit.plan_action = lambda at: calls.append(at) or action()
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True))
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    planned_before_proof = len(calls)
    h.scan('menu_labs_game_speed_running')   # first post-tap scan: still pending
    h.scan('menu_labs_game_speed_running', same_capture=True)
    assert len(calls) == planned_before_proof
    assert len(h.journal.open_transactions()) == 1
    assert h.visit.selected_action == action()


def test_started_slots_accumulate_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lab_plan import LabDecision
    from lab_visit import LabVisitResult
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    h.visit.plan_action = lambda at: None
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True))
    h.visit._next_start(LabVisitResult('started', 'research_confirmed', LabDecision('start', slot=3)))
    assert h.visit._state == 'home' and h.visit._outcome.started_slots == (3,)
    h.visit._next_start(LabVisitResult('started', 'game_speed_confirmed', LabDecision('start', slot=1)))
    assert h.visit._state == 'home'
    assert h.visit._outcome.started_slots == (3, 1)
    assert h.visit._outcome.reason == 'game_speed_confirmed'


def test_a_replanned_visit_reports_it_and_every_started_research(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lab_plan import LabDecision
    from lab_visit import LabVisitResult
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    h.visit.plan_action = lambda at: None
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True))
    h.visit._next_start(LabVisitResult('started', 'research_confirmed',
                                       LabDecision('start', slot=3, research_id='labs.health')))
    h.visit._next_start(LabVisitResult('started', 'game_speed_confirmed',
                                       LabDecision('start', slot=1, research_id='labs.game-speed')))
    assert h.visit._outcome.replanned
    assert h.visit._outcome.started_research == ('labs.health', 'labs.game-speed')
    # The re-planned strip had nothing more: the ending attempt keeps both.
    h.visit._end_attempt(LabVisitResult('observed', 'wait_running', LabDecision('wait_running')))
    assert h.visit._outcome.replanned and h.visit._outcome.started_research == ('labs.health', 'labs.game-speed')


def test_a_visit_that_returns_after_its_start_has_not_replanned(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lab_plan import LabDecision
    from lab_visit import LabVisitResult
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    assert h.visit.request(action(), options=LabVisitOptions(direct_start=True))
    h.visit._next_start(LabVisitResult('started', 'game_speed_confirmed',
                                       LabDecision('start', slot=1, research_id='labs.game-speed')))
    assert h.visit._state == 'return'
    assert not h.visit._outcome.replanned
    assert h.visit._outcome.started_research == ('labs.game-speed',)


def test_each_proven_start_extends_the_visit_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    planned: list[LabAction | None] = [action()]
    h.visit.plan_action = lambda at: planned.pop(0) if planned else None
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True))
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    for _ in range(2):
        h.scan('menu_labs_game_speed_running')
    assert h.visit._state == 'home'
    h.visit._scans = 48    # the single-start scan cap is spent
    h.scan('menu_labs_game_speed_running')
    assert h.visit.active and h.visit._state == 'return'


def test_no_next_start_where_the_planner_hook_cannot_replan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lab_plan import LabDecision
    from lab_visit import LabVisitResult
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    h.visit.plan_action = lambda at: None
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True, native_repeat='on'))
    h.visit._next_start(LabVisitResult('started', 'research_confirmed', LabDecision('start', slot=2)))
    assert h.visit._state == 'return' and h.visit._outcome.started_slots == (2,)


def test_a_later_attempt_rides_on_the_kept_started_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lab_plan import LabDecision
    from lab_visit import LabVisitResult
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    h.visit.plan_action = lambda at: None
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True))
    h.visit._next_start(LabVisitResult('started', 'game_speed_confirmed', LabDecision('start', slot=1)))
    later = LabVisitResult('observed', 'research_unavailable', LabDecision('unknown', slot=2))
    h.visit._end_attempt(later)
    assert h.visit._state == 'return'
    assert h.visit._outcome.status == 'started' and h.visit._outcome.started_slots == (1,)
    assert h.visit._outcome.attempt == later


def scrolled_picker(h: LabHarness, monkeypatch: pytest.MonkeyPatch, *, scrolls_back: bool = True) -> list[tuple]:
    """The game kept the picker scrolled down: Game Speed is off the page until an up swipe."""
    import lab_visit
    real, scrolled, swipes = lab_screen.read_picker_page, [True], []

    def read(screen: object, text: tuple) -> lab_screen.PickerPage:
        page = real(screen, text)
        if not scrolled[0] or not page.open:
            return page
        return replace(page, cards=tuple(c for c in page.cards if c.lab_id != 'labs.game-speed'))

    def swipe(x1: int, y1: int, x2: int, y2: int, seconds: float) -> None:
        swipes.append((x1, y1, x2, y2))
        if scrolls_back and y2 > y1:
            scrolled[0] = False

    monkeypatch.setattr(lab_screen, 'read_picker_page', read)
    monkeypatch.setattr(lab_visit, 'read_picker_page', read)
    h.device.swipe = swipe
    return swipes


@pytest.mark.parametrize('selected', [action(), None])
def test_a_scrolled_picker_swipes_up_to_game_speed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   selected: LabAction | None) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    h.visit.request(selected, options=LabVisitOptions())
    swipes = scrolled_picker(h, monkeypatch)
    h.scan('menu_labs_slot1_affordable')
    h.scan('menu_labs_slot1_affordable')
    for _ in range(12):
        h.scan('menu_labs_game_speed_affordable')
        assert h.visit._outcome is None or h.visit._outcome.reason != 'picker_stage_timeout'
        if h.visit._state == 'dialog':
            break
    assert swipes and swipes[0][3] > swipes[0][1]  # the first swipe scrolls toward the top
    assert h.visit._state == 'dialog'


def test_a_game_speed_card_never_found_ends_research_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    h.visit.request(action(), options=LabVisitOptions())
    swipes = scrolled_picker(h, monkeypatch, scrolls_back=False)
    h.scan('menu_labs_slot1_affordable')
    h.scan('menu_labs_slot1_affordable')
    for _ in range(12):
        h.scan('menu_labs_game_speed_affordable')
    assert swipes
    assert h.visit._outcome.reason == 'research_not_found'


def test_a_picker_timeout_saves_its_frames_and_logs_why_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    h.visit.request(action(), options=LabVisitOptions())
    h.visit.evidence_dir = tmp_path / 'evidence'
    h.visit.picker_reader = lambda screen, text: lab_screen.LabPickerReading(True, None, None, None)
    h.scan('menu_labs_slot1_affordable')
    h.scan('menu_labs_slot1_affordable')
    with caplog.at_level('INFO', logger='lab_visit'):
        for _ in range(9):
            h.scan('menu_labs_game_speed_affordable')
    assert h.visit._outcome.reason == 'picker_stage_timeout'
    saved = sorted(p.name for p in (tmp_path / 'evidence').iterdir())
    assert saved and all(name.startswith('lab-picker-timeout-labs.game-speed-') for name in saved)
    assert caplog.text.count('read unknown') == 1


def test_a_picker_timeout_after_a_proven_start_rides_on_the_started_result(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lab_plan import LabDecision
    from lab_visit import LabVisitResult
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    h.visit.plan_action = lambda at: None
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True))
    h.visit._next_start(LabVisitResult('started', 'game_speed_confirmed', LabDecision('start', slot=1)))
    h.visit._state = 'picker'
    h.visit.picker_reader = lambda screen, text: lab_screen.LabPickerReading(True, None, None, None)
    for _ in range(9):
        h.scan('menu_labs_game_speed_affordable')
    assert h.visit._outcome.status == 'started' and h.visit._outcome.started_slots == (1,)
    assert h.visit._outcome.attempt.reason == 'picker_stage_timeout'
