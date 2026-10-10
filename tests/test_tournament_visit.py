from __future__ import annotations

from pathlib import Path
import pytest
from tests.test_tournament_screen import recorded
from tournament_policy import TournamentConfig
from tournament_store import TournamentStore


class Device:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
    def click(self, x: int, y: int) -> None:
        self.taps.append((x,y))


def test_single_durable_entry_and_restart_reconciliation(tmp_path: Path) -> None:
    from tournament_visit import TournamentVisit
    store = TournamentStore(tmp_path / 'journal.sqlite', 'account')
    visit = TournamentVisit(store, TournamentConfig())
    device = Device()
    frame, boxes = recorded('menu_tournament_open')
    assert visit.advance(frame, boxes, device, now=1791590400)
    frame, boxes = recorded('tournament_join_ticket_1')
    assert visit.advance(frame, boxes, device, now=1791590401)
    assert store.pending().stage == 'entry_pending'
    assert len(device.taps) == 2
    restarted = TournamentVisit(store, TournamentConfig())
    assert not restarted.advance(frame, boxes, device, now=1791590402)
    assert len(device.taps) == 2
    assert restarted.owns_navigation
    frame, boxes = recorded('tournament_run_start')
    assert not restarted.advance(frame, boxes, device, now=1791590403)
    assert restarted.in_run


@pytest.mark.parametrize('name', ['tournament_leaderboard_ticket_0', 'tournament_username_prompt'])
def test_no_paid_or_unconfigured_entry(tmp_path: Path, name: str) -> None:
    from tournament_visit import TournamentVisit
    store = TournamentStore(tmp_path/'journal.sqlite', 'account')
    visit = TournamentVisit(store, TournamentConfig())
    device = Device()
    frame, boxes = recorded(name)
    visit.advance(frame, boxes, device, now=1791590400)
    assert store.pending() is None
    assert all(y != 2036 for _,y in device.taps)
    assert visit.snapshot()['reason'] in {'no_free_entry','setup_required'}


def test_result_return_and_profile_restore_without_retry(tmp_path: Path) -> None:
    from tournament_visit import TournamentVisit
    store = TournamentStore(tmp_path/'journal.sqlite','account')
    store.begin('event','Copper',1,now=100)
    store.entered(7,now=101)
    visit = TournamentVisit(store,TournamentConfig())
    device = Device()
    frame,boxes = recorded('tournament_stats')
    assert visit.advance(frame,boxes,device,now=102)
    assert store.pending().stage == 'result'
    assert visit.take_result().wave == 8
    assert visit.take_result() is None
    assert not visit.advance(frame,boxes,device,now=103)
    assert len(device.taps) == 1
    frame,boxes = recorded('tournament_leaderboard_ticket_0')
    assert visit.advance(frame,boxes,device,now=104)
    frame,boxes = recorded('menu_tournament_open_after_entry')
    assert not visit.advance(frame,boxes,device,now=105)
    assert not visit.in_run and not visit.owns_navigation
    assert store.pending() is None


def test_closed_event_menu_restores_farming_and_next_event_resets_flags(tmp_path: Path) -> None:
    from tournament_visit import TournamentVisit
    store = TournamentStore(tmp_path/'journal.sqlite','account')
    visit = TournamentVisit(store, TournamentConfig())
    device = Device()
    frame, boxes = recorded('tournament_stats')
    for event in ('first', 'second'):
        store.begin(event,'Copper',1,now=100)
        store.entered(None,now=101)
        assert visit.advance(frame, boxes, device, now=102)
        menu, _ = recorded('menu_tournament_open_after_entry')
        assert not visit.advance(menu, (), device, now=103, main_menu=True)
        assert not visit.owns_navigation and store.pending() is None
    assert len(device.taps) == 2


def test_unknown_ticket_retries_after_durable_backoff(tmp_path: Path) -> None:
    from tournament_visit import TournamentVisit
    path = tmp_path/'journal.sqlite'
    store = TournamentStore(path,'account')
    visit = TournamentVisit(store, TournamentConfig())
    device = Device()
    frame, boxes = recorded('tournament_join_ticket_1')
    boxes = tuple(b for b in boxes if not b.text.isdigit())
    visit.advance(frame, boxes, device, now=1791590400)
    assert store.pending() is None
    assert not visit.due(1791590401)
    restarted = TournamentVisit(TournamentStore(path,'account'), TournamentConfig())
    assert not restarted.due(1791590401)
    assert restarted.due(1791590461)


def test_name_setup_returns_to_farming_during_backoff(tmp_path: Path) -> None:
    from tournament_visit import TournamentVisit
    visit = TournamentVisit(TournamentStore(tmp_path/'journal.sqlite','account'), TournamentConfig())
    device = Device()
    frame, boxes = recorded('tournament_username_prompt')
    assert visit.advance(frame, boxes, device, now=1791590400)
    assert visit.owns_navigation
    frame, boxes = recorded('menu_tournament_open')
    assert not visit.advance(frame, boxes, device, now=1791590401, main_menu=True)
    assert not visit.owns_navigation
    assert not visit.due(1791590401)
