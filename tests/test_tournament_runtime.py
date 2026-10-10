from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import pytest

import events
import screens
from evidence_scope import FactScope
from tests.test_bot_reporting import make_bot
from tests.test_tournament_screen import recorded
from tournament_store import TournamentStore


def configured(tmp_path: Path) -> tuple:
    bot, recorder, device = make_bot('in_run_lit')
    scope = FactScope('account', 'lease', 'generation', 1)
    bot.account_state = SimpleNamespace(verified_scope=scope, safety_path=tmp_path/'runs.sqlite',
                                       guard_scope=lambda expected: nullcontext())
    bot._screen_fact_scope = scope
    return bot, recorder, device


def advance(bot: object, name: str) -> tuple:
    frame, boxes = recorded(name)
    bot._screen = frame
    reading = screens.ScreenReading(screens.ScreenState.UNKNOWN, 1., {})
    return bot._advance_tournament(bot.controls.snapshot(), SimpleNamespace(full=lambda: boxes), reading)


def test_free_entry_latches_mode_and_verified_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('tower_bot.time.time', lambda: 1791590400)
    bot, recorder, device = configured(tmp_path)
    advance(bot, 'menu_tournament_open')
    advance(bot, 'tournament_join_ticket_1')
    assert device.click.call_count == 2
    handled, reading = advance(bot, 'tournament_run_start')
    assert not handled and reading.state is screens.ScreenState.IN_RUN
    visit = bot.tournament_visit
    bot.runs.restore(7, 0)
    visit.store.bind_run(7)
    bot.bus.publish(events.BattlePurchased(item='Health', upgrade_id='health', price=10, value=100))
    assert visit.store.pending().growth_index == 1
    assert visit.policy({}, {'wave': 1}).rules[0].upgrade_id == 'attack_speed'
    handled, _ = advance(bot, 'tournament_stats')
    assert handled
    ended = recorder.of(events.RunEnded)[0]
    assert ended.tournament and ended.tier is None and ended.rank == 30
    assert bot._best_wave is None
    assert visit.store.conn.execute('SELECT COUNT(*) FROM ledger WHERE kind=?', ('TOURNAMENT_PAYOUT',)).fetchone()[0] == 1


def test_pause_and_changed_account_never_authorize_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('tower_bot.time.time', lambda: 1791590400)
    bot, _, device = configured(tmp_path)
    bot.controls.apply({'paused': True})
    advance(bot, 'tournament_join_ticket_1')
    assert not device.click.called
    bot.controls.apply({'paused': False})
    bot.account_state.verified_scope = FactScope('other', 'lease', 'generation', 2)
    advance(bot, 'tournament_join_ticket_1')
    assert not device.click.called


def test_ordinary_run_does_not_trigger_full_tournament_ocr(tmp_path: Path) -> None:
    bot, _, _ = configured(tmp_path)
    bot.runs.restore(1, 0)
    reading = screens.ScreenReading(screens.ScreenState.IN_RUN, 1., {})
    def forbidden() -> tuple:
        raise AssertionError('full-frame OCR in an established farm run')
    assert bot._advance_tournament(bot.controls.snapshot(), SimpleNamespace(full=forbidden), reading) == (False, reading)


def test_recovered_result_is_durable_without_event_consumer(tmp_path: Path) -> None:
    store = TournamentStore(tmp_path/'runs.sqlite', 'account')
    store.begin('event', 'Copper', 1, now=10)
    store.entered(None, now=11)
    store.bind_run(7)
    assert store.result(wave=8, rank=30, coins=103, ad_coins=0, killed_by='Basic', now=20)
    store.close()
    recovered = TournamentStore(tmp_path/'runs.sqlite', 'account')
    row = recovered.conn.execute('SELECT * FROM runs WHERE id=7').fetchone()
    assert row['tournament'] and row['wave'] == 8 and row['rank'] == 30
    assert not recovered.result(wave=99, rank=1, coins=1000, ad_coins=0, killed_by='Basic', now=21)
    assert recovered.conn.execute("SELECT COUNT(*) FROM ledger WHERE kind='TOURNAMENT_PAYOUT'").fetchone()[0] == 1


def test_manual_buy_cannot_override_tournament_policy() -> None:
    from dataclasses import replace
    from tests.test_autopilot import parts
    bot, device, frame, observation, policy = parts()
    bot.submit({'action': 'buy', 'upgrade_id': 'damage'}, now=100)
    bot.step(frame, device, replace(policy, enabled=False), cash=100,
             observation=observation, allow_manual=False)
    assert not device.actions


def test_restart_closes_cash_opening_with_unconfirmed_expenditure(tmp_path: Path) -> None:
    from tournament_visit import TournamentVisit
    from tournament_policy import TournamentConfig
    cfg = TournamentConfig.from_dict({'opening_cash': {'cash_bonus_target': 10,
        'cash_per_wave_target': 100, 'cash_budget': 50, 'until_wave': 100}})
    store = TournamentStore(tmp_path/'runs.sqlite', 'account')
    visit = TournamentVisit(store, cfg)
    store.begin('event', 'Copper', 1, now=10)
    store.entered(None, now=11)
    rows = {'cash_bonus': {'status': 'available', 'value': 1, 'price': 10}}
    assert visit.policy(rows, {'wave': 1}).rules[0].upgrade_id == 'cash_bonus'
    # A tap occurred, but the process died before its receipt was observed.
    restarted = TournamentVisit(store, cfg)
    assert all(r.upgrade_id not in {'cash_bonus','cash_per_wave'} for r in restarted.policy(rows, {'wave': 1}).rules)


def test_tournament_preserves_a_due_push_and_does_not_count_as_farming(tmp_path: Path) -> None:
    from tournament_visit import TournamentVisit
    from tournament_policy import TournamentConfig
    bot, _, _ = configured(tmp_path)
    store = TournamentStore(tmp_path/'runs.sqlite', 'account')
    store.begin('event','Copper',1,now=10)
    store.entered(None,now=11)
    bot.tournament_visit = TournamentVisit(store,TournamentConfig())
    bot.push_runs.state.phase = 'ready'
    bot.push_runs.state.farms = 10
    bot.push_runs.state.farm_tier = 1
    bot.push_runs.state.target_tier = 2
    started = bot._start_run(events.RunStarted(run_id=7),bot.controls.snapshot())
    assert started.tournament and started.league == 'Copper'
    assert store.pending().run_id == 7
    assert bot.push_runs.state.phase == 'ready' and bot.push_runs.state.run_id is None
    bot._finish_run(events.RunEnded(run_id=7,duration=10,tournament=True))
    assert bot.push_runs.state.farms == 10 and bot.push_runs.state.phase == 'ready'
