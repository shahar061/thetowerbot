from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import db
import events
import ledger


def test_confirmed_game_speed_research_debits_coins_once() -> None:
    (line,) = ledger.classify(events.LabResearchStarted(
        concept_id="labs.game-speed", price=300, coins_before=400,
        coins_after=100, completes_at=5000., seq=14, ts=1000.))
    assert (line.kind, line.item, line.currency, line.delta) == (
        "LAB", "Game Speed", "coins", -300)
    assert line.observed == 400
    assert line.detail["coins_after"] == 100


def test_game_speed_research_preserves_the_verified_coin_balance(tmp_path: Path) -> None:
    write, _ = writer(tmp_path)
    lines = write.lines_for(events.LabResearchStarted(
        concept_id="labs.game-speed", price=300, coins_before=613,
        coins_after=313, completes_at=5000., seq=15, ts=1000.))
    assert len(lines) == 1
    assert lines[0].kind == "LAB"
    assert lines[0].balance_after == 313
    assert "LabResearchStarted" in ledger._REPLAYABLE


def test_a_workshop_purchase_debits_coins() -> None:
    (line,) = ledger.classify(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, seq=7, ts=1000.0)
    )

    assert (line.kind, line.currency, line.delta) == ("WORKSHOP_BUY", "coins", -75)
    assert (line.price, line.observed, line.seq) == (75, 1770, 7)


def test_a_card_purchase_debits_gems() -> None:
    (line,) = ledger.classify(
        events.Purchased(item="x1", category="CARDS", price=20,
                         gems_before=40, dry_run=False, seq=8, ts=1000.0)
    )

    assert (line.kind, line.currency, line.delta) == ("CARD_BUY", "gems", -20)
    assert line.observed == 40


def test_a_rehearsal_records_the_price_but_moves_nothing() -> None:
    """delta 0, not -price: a dry run never reached device.tap, so no coins
    left the account. price still records what it would have cost."""
    (line,) = ledger.classify(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=True, seq=9, ts=1000.0)
    )

    assert (line.delta, line.price, line.dry_run) == (0, 75, True)


def test_an_unreadable_price_leaves_the_movement_unknown() -> None:
    """None, not 0. Zero means "provably moved nothing"; this purchase did
    move coins, by an amount nobody read."""
    (line,) = ledger.classify(
        events.Purchased(item="Health", category="DEFENSE", price=None,
                         coins_before=1770, dry_run=False, seq=10, ts=1000.0)
    )

    assert line.delta is None and line.price is None


def test_an_unproven_purchase_keeps_its_price_but_debits_nothing_known() -> None:
    """The price was read before the tap; the journal could not prove it
    left the wallet. The line keeps what it would have cost and leaves what
    actually moved unknown, rather than turning a prediction into a debit."""
    (line,) = ledger.classify(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, verdict="unproven",
                         spent=None, seq=12, ts=1000.0)
    )

    assert (line.delta, line.price) == (None, 75)
    assert line.detail == {"verdict": "unproven"}


def test_a_proven_purchase_debits_what_the_journal_proved() -> None:
    (line,) = ledger.classify(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, verdict="bought",
                         spent=75, seq=13, ts=1000.0)
    )

    assert (line.delta, line.price) == (-75, 75)


def test_a_bought_verdict_with_an_unknown_amount_is_not_the_read_price() -> None:
    """Income landed mid-purchase: bought, but by an amount nobody knows."""
    (line,) = ledger.classify(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, verdict="bought",
                         spent=None, seq=14, ts=1000.0)
    )

    assert line.delta is None


def test_a_free_upgrade_moves_nothing_even_with_a_price_read() -> None:
    (line,) = ledger.classify(
        events.Purchased(item="Health", category="DEFENSE", price=0,
                         coins_before=1770, dry_run=False, verdict="free",
                         spent=0, seq=15, ts=1000.0)
    )

    assert line.delta == 0


def test_backfill_keeps_the_journal_verdict_of_a_stored_purchase(tmp_path: Path) -> None:
    """A replay must not resurrect the read price as a debit."""
    from sinks import store

    conn = db.connect(tmp_path / "bot.db")
    event = events.Purchased(item="Health", category="DEFENSE", price=75,
                             coins_before=1770, dry_run=False, verdict="unproven",
                             spent=None, seq=1, ts=1.0)
    db.insert_event(conn, store.to_row(event, None))
    conn.commit()

    ledger.backfill(conn)

    (delta, price) = conn.execute(
        "SELECT delta, price FROM ledger WHERE kind = 'WORKSHOP_BUY'"
    ).fetchone()
    assert (delta, price) == (None, 75)


def test_a_skip_moves_nothing_and_names_the_currency_it_would_have_spent() -> None:
    (line,) = ledger.classify(
        events.PurchaseSkipped(item="Damage", reason="unaffordable",
                               coins_before=1770, seq=11, ts=1000.0)
    )

    assert (line.kind, line.currency, line.delta) == ("BUY_SKIPPED", "coins", 0)
    assert (line.observed, line.reason) == (1770, "unaffordable")


def test_a_card_skip_reconciles_against_gems() -> None:
    (line,) = ledger.classify(
        events.PurchaseSkipped(item="x10", reason="capped", detail="gem floor",
                               gems_before=40, seq=12, ts=1000.0)
    )

    assert (line.currency, line.observed) == ("gems", 40)


def test_a_skip_with_no_readable_balance_still_moved_nothing() -> None:
    """A row backfilled from before PurchaseSkipped carried balances. It
    reconciles nothing, but "provably moved nothing" is still the truth -
    None would claim it moved by an unknown amount."""
    (line,) = ledger.classify(
        events.PurchaseSkipped(item="Damage", reason="no_match", seq=1, ts=1.0)
    )

    assert (line.currency, line.delta) == (None, 0)


def test_a_run_payout_credits_the_coins_it_earned() -> None:
    (line,) = ledger.classify(
        events.RunEnded(run_id=4, duration=300.0, wave=10, coins=350, tier=1,
                        seq=13, ts=1000.0)
    )

    assert (line.kind, line.currency, line.delta) == ("RUN_PAYOUT", "coins", 350)
    assert line.run_id == 4


def test_an_unreadable_payout_credits_nothing_known() -> None:
    (line,) = ledger.classify(
        events.RunEnded(run_id=4, duration=300.0, wave=10, coins=None, tier=1,
                        seq=14, ts=1000.0)
    )

    assert line.delta is None


@pytest.mark.parametrize(
    "event",
    [
        events.ShoppingStarted(visit=1, dry_run=False),
        events.ShoppingEnded(visit=1, bought=2, spent=95),
        events.ShoppingUnavailable(reason="header atlas incomplete"),
        events.ControlChanged(changed={"paused": True}),
    ],
)
def test_the_non_financial_lines_take_no_part_in_the_arithmetic(
    event: events.Event,
) -> None:
    (line,) = ledger.classify(event)

    assert line.currency is None


@pytest.mark.parametrize(
    "event",
    [
        events.Tapped(action="Damage", x=1, y=2, score=0.9),
        events.Skipped(action="Damage", reason="cooldown"),
        events.ScanCompleted(screen="IN_RUN", duration_ms=12.0),
        events.ScreenChanged(prev="MAIN_MENU", curr="IN_RUN", confidence=0.9, scores={}),
        events.PageChanged(prev_page="MAIN_MENU", curr_page="WORKSHOP", confidence=0.9),
        events.Navigated(target="BATTLE"),
        events.RunStarted(run_id=1),
        events.BotError(message="boom"),
        events.UnknownScreen(snapshot_path="x.png", best_anchor="a", best_score=0.1),
    ],
)
def test_in_run_and_diagnostic_events_are_not_ledger_lines(
    event: events.Event,
) -> None:
    """The ledger is the non-battle history. In-run upgrades are bought with
    per-run cash that resets, so they are not account history at all."""
    assert ledger.classify(event) == ()


def writer(tmp_path: Path) -> tuple[ledger.LedgerWriter, sqlite3.Connection]:
    conn = db.connect(tmp_path / "bot.db")
    return ledger.LedgerWriter(conn), conn


def test_a_matching_reading_produces_one_line_and_no_adjustment(tmp_path: Path) -> None:
    write, _ = writer(tmp_path)

    first = write.lines_for(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, seq=1, ts=1.0)
    )
    second = write.lines_for(
        events.Purchased(item="Damage", category="ATTACK", price=50,
                         coins_before=1695, dry_run=False, seq=2, ts=2.0)
    )

    assert [line.kind for line in first] == ["WORKSHOP_BUY"]
    assert first[0].balance_after == 1695
    assert [line.kind for line in second] == ["WORKSHOP_BUY"]
    assert second[0].balance_after == 1645


def test_a_balance_that_moved_behind_the_bot_gets_its_own_line(tmp_path: Path) -> None:
    """A lab slot bought by hand is the motivating case: the bot cannot see
    the Labs screen at all, so the only trace is gems that went missing."""
    write, _ = writer(tmp_path)

    write.lines_for(
        events.Purchased(item="x1", category="CARDS", price=20,
                         gems_before=200, dry_run=False, seq=1, ts=1.0)
    )
    lines = write.lines_for(
        events.Purchased(item="x1", category="CARDS", price=20,
                         gems_before=40, dry_run=False, seq=2, ts=2.0)
    )

    assert [line.kind for line in lines] == ["UNEXPLAINED", "CARD_BUY"]
    adjustment = lines[0]
    assert (adjustment.currency, adjustment.delta) == ("gems", -140)
    assert adjustment.balance_after == 40
    assert adjustment.seq is None
    # Dated to the event that REVEALED it, not to when the spend happened -
    # which nobody knows.
    assert adjustment.ts == 2.0
    assert lines[1].balance_after == 20


def test_coins_and_gems_reconcile_independently(tmp_path: Path) -> None:
    write, _ = writer(tmp_path)

    write.lines_for(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, seq=1, ts=1.0)
    )
    lines = write.lines_for(
        events.Purchased(item="x1", category="CARDS", price=20,
                         gems_before=40, dry_run=False, seq=2, ts=2.0)
    )

    # The first gem reading ever seen establishes the chain; it cannot
    # contradict a coin balance.
    assert [line.kind for line in lines] == ["CARD_BUY"]
    assert lines[0].balance_after == 20


def test_the_first_reading_of_a_currency_never_looks_unexplained(tmp_path: Path) -> None:
    write, _ = writer(tmp_path)

    lines = write.lines_for(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, seq=1, ts=1.0)
    )

    assert [line.kind for line in lines] == ["WORKSHOP_BUY"]


def test_a_hole_from_an_unreadable_price_is_closed_by_the_next_reading(
    tmp_path: Path,
) -> None:
    write, _ = writer(tmp_path)

    write.lines_for(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, seq=1, ts=1.0)
    )
    hole = write.lines_for(
        events.Purchased(item="Damage", category="ATTACK", price=None,
                         coins_before=1695, dry_run=False, seq=2, ts=2.0)
    )
    after = write.lines_for(
        events.PurchaseSkipped(item="Damage", reason="unaffordable",
                               coins_before=1600, seq=3, ts=3.0)
    )

    # The purchase moved coins by an amount nobody read, so the chain breaks.
    assert hole[0].balance_after is None
    # ...and the next reading turns the whole gap into one honest line.
    assert [line.kind for line in after] == ["UNEXPLAINED", "BUY_SKIPPED"]
    assert after[0].delta == -95
    assert after[1].balance_after == 1600


def test_a_reading_taken_on_an_unreadable_purchase_is_not_reported_twice(
    tmp_path: Path,
) -> None:
    """The gap a reading reveals is priced once. Discarding the reading
    because the same line's price was unreadable made the next reading
    report the same coins missing all over again."""
    write, _ = writer(tmp_path)

    write.lines_for(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, seq=1, ts=1.0)
    )
    # 195 coins went somewhere the bot cannot see, and the price of this
    # purchase could not be read either.
    revealed = write.lines_for(
        events.Purchased(item="Damage", category="ATTACK", price=None,
                         coins_before=1500, dry_run=False, seq=2, ts=2.0)
    )
    later = write.lines_for(
        events.PurchaseSkipped(item="Damage", reason="unaffordable",
                               coins_before=1400, seq=3, ts=3.0)
    )

    assert [line.kind for line in revealed] == ["UNEXPLAINED", "WORKSHOP_BUY"]
    assert revealed[0].delta == -195
    # Only the unreadable purchase's own cost is still outstanding - the 195
    # is settled and must not appear again.
    assert [line.kind for line in later] == ["UNEXPLAINED", "BUY_SKIPPED"]
    assert later[0].delta == -100


def test_a_known_payout_still_counts_while_the_chain_is_stale(
    tmp_path: Path,
) -> None:
    """A run payout the bot recorded itself is not unexplained. Dropping it
    from the anchor made the next reading report a LOSS of 100 as a GAIN of
    250 - wrong sign, on the one line the page exists to make trustworthy."""
    write, _ = writer(tmp_path)

    write.lines_for(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, seq=1, ts=1.0)
    )
    write.lines_for(  # actually cost 100, unreadable
        events.Purchased(item="Damage", category="ATTACK", price=None,
                         coins_before=1695, dry_run=False, seq=2, ts=2.0)
    )
    payout = write.lines_for(
        events.RunEnded(run_id=1, duration=300.0, wave=10, coins=350, tier=1,
                        seq=3, ts=3.0)
    )
    after = write.lines_for(
        events.PurchaseSkipped(item="Damage", reason="unaffordable",
                               coins_before=1945, seq=4, ts=4.0)
    )

    # The chain is stale, so the payout line itself still reports no balance.
    assert payout[0].balance_after is None
    assert [line.kind for line in after] == ["UNEXPLAINED", "BUY_SKIPPED"]
    assert after[0].delta == -100


def test_a_rehearsal_leaves_the_balance_exactly_where_it_was(tmp_path: Path) -> None:
    write, _ = writer(tmp_path)

    lines = write.lines_for(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=True, seq=1, ts=1.0)
    )

    assert lines[0].balance_after == 1770


def test_a_run_payout_credits_the_running_coin_balance(tmp_path: Path) -> None:
    write, _ = writer(tmp_path)

    write.lines_for(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, seq=1, ts=1.0)
    )
    lines = write.lines_for(
        events.RunEnded(run_id=1, duration=300.0, wave=10, coins=350, tier=1,
                        seq=2, ts=2.0)
    )

    assert lines[0].balance_after == 2045


def test_a_writer_opened_over_an_existing_ledger_resumes_its_balances(
    tmp_path: Path,
) -> None:
    """A restart must not invent a bogus UNEXPLAINED by starting from zero."""
    first, conn = writer(tmp_path)
    for line in first.lines_for(
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False, seq=1, ts=1.0)
    ):
        db.insert_ledger(conn, line.as_row())

    resumed = ledger.LedgerWriter(conn)
    lines = resumed.lines_for(
        events.Purchased(item="Damage", category="ATTACK", price=50,
                         coins_before=1695, dry_run=False, seq=2, ts=2.0)
    )

    assert [line.kind for line in lines] == ["WORKSHOP_BUY"]


def test_an_event_outside_the_catalog_produces_no_lines(tmp_path: Path) -> None:
    write, _ = writer(tmp_path)

    assert write.lines_for(events.Tapped(action="Damage", x=1, y=2, score=0.9)) == []


def test_backfill_replays_stored_events_into_lines(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "bot.db")
    db.insert_event(conn, {
        "seq": 1, "run_id": None, "ts": 1.0, "type": "Purchased",
        "screen": None, "action": None, "reason": None, "score": None,
        "price": 75, "wallet": None,
        "detail": '{"item": "Health", "category": "DEFENSE", '
                  '"coins_before": 1770, "gems_before": null, "dry_run": false}',
    })

    written = ledger.backfill(conn)

    assert written == 1
    line = db.ledger_page(conn)[0]
    assert (line["kind"], line["item"], line["delta"]) == ("WORKSHOP_BUY", "Health", -75)
    assert line["balance_after"] == 1695


def test_backfill_is_a_one_time_migration_not_a_repair(tmp_path: Path) -> None:
    """Derived UNEXPLAINED lines have no seq, so the unique index cannot
    dedupe them - a second unguarded replay would duplicate every one."""
    conn = db.connect(tmp_path / "bot.db")
    db.insert_event(conn, {
        "seq": 1, "run_id": None, "ts": 1.0, "type": "Purchased",
        "screen": None, "action": None, "reason": None, "score": None,
        "price": 75, "wallet": None,
        "detail": '{"item": "Health", "category": "DEFENSE", '
                  '"coins_before": 1770, "gems_before": null, "dry_run": false}',
    })

    assert ledger.backfill(conn) == 1
    assert ledger.backfill(conn) == 0
    assert len(db.ledger_page(conn)) == 1


def test_backfill_over_an_empty_events_table_writes_nothing(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "bot.db")

    assert ledger.backfill(conn) == 0


def test_backfill_replays_a_milestone_claim(tmp_path: Path) -> None:
    """The only thing that proves `_rebuild` can reconstruct a MilestoneClaimed
    from a stored row's detail JSON, rather than just its own class."""
    conn = db.connect(tmp_path / "bot.db")
    db.insert_event(conn, {
        "seq": 1, "run_id": None, "ts": 1.0, "type": "MilestoneClaimed",
        "screen": None, "action": None, "reason": None, "score": None,
        "price": None, "wallet": None,
        "detail": '{"reward_text": "25 COINS", "currency": "coins", '
                  '"amount": 25, "tier": 1}',
    })

    written = ledger.backfill(conn)

    assert written == 1
    line = db.ledger_page(conn)[0]
    assert (line["kind"], line["item"], line["delta"]) == (
        "MILESTONE_CLAIM", "25 COINS", 25)


def test_backfill_replays_an_uncertain_claim(tmp_path: Path) -> None:
    """The only thing that proves `_rebuild` can reconstruct a ClaimUncertain
    from a stored row, and specifically the branch that matters: unlike
    MilestoneClaimed, ClaimUncertain has a `reason` field, which `store.to_row`
    pops into its own `reason` COLUMN rather than the detail JSON blob - so
    this is the row shape that actually reaches `_rebuild` in production."""
    conn = db.connect(tmp_path / "bot.db")
    db.insert_event(conn, {
        "seq": 1, "run_id": None, "ts": 1.0, "type": "ClaimUncertain",
        "screen": None, "action": None, "reason": "claim_not_confirmed",
        "score": None, "price": None, "wallet": None,
        "detail": '{"target": "missions", '
                  '"detail": "\\"Mission 0\\" was tapped but not confirmed."}',
    })

    written = ledger.backfill(conn)

    assert written == 1
    line = db.ledger_page(conn)[0]
    assert (line["kind"], line["reason"], line["delta"]) == (
        "CLAIM_UNCERTAIN", "claim_not_confirmed", None)


# --- mission claims -------------------------------------------------------

def test_a_mission_claim_credits_both_currencies_as_two_lines() -> None:
    """One claim, two balances. This is why classify returns a tuple."""
    coins, gems = ledger.classify(
        events.MissionClaimed(mission="Kill 200 basic enemies",
                              mission_id="kill_200_basic_enemies",
                              coins=25, gems=3, completed_before=0,
                              completed_after=1, gems_before=60,
                              seq=20, ts=1000.0))
    assert (coins.kind, coins.currency, coins.delta) == ("MISSION_CLAIM", "coins", 25)
    assert (gems.kind, gems.currency, gems.delta) == ("MISSION_CLAIM", "gems", 3)
    assert coins.item == gems.item == "Kill 200 basic enemies"


def test_a_claim_never_treats_the_abbreviated_coin_balance_as_a_reading() -> None:
    """The page shows coins as "6.08K". Handing that to the reconciler as a
    balance would contradict the running total and manufacture an UNEXPLAINED
    line on every single claim. Gems read exactly and are safe."""
    coins, gems = ledger.classify(
        events.MissionClaimed(mission="Start 1 battles", mission_id="start_1_battles",
                              coins=25, gems=3, completed_before=1,
                              completed_after=2, gems_before=63,
                              seq=21, ts=1000.0))
    assert coins.observed is None
    assert gems.observed == 63


def test_an_unread_reward_is_an_unknown_delta_not_a_zero_one() -> None:
    coins, gems = ledger.classify(
        events.MissionClaimed(mission="Kill 5 bosses", mission_id="kill_5_bosses",
                              coins=None, gems=None, completed_before=2,
                              completed_after=3, gems_before=None,
                              seq=22, ts=1000.0))
    assert coins.delta is None and gems.delta is None


def test_the_two_claim_lines_reconcile_against_separate_balances(tmp_path: Path) -> None:
    """An unreadable coin amount must not stall the gem chain."""
    write, _ = writer(tmp_path)
    lines = write.lines_for(
        events.MissionClaimed(mission="Buy 20 battle upgrades",
                              mission_id="buy_20_battle_upgrades",
                              coins=None, gems=3, completed_before=3,
                              completed_after=4, gems_before=63,
                              seq=23, ts=1000.0))
    gems = [line for line in lines if line.currency == "gems"]
    assert len(gems) == 1 and gems[0].balance_after == 66


def test_a_claim_walk_bookends_reuse_the_visit_lines() -> None:
    (start,) = ledger.classify(events.ClaimStarted(target="missions", seq=24, ts=1.0))
    (end,) = ledger.classify(
        events.ClaimEnded(target="missions", claimed=3, reason="claimed", seq=25, ts=2.0))
    assert start.kind == "VISIT_START"
    assert (end.kind, end.detail["claimed"]) == ("VISIT_END", 3)


def test_a_refused_claim_is_a_line_that_provably_moved_nothing() -> None:
    (line,) = ledger.classify(
        events.ClaimSkipped(target="missions", reason="counter_unreadable",
                            detail="The completed counter could not be read.",
                            seq=26, ts=1000.0))
    assert (line.kind, line.delta, line.reason) == (
        "CLAIM_SKIPPED", 0, "counter_unreadable")


def test_a_coin_milestone_yields_one_line_that_never_anchors_a_balance() -> None:
    """The header abbreviates coins ('6.08K'). An abbreviated reading handed to
    the reconciler as a balance contradicts the running total and manufactures
    an UNEXPLAINED line on every claim."""
    (line,) = ledger.classify(events.MilestoneClaimed(
        seq=1, ts=100., reward_text='25 COINS', currency='coins',
        amount=25, tier=1))
    assert (line.kind, line.currency, line.delta) == ('MILESTONE_CLAIM', 'coins', 25)
    assert line.observed is None


def test_a_gem_milestone_yields_a_gems_line() -> None:
    (line,) = ledger.classify(events.MilestoneClaimed(
        seq=1, ts=100., reward_text='15GEMS', currency='gems', amount=15, tier=1))
    assert (line.currency, line.delta) == ('gems', 15)


def test_unlock_lab_moved_no_currency_which_is_not_an_unreadable_amount() -> None:
    """delta=0 is 'provably moved nothing'. delta=None is 'moved by an unknown
    amount'. `Unlock Lab` is the first."""
    (line,) = ledger.classify(events.MilestoneClaimed(
        seq=1, ts=100., reward_text='Unlock Lab', currency=None,
        amount=None, tier=1))
    assert line.currency is None
    assert line.delta == 0


def test_an_unread_milestone_reward_moved_an_unknown_amount_not_nothing() -> None:
    """reward_text=None is the modal's reward line never being read - a third
    state parse_frame explicitly produces (_modal_reward returns None on 0 or
    2+ boxes in the band). It is not the same fact as `Unlock Lab`, which DID
    read and just names no currency."""
    (line,) = ledger.classify(events.MilestoneClaimed(
        seq=1, ts=100., reward_text=None, currency=None, amount=None, tier=1))
    assert line.currency is None
    assert line.delta is None


def test_an_uncertain_claim_moved_an_unknown_amount_not_nothing() -> None:
    """This is the distinction the parked finding was about. A CLAIM that was
    TAPPED and never confirmed may well have taken a reward, so it must not
    share ClaimSkipped's delta=0, which asserts nothing moved."""
    (line,) = ledger.classify(events.ClaimUncertain(
        seq=1, ts=100., target='milestones', reason='claim_not_confirmed',
        detail='"25 COINS" was tapped but the modal did not close.'))
    assert line.kind == 'CLAIM_UNCERTAIN'
    assert line.delta is None
    assert line.currency is None
    assert line.reason == 'claim_not_confirmed'


def test_every_claim_event_can_be_replayed_from_the_events_table() -> None:
    """A ledger that cannot be rebuilt from events is not a ledger."""
    for name in ("ClaimStarted", "MissionClaimed", "ClaimSkipped", "ClaimEnded",
                 "MilestoneClaimed", "ClaimUncertain"):
        assert name in ledger._REPLAYABLE


# -- the floating gem -------------------------------------------------------


def test_a_floating_gem_claim_credits_gems() -> None:
    (line,) = ledger.classify(
        events.FloatingGemClaimed(point=(560, 700), gems_before=60, gems_after=62,
                                  delta=2, run_id=7, seq=11, ts=1000.0)
    )

    assert (line.kind, line.currency, line.delta) == ("GEM_CLAIM", "gems", 2)
    assert (line.observed, line.balance_after) == (60, 62)
    assert line.run_id == 7


def test_a_floating_gem_claim_is_a_credit_not_a_purchase() -> None:
    """It costs nothing, so `price` must stay None rather than become 0.

    price=0 would read as "bought for free", which is a different claim
    about the world than "was given".
    """
    (line,) = ledger.classify(
        events.FloatingGemClaimed(point=(560, 700), gems_before=60, gems_after=62,
                                  delta=2, seq=11, ts=1000.0)
    )

    assert line.price is None
    assert line.delta > 0


def test_an_unconfirmed_floating_gem_reuses_the_uncertain_kind() -> None:
    """No new ledger kind for the failure path.

    A floating gem tapped and never confirmed is exactly what
    ClaimUncertain and CLAIM_UNCERTAIN already describe, so the claim
    publishes that event rather than a bespoke one - which is also why
    delta lands as None and not 0.
    """
    (line,) = ledger.classify(
        events.ClaimUncertain(target="floating_gem", reason="counter_unchanged",
                              detail="tapped 3x at (560, 700)", seq=12, ts=1000.0)
    )

    assert (line.kind, line.delta) == ("CLAIM_UNCERTAIN", None)
    assert line.detail["target"] == "floating_gem"


# --- the dashboard's copies of these lists --------------------------------
#
# web/ui/lib/ledger.ts keeps two hand-written lists that must mirror this
# module: the kinds a reader can filter by, and the events that should make
# the page refetch. They drifted once already - both stopped at the shopping
# events, so the claim rows (the only multi-currency ones) could neither be
# filtered to nor appeared until something unrelated triggered a reload.
# Nothing failed, because nothing compared them. These do.

_LEDGER_TS = Path(__file__).resolve().parent.parent / "web" / "ui" / "lib" / "ledger.ts"

# ledger.KINDS ends with six kinds nothing emits yet. The page deliberately
# leaves them out: a chip for them could only ever return nothing.
_RESERVED_KINDS = {"CARD_SLOT", "MODULE", "RELIC", "UW", "MANUAL"}


def _ts_strings(opener: str) -> set[str]:
    """The quoted strings in the TypeScript literal that starts at `opener`."""
    source = _LEDGER_TS.read_text()
    start = source.index(opener) + len(opener)
    body = source[start:source.index("]", start)]
    return set(re.findall(r'"([A-Za-z_]+)"', body))


def test_the_reserved_kinds_are_the_tail_of_the_kind_list() -> None:
    """The comment on KINDS promises the reserved ones come last. If a new
    real kind is appended after them instead, the page test below would
    quietly treat it as reserved."""
    assert set(ledger.KINDS[-len(_RESERVED_KINDS):]) == _RESERVED_KINDS


def test_the_page_offers_a_filter_for_every_kind_the_ledger_emits() -> None:
    emitted = set(ledger.KINDS) - _RESERVED_KINDS

    assert _ts_strings("export const FILTER_KINDS = [") == emitted


def test_the_page_refetches_on_every_event_that_writes_a_ledger_line() -> None:
    assert _ts_strings("export const LEDGER_EVENTS = new Set([") == set(ledger._REPLAYABLE)
