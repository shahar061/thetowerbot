"""Wall deadlines request evidence, never synthesize research completion."""
from dataclasses import replace
from pathlib import Path

import pytest

from lab_runtime import LabRuntime
from tests.test_lab_runtime import observation


def running(root: Path, finish: float = 110.) -> LabRuntime:
    store = LabRuntime(root, 'account-a', lease_id='lease', generation='generation')
    reading = observation(10.)
    job = replace(reading.jobs[0], concept_id='labs.game-speed', source_level=0,
                  target_level=1, completes_at=finish, remaining_s=finish-10)
    reading = replace(reading, jobs=(job,))
    store.observe(reading)
    store.observe(replace(reading, observed_at=11.))
    return store


def test_deadline_uses_catalog_speed_and_never_completes_job(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    store = running(tmp_path)
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(store.snapshot())
    assert schedule.next_deadline() == 110.
    assert schedule.due(109.) == ()
    due = schedule.due(110.)
    assert {a.kind for a in due} == {'speed_check', 'inspect_labs'}
    assert next(a for a in due if a.kind == 'speed_check').target_speed == 2.
    assert store.snapshot().slots[0].state == 'researching'


def test_generation_claim_survives_restart_and_correction(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    store = running(tmp_path)
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(store.snapshot())
    speed = next(a for a in schedule.due(110.) if a.kind == 'speed_check')
    assert schedule.claim(speed)
    assert not schedule.claim(speed)
    restarted = MaintenanceSchedule(tmp_path, 'account-a')
    restarted.observe(store.snapshot())
    assert not any(a.kind == 'speed_check' for a in restarted.due(500.))
    assert any(a.kind == 'inspect_labs' for a in restarted.due(12.))


def test_boost_replaces_deadline_and_new_generation_rearms(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    store = running(tmp_path)
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    snapshot = store.snapshot()
    schedule.observe(snapshot)
    job = replace(snapshot.slots[0], expected_finish=40., remaining_s=20., observed_at=20.)
    schedule.observe(replace(snapshot, slots=(job, *snapshot.slots[1:])))
    assert schedule.next_deadline() == 40.
    old = next(a for a in schedule.due(40.) if a.kind == 'speed_check')
    assert schedule.claim(old)
    job = replace(job, generation='new-job', target_level=2, expected_finish=60.)
    schedule.observe(replace(snapshot, slots=(job, *snapshot.slots[1:])))
    assert next(a for a in schedule.due(60.) if a.kind == 'speed_check').target_speed == 2.5


def test_clock_jump_resume_and_sleep_do_not_spin_on_overdue(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(running(tmp_path).snapshot())
    assert schedule.wait_seconds(100., 30.) == 10.
    schedule.tick(100., 10., paused=False)
    schedule.tick(95., 11., paused=False)
    assert any(a.reason == 'clock_changed' for a in schedule.due(95.))
    assert schedule.wait_seconds(120., 30.) == 30.
    schedule.tick(96., 12., paused=True)
    schedule.tick(97., 13., paused=False)
    assert any(a.reason == 'resumed' for a in schedule.due(97.))


def test_speed_rearm_is_once_per_generation() -> None:
    from speed import SpeedController
    speed = SpeedController(patience=1)
    assert speed.decide(1., 1.5) == 'up'
    assert speed.decide(1., 1.5) is None
    assert speed.rearm_completion('job-a')
    assert speed.decide(1., 1.5) == 'up'
    assert not speed.rearm_completion('job-a')
    assert speed.decide(1., 1.5) is None
    assert speed.rearm_completion('job-b')
    assert speed.decide(1., 1.5) == 'up'


def test_widget_capability_requires_distinct_frames_and_survives_restart(tmp_path: Path) -> None:
    store = running(tmp_path)
    assert not store.observe_speed(1.5, observed_at=20.)
    assert not store.observe_speed(1.5, observed_at=20.)
    assert store.observe_speed(1.5, observed_at=21.)
    assert store.snapshot().verified_speed == 1.5
    assert LabRuntime(tmp_path, 'account-a', lease_id='new', generation='new').snapshot().verified_speed == 1.5
    assert LabRuntime(tmp_path, 'other', lease_id='new', generation='new').snapshot().verified_speed is None
    assert store.snapshot().slots[0].state == 'researching'


def test_manual_target_wins_over_auto_policy() -> None:
    from strategy import Strategy, ActionRule
    strategy = Strategy(name='test', actions=(ActionRule('Damage', 'upgrade_damage.png'),),
                        auto_fastest=True, target_speed=1.)
    assert Strategy.from_dict(strategy.to_dict()).auto_fastest is True
    with pytest.raises(ValueError):
        replace(strategy, auto_fastest='yes')


def test_false_deadline_confirmed_corrections_get_three_persistent_windows(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    snapshot = running(tmp_path).snapshot()
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(snapshot)
    first = next(a for a in schedule.due(110.) if a.kind == 'speed_check')
    assert schedule.claim(first)
    job = replace(snapshot.slots[0], expected_finish=150., observed_at=120.)
    updated = replace(snapshot, slots=(job, *snapshot.slots[1:]))
    schedule.observe(updated)
    assert not any(a.kind == 'speed_check' for a in schedule.due(149.))
    second = next(a for a in schedule.due(150.) if a.kind == 'speed_check')
    assert second.generation != first.generation
    assert schedule.claim(second)
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(updated)
    noisy = replace(job, expected_finish=151., observed_at=130.)
    schedule.observe(replace(snapshot, slots=(noisy, *snapshot.slots[1:])))
    assert not any(a.kind == 'speed_check' for a in schedule.due(160.))
    job = replace(job, expected_finish=200., observed_at=160.)
    schedule.observe(replace(snapshot, slots=(job, *snapshot.slots[1:])))
    third = next(a for a in schedule.due(200.) if a.kind == 'speed_check')
    assert schedule.claim(third)
    job = replace(job, expected_finish=250., observed_at=210.)
    schedule.observe(replace(snapshot, slots=(job, *snapshot.slots[1:])))
    assert not any(a.kind == 'speed_check' for a in schedule.due(300.))
    assert any(a.reason == 'timer_correction_limit' for a in schedule.due(300.))


def test_inspection_requires_new_complete_runtime_evidence(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    from dataclasses import replace
    store = running(tmp_path)
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(store.snapshot())
    due = schedule.due(110.)
    assert any(a.kind == 'inspect_labs' for a in due)
    schedule.acknowledge_observation(store.snapshot(), now=110.)
    assert schedule.due(110.) == due
    snapshot = store.snapshot()
    complete = replace(snapshot, slots_owned=1, observed_at=111., strip_complete=True,
        slots=(replace(snapshot.slots[0], observed_at=111.), *snapshot.slots[1:]))
    schedule.acknowledge_observation(complete, now=111.)
    assert not any(a.kind == 'inspect_labs' for a in schedule.due(111.))


def test_new_job_releases_timer_correction_blocker(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    snapshot = running(tmp_path).snapshot()
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    generation = snapshot.slots[0].generation
    schedule.request('inspect_labs', generation + ':v3', 20., reason='timer_correction_limit', slot=1)
    snapshot = replace(snapshot, slots=(replace(snapshot.slots[0], generation='fresh-job'), *snapshot.slots[1:]))
    schedule.observe(snapshot)
    assert not any(a.reason == 'timer_correction_limit' for a in schedule.due(200.))


def test_corrected_inspection_requeues_even_when_speed_policy_is_off(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    snapshot = running(tmp_path).snapshot()
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(snapshot)
    first = next(a for a in schedule.due(110.) if a.kind == 'inspect_labs')
    assert schedule.claim(first)
    job = replace(snapshot.slots[0], expected_finish=150., observed_at=120.)
    schedule.observe(replace(snapshot, slots=(job, *snapshot.slots[1:])))
    assert any(a.kind == 'inspect_labs' for a in schedule.due(150.))


def test_resume_and_restart_reconcile_even_after_all_windows_consumed(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(running(tmp_path).snapshot())
    for action in schedule.due(110.):
        schedule.claim(action)
    schedule.tick(110., 10., paused=True)
    schedule.tick(111., 11., paused=False)
    assert any(a.reason == 'resumed' for a in schedule.due(111.))
    for action in schedule.due(111.):
        schedule.claim(action)
    restarted = MaintenanceSchedule(tmp_path, 'account-a')
    assert any(a.reason == 'restart' for a in restarted.due(112.))


@pytest.mark.parametrize('research_id', ['labs.game-speed', 'labs.attack-speed'])
def test_corrected_inspection_has_precise_unix_identity_after_restart(tmp_path: Path, research_id: str) -> None:
    from maintenance_schedule import MaintenanceSchedule
    snapshot = running(tmp_path, finish=1790532000.).snapshot()
    job = replace(snapshot.slots[0], research_id=research_id)
    snapshot = replace(snapshot, slots=(job, *snapshot.slots[1:]))
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(snapshot)
    first = next(a for a in schedule.due(1790532000.) if a.kind == 'inspect_labs')
    assert schedule.claim(first)
    # No speed window was claimed, as with automatic/manual speed disabled.
    corrected = replace(job, expected_finish=1790532040., observed_at=1790532010.)
    schedule.observe(replace(snapshot, slots=(corrected, *snapshot.slots[1:])))
    revised = [a for a in schedule.due(1790532050.)
               if a.kind == 'inspect_labs' and a.reason == 'research_deadline']
    assert len(revised) == 1
    assert revised[0].generation != first.generation
    assert revised[0].deadline == 1790532040.
    restarted = MaintenanceSchedule(tmp_path, 'account-a')
    assert revised == [a for a in restarted.due(1790532050.)
                       if a.kind == 'inspect_labs' and a.reason == 'research_deadline']


def test_partial_strip_with_retained_ownership_does_not_acknowledge(tmp_path: Path) -> None:
    from lab_runtime import LabRuntime
    from maintenance_schedule import MaintenanceSchedule
    store = LabRuntime(tmp_path, 'a', lease_id='l', generation='g')
    reading = observation(100.)
    reading = replace(reading, slots_owned=1, jobs=(reading.jobs[0],))
    store.observe(reading)
    store.observe(replace(reading, observed_at=101.))
    schedule = MaintenanceSchedule(tmp_path, 'a')
    schedule.request('inspect_labs', 'test', 110., reason='research_deadline')
    partial = replace(reading, slots_owned=None, slots_status='unknown', observed_at=120.)
    store.observe(partial)
    store.observe(replace(partial, observed_at=121.))
    assert not partial.strip_read()
    assert store.snapshot().slots_owned == 1 and not store.snapshot().strip_complete
    schedule.acknowledge_observation(store.snapshot(), now=121.)
    assert any(a.kind == 'inspect_labs' for a in schedule.due(121.))
    store.observe(replace(reading, observed_at=122.))
    store.observe(replace(reading, observed_at=123.))
    assert store.snapshot().strip_complete
    schedule.acknowledge_observation(store.snapshot(), now=123.)
    assert not any(a.kind == 'inspect_labs' for a in schedule.due(123.))


def test_legacy_rounded_inspection_key_is_removed_on_correction(tmp_path: Path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    snapshot = running(tmp_path, finish=1790532000.).snapshot()
    job = snapshot.slots[0]
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.observe(snapshot)
    exact = next(a for a in schedule.due(1790532000.) if a.kind == 'inspect_labs')
    assert exact.generation.endswith(':at1790532000.0')
    schedule.request('inspect_labs', f'{job.generation}:v0:at1.79053e+09', 1790532000.,
                     reason='research_deadline', slot=1)
    corrected = replace(job, expected_finish=1790532040., observed_at=1790532010.)
    schedule.observe(replace(snapshot, slots=(corrected, *snapshot.slots[1:])))
    assert [a.generation for a in schedule.due(1790532050.) if a.kind == 'inspect_labs'] == [
        f'{job.generation}:v0:at1790532040.0']


def test_inspection_visits_back_off_until_acknowledged(tmp_path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    schedule = MaintenanceSchedule(tmp_path, 'account-a')
    schedule.request('inspect_labs', 'clock_changed:x', 10., reason='clock_changed')
    schedule.request('inspect_labs', 'job:v3', 10., reason='timer_correction_limit', slot=1)
    assert schedule.inspection_navigable(20.)
    schedule.note_inspection_visit(20.)
    assert not schedule.inspection_navigable(139.)
    assert schedule.inspection_navigable(140.)
    schedule.note_inspection_visit(140.)
    assert not schedule.inspection_navigable(379.) and schedule.inspection_navigable(380.)
    action = next(a for a in schedule.due(400.) if a.reason == 'clock_changed')
    assert schedule.claim(action)
    # Only the correction-limit blocker remains: surfaced, never navigated.
    assert any(a.reason == 'timer_correction_limit' for a in schedule.due(400.))
    assert not schedule.inspection_navigable(400.)


def _restart_run(root, generation: str, t0: float, finish: float, *, epoch: int = 0):
    from dataclasses import replace
    from lab_runtime import LabRuntime
    from tests.test_lab_runtime import observation
    store = LabRuntime(root, 'account-a', lease_id='lease', generation=generation, epoch=epoch)
    reading = observation(t0)
    job = replace(reading.jobs[0], concept_id='labs.game-speed', source_level=0,
                  target_level=1, completes_at=finish, remaining_s=finish - t0)
    reading = replace(reading, jobs=(job,))
    store.observe(reading)
    store.observe(replace(reading, observed_at=t0 + 1))
    return store


def test_worker_restart_keeps_one_speed_window_per_running_research(tmp_path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    first = _restart_run(tmp_path, 'gen-one', 10., 110.)
    MaintenanceSchedule(tmp_path, 'account-a').observe(first.snapshot())
    restarted = MaintenanceSchedule(tmp_path, 'account-a')
    second = _restart_run(tmp_path, 'gen-two', 50., 110., epoch=1)  # restart + epoch bump
    assert second.snapshot().slots[0].generation == first.snapshot().slots[0].generation
    restarted.observe(second.snapshot())
    speed = [a for a in restarted.due(110.) if a.kind == 'speed_check']
    assert len(speed) == 1, [a.generation for a in speed]


def test_correction_bound_survives_worker_restarts(tmp_path) -> None:
    from maintenance_schedule import MaintenanceSchedule
    finish, t, windows = 110., 10., 0
    for n in range(6):
        schedule = MaintenanceSchedule(tmp_path, 'account-a')
        schedule.observe(_restart_run(tmp_path, f'gen-{n}', t, finish).snapshot())
        for action in schedule.due(finish):
            if action.kind == 'speed_check' and schedule.claim(action):
                windows += 1
        t, finish = finish + 1, finish + 30.  # Still running; the timer slipped.
    assert windows <= 4
    assert any(a.reason == 'timer_correction_limit' for a in schedule.due(10**6))


def test_restart_carries_only_job_identity_for_the_same_research(tmp_path) -> None:
    from dataclasses import replace
    from lab_runtime import LabRuntime
    from tests.test_lab_runtime import observation
    first = _restart_run(tmp_path, 'gen-one', 10., 110.).snapshot().slots[0].generation
    restarted = LabRuntime(tmp_path, 'account-a', lease_id='lease', generation='gen-two')
    assert restarted.snapshot().slots[0].state == 'unknown'  # No state is restored.
    reading = observation(50.)
    other = replace(reading.jobs[0], concept_id='labs.game-speed', source_level=1,
                    target_level=2, completes_at=150., remaining_s=100.)
    reading = replace(reading, jobs=(other,))
    restarted.observe(reading)
    restarted.observe(replace(reading, observed_at=51.))
    assert restarted.snapshot().slots[0].generation != first  # A different job is new.
