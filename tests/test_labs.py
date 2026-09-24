"""Focused checks for the Lab state model.

There is no recorded Labs capture in this repository, so nothing here parses
a frame. What is under test is the model: that a lab nobody has looked at
stays `unknown` rather than becoming a level 0, that owned slots and running
research survive a restart, and that ambiguous evidence buys nothing.

`reading()` defaults to no slot evidence at all, so a test that says nothing
about slots is testing levels and only levels. The slot tests opt in.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import types
from typing import Any

import cv2
import pytest

import config
import ocr

FIXTURES = Path(__file__).parent / 'fixtures'

# Real catalog identities, so these fixtures cannot drift away from the
# registry the module reads.
DAMAGE = 'labs.damage'
DEFENSE = 'labs.defense'
CRITICAL = 'labs.critical-factor'


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / 'ocr' / f'{name}.json').read_text()))


def entry(concept_id: str | None = DAMAGE, level: int | None = 3, *,
          status: str = 'available', max_level: int | None = 20,
          confidence: float = .99, cost: float | None = 1500.,
          duration_s: float | None = 3600.) -> Any:
    import labs
    return labs.LabEntry(concept_id, concept_id or 'Unnamed', level, max_level, cost,
                         duration_s, status, confidence, (10, 20, 30, 40))


def job(concept_id: str | None = DAMAGE, completes_at: float | None = 5000., *,
        slot: int = 0, status: str = 'researching', confidence: float = .99,
        speed_multiplier: float | None = 1., acceleration: str = 'none') -> Any:
    import labs
    return labs.LabJob(slot, concept_id, concept_id or 'Unnamed', completes_at,
                       None if completes_at is None else completes_at - 100.,
                       speed_multiplier, acceleration, status, confidence,
                       (10, 200, 30, 40))


def reading(entries: tuple[Any, ...] = (), jobs: tuple[Any, ...] = (), *,
            now: float = 1000., slots_owned: int | None = None,
            slots_status: str = 'unknown') -> Any:
    import labs
    return labs.LabsReading(now, 1080, 2400, 'digest', slots_owned, slots_status,
                            entries, jobs)


def strip(jobs: tuple[Any, ...], *, now: float = 1000., owned: int | None = None) -> Any:
    """A reading whose whole slot strip was read, which is what lets jobs move."""
    return reading((), jobs, now=now, slots_owned=len(jobs) if owned is None else owned,
                   slots_status='observed')


def state(tmp_path: Path) -> tuple[Any, Any]:
    from account_state import AccountRepository, AccountState
    import labs
    account = AccountState(AccountRepository(tmp_path / 'state.db'))
    return labs.LabsState(account), account


def test_identity_comes_from_the_catalog_and_stops_at_its_edge() -> None:
    """Success: the module names the catalog's labs and refuses everything else."""
    import labs
    from concepts import REGISTRY
    catalog = {c.concept_id for c in REGISTRY.concepts if c.domain == 'labs'}
    assert labs.LAB_CONCEPT_IDS == catalog and len(catalog) == 158
    assert all(cid.startswith('labs.') for cid in labs.LAB_CONCEPT_IDS)
    # An Ultimate Weapon is a different domain and a different task: a lab
    # that researches one is a `labs.*` identity, the weapon itself is not.
    assert 'ultimate-weapons.black_hole' not in labs.LAB_CONCEPT_IDS
    assert 'labs.black-hole-damage' in labs.LAB_CONCEPT_IDS


def test_unseen_lab_is_unknown_and_never_a_level(tmp_path: Path) -> None:
    """The acceptance gate: absence of evidence is not evidence of level 0."""
    import labs
    lab_state, account = state(tmp_path)
    before = lab_state.levels()
    assert set(before) == labs.LAB_CONCEPT_IDS
    assert all(v['status'] == 'unknown' and v['level'] is None for v in before.values())

    assert lab_state.observe(reading((entry(DAMAGE, 0),))) is False
    assert lab_state.observe(reading((entry(DAMAGE, 0),), now=1010.)) is True

    after = lab_state.levels()
    assert after[DAMAGE] == {'level': 0, 'status': 'available', 'observed_at': 1010.}
    assert after[DEFENSE]['status'] == 'unknown' and after[DEFENSE]['level'] is None
    # The two are different answers, not two spellings of the same one.
    assert after[DAMAGE]['status'] != after[DEFENSE]['status']


def test_owned_slots_and_active_completions_survive_a_restart(tmp_path: Path) -> None:
    """The acceptance gate: a fresh repository over the same file recovers both."""
    from account_state import AccountRepository, AccountState
    import labs
    lab_state, account = state(tmp_path)
    running = (job(DAMAGE, 5000., slot=0), job(DEFENSE, 9000., slot=1))
    lab_state.observe(reading((entry(DAMAGE, 3),), running, slots_owned=2, slots_status='observed'))
    assert lab_state.observe(reading((entry(DAMAGE, 3),), running, now=1010.,
                                     slots_owned=2, slots_status='observed')) is True

    restarted = AccountState(AccountRepository(tmp_path / 'state.db'))
    revision = restarted.snapshot()['revision']
    assert revision['lab_slots_owned'] == 2
    assert {f['concept_id']: f['value'] for f in revision['lab_jobs']} == {DAMAGE: 5000., DEFENSE: 9000.}
    assert all(f['status'] == 'researching' for f in revision['lab_jobs'])
    assert [f['concept_id'] for f in revision['lab_levels']] == [DAMAGE]
    assert labs.LabsState(restarted).levels()[DAMAGE]['level'] == 3


@pytest.mark.parametrize('status', ['locked', 'unavailable', 'unreadable'])
def test_unavailable_rows_keep_their_own_state_and_no_level(tmp_path: Path, status: str) -> None:
    """Unavailable/locked: seen, refused a level, and still not `unknown`."""
    lab_state, account = state(tmp_path)
    rows = (entry(DAMAGE, None, status=status),)
    lab_state.observe(reading(rows))
    assert lab_state.observe(reading(rows, now=1010.)) is False
    # The frame says something definite about this lab; the account file does not.
    assert lab_state.current().status_for(DAMAGE) == status
    assert lab_state.levels()[DAMAGE] == {'level': None, 'status': 'unknown', 'observed_at': None}


def test_maxed_is_persisted_and_is_not_available(tmp_path: Path) -> None:
    lab_state, account = state(tmp_path)
    rows = (entry(DAMAGE, 20, status='maxed'),)
    lab_state.observe(reading(rows))
    assert lab_state.observe(reading(rows, now=1010.)) is True
    assert lab_state.levels()[DAMAGE] == {'level': 20, 'status': 'maxed', 'observed_at': 1010.}


def test_ambiguous_evidence_makes_no_claim_and_no_device_action(tmp_path: Path) -> None:
    """No-action failure case: two rows for one lab buy nothing at all."""
    import labs
    lab_state, account = state(tmp_path)
    rows = (entry(DAMAGE, 3), entry(DAMAGE, 7))
    lab_state.observe(reading(rows))
    assert lab_state.observe(reading(rows, now=1010.)) is False
    assert account.snapshot()['revision'] is None
    assert lab_state.current().status_for(DAMAGE) == 'ambiguous'
    assert lab_state.levels()[DAMAGE]['status'] == 'unknown'
    # This state module still has no route to the phone. The separate
    # reroll-only Game Speed walk owns the one declared action.
    assert labs.capabilities()['actions'] == ('reroll_game_speed_slot_1',)
    reachable = {v.__name__ for v in vars(labs).values() if isinstance(v, types.ModuleType)}
    assert not reachable & {'device', 'control', 'navigate', 'jitter', 'shopping'}


def test_uncatalogued_and_untrusted_rows_are_refused(tmp_path: Path) -> None:
    lab_state, account = state(tmp_path)
    rows = (entry('labs.not-a-real-lab', 3), entry('ultimate-weapons.black_hole', 3),
            entry(CRITICAL, 3, confidence=.4), entry(DEFENSE, -1))
    lab_state.observe(reading(rows))
    assert lab_state.observe(reading(rows, now=1010.)) is False
    assert account.snapshot()['revision'] is None
    assert lab_state.levels()[CRITICAL]['status'] == 'unknown'


def test_one_frame_and_a_stale_gap_never_confirm(tmp_path: Path) -> None:
    lab_state, account = state(tmp_path)
    assert lab_state.observe(reading((entry(DAMAGE, 3),))) is False
    assert account.snapshot()['revision'] is None
    # Too far apart to be the same screen.
    assert lab_state.observe(reading((entry(DAMAGE, 3),), now=1100.)) is False
    # A level that changed between the two frames is not a confirmation.
    assert lab_state.observe(reading((entry(DAMAGE, 4),), now=1110.)) is False
    assert account.snapshot()['revision'] is None


def test_a_drifting_completion_time_is_not_the_same_job(tmp_path: Path) -> None:
    """A countdown redrawn agrees on the finish time; a different job does not."""
    lab_state, account = state(tmp_path)
    lab_state.observe(strip((job(DAMAGE, 5000.),)))
    assert lab_state.observe(strip((job(DAMAGE, 5001.),), now=1010.)) is True
    lab_state.observe(strip((job(DAMAGE, 5400.),), now=2000.))
    assert lab_state.observe(strip((job(DAMAGE, 5800.),), now=2010.)) is False
    # The confirmed pair stands, at the fresher of its two deadlines; the
    # drifting pair that followed it overwrote nothing.
    stored = account.snapshot()['revision']['lab_jobs']
    assert [(f['concept_id'], f['value']) for f in stored] == [(DAMAGE, 5001.)]


def test_an_unreadable_slot_strip_does_not_erase_stored_completions(tmp_path: Path) -> None:
    """Retry: a frame we could not read leaves the last verified answer alone."""
    lab_state, account = state(tmp_path)
    lab_state.observe(strip((job(DAMAGE, 5000.),)))
    assert lab_state.observe(strip((job(DAMAGE, 5000.),), now=1010.)) is True
    blind = reading((), (), now=2000., slots_status='unreadable')
    lab_state.observe(blind)
    assert lab_state.observe(reading((), (), now=2010., slots_status='unreadable')) is False
    revision = account.snapshot()['revision']
    assert [f['concept_id'] for f in revision['lab_jobs']] == [DAMAGE]
    assert revision['lab_slots_owned'] == 1


def test_a_finished_slot_clears_its_job_once_the_strip_is_read(tmp_path: Path) -> None:
    lab_state, account = state(tmp_path)
    lab_state.observe(strip((job(DAMAGE, 5000.),)))
    lab_state.observe(strip((job(DAMAGE, 5000.),), now=1010.))
    idle = (job(None, None, status='idle'),)
    lab_state.observe(strip(idle, now=2000.))
    assert lab_state.observe(strip(idle, now=2010.)) is True
    assert account.snapshot()['revision']['lab_jobs'] == []
    # The job ended; the level it was buying is not retracted with it.
    assert account.snapshot()['revision']['lab_slots_owned'] == 1


def test_persistence_failure_does_not_advance_and_retries(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Restart/retry: a locked database loses the claim, not the evidence rule."""
    from account_state import AccountRepository, AccountState
    import labs
    repo = AccountRepository(tmp_path / 'state.db')
    account = AccountState(repo)
    lab_state = labs.LabsState(account)
    original = repo.save_account

    def fail(*args: Any) -> None:
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(repo, 'save_account', fail)
    lab_state.observe(reading((entry(DAMAGE, 3),)))
    assert lab_state.observe(reading((entry(DAMAGE, 3),), now=1010.)) is False
    assert account.snapshot()['revision'] is None
    assert 'locked' in account.snapshot()['error']

    monkeypatch.setattr(repo, 'save_account', original)
    lab_state.observe(reading((entry(DAMAGE, 3),), now=2000.))
    assert lab_state.observe(reading((entry(DAMAGE, 3),), now=2010.)) is True
    assert account.snapshot()['error'] is None


def test_labs_state_without_persistence_is_explicit() -> None:
    import labs
    lab_state = labs.LabsState()
    lab_state.observe(reading((entry(DAMAGE, 3),)))
    assert lab_state.observe(reading((entry(DAMAGE, 3),), now=1010.)) is False
    assert lab_state.snapshot()['persistence_available'] is False
    assert lab_state.levels()[DAMAGE]['status'] == 'unknown'


def test_unlock_milestones_are_unknown_rather_than_absent() -> None:
    """The catalog records no prerequisite for any lab; that is not "none"."""
    import labs
    assert labs.unlock_milestone(DAMAGE) == ('unknown', None)
    assert labs.unlock_milestone('labs.not-a-real-lab') == ('unknown', None)
    assert labs.unlock_milestone('ultimate-weapons.black_hole') == ('unknown', None)
    assert all(labs.unlock_milestone(cid)[0] == 'unknown' for cid in labs.LAB_CONCEPT_IDS)


def test_recorded_labs_pages_are_readable_without_enabling_spending() -> None:
    import labs
    import screen_discovery
    idle = cv2.imread(str(FIXTURES / 'menu_labs_slot1_idle.png'))
    picker = cv2.imread(str(FIXTURES / 'menu_labs_game_speed_picker.png'))
    frame = cv2.imread(str(FIXTURES / 'menu_workshop_attack.png'))
    assert screen_discovery.discover(idle, recorded('menu_labs_slot1_idle'), 'labs').screen_id == 'labs.home'
    assert screen_discovery.discover(picker, recorded('menu_labs_game_speed_picker'), 'labs').screen_id == 'labs.research'
    result = screen_discovery.discover(frame, recorded('menu_workshop_attack'), 'labs')
    assert result.screen_id is None and not result.readable

    capabilities = screen_discovery.capabilities()
    assert capabilities['readers']['labs.home'] == 'menu_labs_slot1_idle'
    assert capabilities['unsupported_owners']['labs_research_actions'] == 'L02'
    assert capabilities['unsupported_owners']['labs_acceleration_spend'] == 'L03'
    assert 'labs_screen_layout' not in capabilities['unsupported']
    assert labs.capabilities()['reader'] == 'lab_screen'
    assert labs.capabilities()['actions'] == ('reroll_game_speed_slot_1',)
    assert labs.capabilities()['unsupported_owners'] == {
        'labs_research_actions': 'L02',
        'labs_acceleration_spend': 'L03'}


def test_speed_and_acceleration_are_carried_not_invented(tmp_path: Path) -> None:
    lab_state, account = state(tmp_path)
    running = (job(DAMAGE, 5000., speed_multiplier=None, acceleration='unknown'),)
    lab_state.observe(strip(running))
    assert lab_state.observe(strip(running, now=1010.)) is True
    current = lab_state.current()
    assert current.jobs[0].speed_multiplier is None
    assert current.jobs[0].acceleration == 'unknown'
    # A running job says what is being paid for, not what level was reached.
    assert current.status_for(DAMAGE) == 'unknown'
