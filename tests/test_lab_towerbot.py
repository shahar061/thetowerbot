"""TowerBot's Lab integration: route-gated selection, authorization and visibility.

Fakes stand in for the plan/account seams; LabVisit and the route gates are real.
"""
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import events
from fleet.resource_blocks import LabAction, LabFacts, evaluate_lab_plan
from lab_plan import LabDecision, LabVisitOptions
from lab_visit import LabVisit
from tests.conftest import _RecordingBus
from tests.test_resource_blocks import template_route
import vision

REVISION = 7


def action(slot: int = 1, research: str = 'labs.game-speed', level: int = 4,
           revision: int = REVISION) -> LabAction:
    return LabAction(slot, research, level, 'start', revision, 'planned')


@dataclass
class FakeAccount:
    planned: LabAction | None
    calls: list = field(default_factory=list)

    def lab_facts(self, runtime: Any, *, now: float) -> Any:
        return SimpleNamespace(available_coins=20000)

    def lab_action(self, plan: Any, runtime: Any, *, revision: int, now: float) -> LabAction | None:
        self.calls.append(revision)
        return self.planned if plan is not None and revision == REVISION else None


class FakeProgress:
    def __init__(self, plan: Any, *, options: LabVisitOptions | None = None) -> None:
        self.route_runtime = SimpleNamespace(current=lambda: SimpleNamespace(revision=REVISION))
        self._plan = plan
        self._options = options or LabVisitOptions()

    def lab_visit_options(self) -> LabVisitOptions:
        return self._options

    def lab_strategy_plan(self, runtime: Any, *, available_coins: int | None, now: float) -> Any:
        return self._plan


def planned_with_uncalibrated_slots() -> Any:
    return evaluate_lab_plan(template_route(), LabFacts(
        now=1000., wallet_coins=20000, wallet_gems=160,
        slot2={"status": "locked", "wallet_gems": 160, "observed_at": 990.}))


def bot(planned: LabAction | None, *, plan: Any = None, options: LabVisitOptions | None = None) -> Any:
    from tower_bot import TowerBot
    instance = TowerBot.__new__(TowerBot)
    instance.bus = _RecordingBus()
    instance.account_state = FakeAccount(planned)
    instance.reroll_progress = FakeProgress(plan if plan is not None else planned_with_uncalibrated_slots(),
                                            options=options)
    instance.lab_runtime = SimpleNamespace(snapshot=lambda: SimpleNamespace(slots=()))
    instance.lab_visit = LabVisit(vision.TemplateCache(Path('templates')))
    instance.lab_route_pending = ()
    instance._lab_visit_revision = REVISION
    instance._lab_action_last = None
    return instance


def decision(slot: int = 1, research: str = 'labs.game-speed', level: int = 4,
             revision: int | None = REVISION) -> LabDecision:
    return LabDecision('start', 300, 613, game_speed_level=level, slot=slot,
                       research_id=research, strategy_revision=revision)


def test_legacy_slot_one_action_is_armed_as_the_selected_action() -> None:
    b = bot(action())
    assert b._request_planned_lab_visit(1000., due=False)
    assert b.lab_visit.active and b.lab_visit.selected_action == action()


def test_nothing_due_and_no_gated_action_arms_nothing() -> None:
    b = bot(None)
    assert not b._request_planned_lab_visit(1000., due=False)
    assert not b.lab_visit.active
    assert b._request_planned_lab_visit(1000., due=True)  # legacy observation visit
    assert b.lab_visit.selected_action is None


def test_uncalibrated_planned_action_is_never_requested() -> None:
    # Defensive: even if a plan seam returned an uncalibrated slot-2 start,
    # _plan_lab_action no longer duplicates the gate (LabVisit.gate is the
    # single source of truth); LabVisit.request's own gate still refuses it
    # and nothing arms.
    b = bot(action(2, 'labs.attack-speed', 1))
    assert b._plan_lab_action(1000.) is not None
    assert not b._request_planned_lab_visit(1000., due=False)
    assert not b.lab_visit.active


def test_gate_refusal_between_plan_and_request_falls_back_to_legacy_check() -> None:
    """The gate can move between the plan read and the request read (e.g.
    another worker wrote lab-starter-rollout.json this scan). LabVisit.request
    then refuses the planned action, but that must not skip the whole scan:
    due work still arms the legacy slot-1 check, and undue work is a no-op."""
    b = bot(action(2, 'labs.attack-speed', 1))  # gate blocked: no starter wired
    assert not b._request_planned_lab_visit(1000., due=False)
    assert not b.lab_visit.active
    assert b._request_planned_lab_visit(1000., due=True)
    assert b.lab_visit.active and b.lab_visit.selected_action is None


def test_uncalibrated_plan_choices_are_published_once_with_their_reason() -> None:
    b = bot(None)
    b._plan_lab_action(1000.)
    b._plan_lab_action(1001.)
    skipped = [e for e in b.bus.published if isinstance(e, events.Skipped)]
    assert len(skipped) == 1
    assert skipped[0].action == 'labs' and skipped[0].reason == 'lab_route_calibration_required'
    assert any(line.startswith('Lab 3 labs.coins-wave: Planning only:') for line in b.lab_route_pending)
    assert not any(line.startswith('gems.lab2:') for line in b.lab_route_pending)
    assert 'Lab 1' not in skipped[0].detail  # the legacy route is executable, not pending


def test_authorize_accepts_only_the_matching_gated_plan_action() -> None:
    b = bot(action())
    b.lab_visit.request(action(), options=LabVisitOptions())
    assert b._authorize_lab('lab_start', decision(), 1000.)
    assert not b._authorize_lab('lab_start', decision(level=5), 1000.)


def test_authorize_compares_strategy_revision() -> None:
    b = bot(action())
    assert not b._authorize_lab('lab_start', decision(revision=REVISION - 1), 1000.)
    b._lab_visit_revision = REVISION - 1  # the route moved on since the visit was armed
    assert not b._authorize_lab('lab_start', decision(), 1000.)


def test_authorize_refuses_drift_from_the_selected_action() -> None:
    b = bot(action())
    b.lab_visit.request(action(level=3), options=LabVisitOptions())
    assert not b._authorize_lab('lab_start', decision(), 1000.)


def test_authorize_requires_the_route_gate_for_other_slots(tmp_path: Path) -> None:
    from lab_starter_rollout import LabStarterRollout
    planned = action(2, 'labs.attack-speed', 1)
    b = bot(planned)
    assert not b._authorize_lab('lab_start', decision(2, 'labs.attack-speed', 1), 1000.)
    starter = LabStarterRollout(tmp_path / 'fleet')
    for at in (1., 700.):
        starter.note_start_dry_run(2, 'Air_1', 'account-a', 'labs.attack-speed', 1, 30, 15., at)
    starter.note_start(2, 'Air_1', 'account-a', 'seed', 'bought', at=800.)
    b.lab_visit.starter, b.lab_visit.worker = starter, 'Air_1'
    assert b._authorize_lab('lab_start', decision(2, 'labs.attack-speed', 1), 1000.)


def test_authorize_unlock_follows_the_rollout(tmp_path: Path) -> None:
    from lab_unlock_rollout import LabUnlockRollout
    b = bot(None, options=LabVisitOptions(unlock_slots=(2,)))
    b.account_state.verified_scope = SimpleNamespace(account_id='a')
    b.lab_visit.request(LabVisitOptions(unlock_slots=(2,)))
    unlock = LabDecision('unlock_slot', price=100, slot=2)
    assert not b._authorize_lab('lab_unlock', unlock, 1000.)  # no rollout record
    b.lab_visit.rollout, b.lab_visit.worker = LabUnlockRollout(tmp_path), 'Air_1'
    assert not b._authorize_lab('lab_unlock', unlock, 1000.)  # a dry run never taps
    b.lab_visit.rollout.note_dry_run(2, 'Air_1', 100, 150, 0., account_id='a')
    b.lab_visit.rollout.note_dry_run(2, 'Air_1', 100, 150, 700., account_id='a')
    assert b._authorize_lab('lab_unlock', unlock, 1000.)  # the canary
    assert not b._authorize_lab('lab_unlock', None, 1000.)
    assert not b._authorize_lab('lab_unlock', LabDecision('unlock_slot', price=90, slot=2), 1000.)
    assert not b._authorize_lab('lab_unlock', LabDecision('unlock_slot', price=400, slot=3), 1000.)
    b.account_state.verified_scope = SimpleNamespace(account_id='b')
    assert not b._authorize_lab('lab_unlock', unlock, 1000.)  # the canary now plays another account
    b.account_state.verified_scope = SimpleNamespace(account_id='a')
    b.lab_visit.worker = 'Air_2'
    assert not b._authorize_lab('lab_unlock', unlock, 1000.)


def test_route_runtime_absent_allows_only_the_legacy_route(tmp_path: Path) -> None:
    from lab_starter_rollout import LabStarterRollout
    b = bot(action())
    b.reroll_progress.route_runtime = None
    b._lab_visit_revision = None
    assert b._authorize_lab('lab_start', decision(revision=None), 1000.)
    starter = LabStarterRollout(tmp_path / 'fleet')
    for at in (1., 700.):
        starter.note_start_dry_run(2, 'Air_1', 'account-a', 'labs.attack-speed', 1, 30, 15., at)
    starter.note_start(2, 'Air_1', 'account-a', 'seed', 'bought', at=800.)
    b.lab_visit.starter, b.lab_visit.worker = starter, 'Air_1'
    assert not b._authorize_lab('lab_start', decision(2, 'labs.attack-speed', 1, revision=None), 1000.)
    assert b._plan_lab_action(1000.) is None


def end_visit(b: Any, status: str = 'failed', reason: str = 'selected_research_mismatch') -> None:
    """The visit ended; TowerBot settles the planned attempt as _finish_lab_visit does."""
    from lab_visit import LabVisitResult
    b.lab_visit._state = 'idle'
    b._settle_planned_lab_attempt(LabVisitResult(status, reason, LabDecision('unknown')))


def test_failed_planned_visit_does_not_rearm_every_menu_pass() -> None:
    """Regression: a persistent mismatch must not hold the BATTLE navigator off."""
    b = bot(action())
    armed = 0
    for scan in range(5):
        if b._request_planned_lab_visit(1000. + scan, due=False):
            armed += 1
            end_visit(b)
        assert not b.lab_visit.active  # battle navigation is free to proceed
    assert armed == 1


def test_backoff_keeps_legacy_due_path_but_never_the_failed_action() -> None:
    b = bot(action())
    assert b._request_planned_lab_visit(1000., due=False)
    end_visit(b, reason='picker_stage_timeout')
    assert b._request_planned_lab_visit(1001., due=True)  # legacy cadence-paced check
    assert b.lab_visit.selected_action is None
    end_visit(b)
    assert not b._request_planned_lab_visit(1002., due=False)
    assert not b.lab_visit.active


def test_backoff_clears_when_the_action_key_changes() -> None:
    b = bot(action(level=4))
    assert b._request_planned_lab_visit(1000., due=False)
    end_visit(b)
    assert not b._request_planned_lab_visit(1001., due=False)
    b.account_state.planned = action(level=5)  # new plan level
    assert b._request_planned_lab_visit(1002., due=False)
    assert b.lab_visit.selected_action == action(level=5)
    end_visit(b)
    assert not b._request_planned_lab_visit(1003., due=False)  # same new key: backing off
    b.account_state.planned = action(level=5, revision=REVISION + 1)  # new strategy revision
    assert b._request_planned_lab_visit(1004., due=False)


def test_rehearse_action_backs_off_under_its_own_key() -> None:
    """A 'rehearse' and a 'start' sharing slot/research/level/revision must not
    share a backoff key: switching the operation is a fresh action to arm."""
    from dataclasses import replace
    b = bot(action())
    assert b._request_planned_lab_visit(1000., due=False)
    assert b.lab_visit.selected_action == action()
    end_visit(b)
    assert not b._request_planned_lab_visit(1001., due=False)  # same start key: backing off
    b.account_state.planned = replace(action(), operation='rehearse')
    assert b._request_planned_lab_visit(1002., due=False)  # different operation: its own key
    assert b.lab_visit.selected_action == replace(action(), operation='rehearse')


def test_backoff_clears_on_new_runtime_evidence_for_the_slot() -> None:
    b = bot(action())
    slot = SimpleNamespace(slot=1, state='idle', research_id=None, transaction_id=None)
    b.lab_runtime = SimpleNamespace(snapshot=lambda: SimpleNamespace(slots=(slot,)))
    assert b._request_planned_lab_visit(1000., due=False)
    end_visit(b)
    assert not b._request_planned_lab_visit(1001., due=False)
    slot.state, slot.research_id = 'researching', 'labs.game-speed'  # material slot change
    b._request_planned_lab_visit(1002., due=False)
    slot.state, slot.research_id = 'idle', None
    b.lab_visit._state = 'idle'
    assert b._request_planned_lab_visit(1003., due=False)


def test_backoff_is_bounded_and_then_waits_for_due_work() -> None:
    import tower_bot
    b = bot(action())
    assert b._request_planned_lab_visit(1000., due=False)
    end_visit(b)
    later = 1000. + tower_bot.LAB_ACTION_BACKOFF_SECONDS + 1
    assert not b._request_planned_lab_visit(later, due=False)  # expired, but nothing is due
    assert b._request_planned_lab_visit(later, due=True)
    assert b.lab_visit.selected_action == action()


def test_a_started_visit_clears_the_backoff_but_not_the_due_requirement() -> None:
    b = bot(action())
    assert b._request_planned_lab_visit(1000., due=False)
    end_visit(b, status='started', reason='game_speed_confirmed')
    assert not b._request_planned_lab_visit(1001., due=False)
    assert b._request_planned_lab_visit(1002., due=True)
    assert b.lab_visit.selected_action == action()


def test_cancelled_purple_dot_visit_releases_labs_in_flight(tmp_path: Path) -> None:
    """Pause/walk-cancel/scope-change end the visit without a result."""
    from notification_state import NotificationState
    from tower_bot import TowerBot
    state = NotificationState(tmp_path / 'n.json', scope={'account_id': 'a'})
    state.observe('labs', True, 1., frame_id='f1')
    state.observe('labs', True, 2., frame_id='f2')
    assert state.eligible('labs', 3.)
    state.begin('labs', 3.)
    b = TowerBot.__new__(TowerBot)
    b._notifications = state
    b.lab_visit = SimpleNamespace(active=True)
    b._settle_lab_notification()
    assert state.snapshot()['kinds']['labs']['in_flight']  # Still visiting.
    b.lab_visit.active = False  # LabVisit.cancel('paused') leaves no result.
    b._settle_lab_notification()
    assert not state.snapshot()['kinds']['labs']['in_flight']
    for n, visible in enumerate((False, False, True, True)):
        state.observe('labs', visible, 10. + n, frame_id=f'g{n}')
    assert state.eligible('labs', 10_000.)


class RecordingProgress:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def note_lab_slots(self, slots: dict, gems: int | None) -> None:
        self.calls.append(('slots', slots))

    def note_lab_observation(self, decision: LabDecision) -> None:
        self.calls.append(('observation', decision.kind))


def test_a_finished_visit_logs_the_slot_it_unlocked(caplog: pytest.LogCaptureFixture) -> None:
    from lab_visit import LabVisitResult
    b = bot(None)
    b._notifications = SimpleNamespace(snapshot=lambda: {'kinds': {'labs': {'in_flight': False}}})
    b.reroll_progress = RecordingProgress()
    result = LabVisitResult('observed', 'auto_start_off', LabDecision('inspect'),
                            slot_status=((2, 'owned'), (3, 'owned'), (4, 'locked')),
                            gem_balance=100, gems_before=500, observed_gem_spend=400, unlocked_slot=3)
    with caplog.at_level('INFO', logger='tower_bot'):
        b._finish_lab_visit(result)
    assert '; Lab 3 unlocked' in caplog.text and 'Lab 2 unlocked' not in caplog.text
    assert ('observation', 'inspect') not in b.reroll_progress.calls


def test_rehearsal_result_is_never_observed_as_a_start_or_settles_the_backoff() -> None:
    """Controller ruling (Task 6 review): a rehearsal's decision carries kind
    'start' for the rehearsed slot, but research never began. _finish_lab_visit
    must not report it to reroll_progress as any observation (start or
    otherwise), and _settle_planned_lab_attempt must not clear the action
    backoff for it (status is 'observed', never 'started')."""
    from lab_visit import LabVisitResult
    b = bot(action(2, 'labs.attack-speed', 1))
    b._notifications = SimpleNamespace(snapshot=lambda: {'kinds': {'labs': {'in_flight': False}}})
    b.reroll_progress = RecordingProgress()
    held_backoff = (2, 'labs.attack-speed', 1, REVISION, 'rehearse'), None, 2000.
    b._lab_action_last = held_backoff
    b.lab_visit.selected_action = action(2, 'labs.attack-speed', 1)
    rehearsed = LabDecision('start', slot=2, research_id='labs.attack-speed', game_speed_level=1)
    result = LabVisitResult('observed', 'research_rehearsed', rehearsed)
    b._finish_lab_visit(result)
    assert b.reroll_progress.calls == []  # no start, no other observation either
    assert b._lab_action_last == held_backoff  # untouched: only a verified start clears it
