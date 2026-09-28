from __future__ import annotations

import pytest

import events
from sinks.state import BotState


def stamped(event: events.Event) -> events.Event:
    return events.EventBus().publish(event)


def test_a_run_opens_and_closes_the_current_run_panel() -> None:
    state = BotState()

    state.apply(stamped(events.RunStarted(run_id=4)))
    assert state.snapshot()["run"]["id"] == 4

    state.apply(stamped(events.RunEnded(run_id=4, duration=60.0)))
    assert state.snapshot()["run"] is None
    assert state.snapshot()["runs_completed"] == 1


def test_taps_are_counted_both_overall_and_for_the_current_run() -> None:
    state = BotState()
    state.apply(stamped(events.Tapped(action="Damage", x=1, y=2, score=0.9)))
    state.apply(stamped(events.RunStarted(run_id=1)))
    state.apply(stamped(events.Tapped(action="Damage", x=1, y=2, score=0.9)))

    snapshot = state.snapshot()

    assert snapshot["taps"]["Damage"] == 2
    assert snapshot["run"]["taps"]["Damage"] == 1


def test_the_wallet_tracks_the_latest_scan() -> None:
    state = BotState()
    state.apply(stamped(events.ScanCompleted(screen="IN_RUN", duration_ms=9.0, wallet=450)))

    assert state.snapshot()["wallet"] == 450


def test_skips_are_counted_by_reason() -> None:
    state = BotState()
    state.apply(stamped(events.Skipped(action="Damage", reason="unaffordable")))
    state.apply(stamped(events.Skipped(action="Damage", reason="unaffordable")))
    state.apply(stamped(events.Skipped(action="*", reason="screen_gated")))

    assert state.snapshot()["skips"] == {"unaffordable": 2, "screen_gated": 1}


def test_the_snapshot_is_json_serialisable() -> None:
    """It is handed straight to FastAPI; a Counter in it would 500 the route."""
    import json

    state = BotState()
    state.apply(stamped(events.RunStarted(run_id=1)))
    state.apply(stamped(events.Tapped(action="Damage", x=1, y=2, score=0.9)))
    state.apply(stamped(events.BotError(message="device gone")))

    json.dumps(state.snapshot())  # must not raise


def test_the_last_error_is_kept() -> None:
    state = BotState()
    state.apply(stamped(events.BotError(message="device gone")))

    assert state.snapshot()["last_error"] == "device gone"


def test_the_last_error_clears_on_the_next_successful_scan() -> None:
    """M8: a single transient EmulatorError must not pin a red line in the
    dashboard header for the rest of the session - ScanCompleted only fires
    once a scan finishes without raising, so seeing one means it recovered."""
    state = BotState()
    state.apply(stamped(events.BotError(message="device gone")))
    assert state.snapshot()["last_error"] == "device gone"

    state.apply(stamped(events.ScanCompleted(screen="IN_RUN", duration_ms=9.0)))

    assert state.snapshot()["last_error"] is None


def test_reset_clears_the_previous_bot_s_accumulators() -> None:
    """BotState outlives a bot now, so its counters must be resettable.

    Without this the status bar shows a stopped bot's scan count beside a
    fresh bot's uptime, which reads as one impossibly slow session.
    """
    import time

    import events
    from sinks.state import BotState

    state = BotState()
    state.apply(events.ScanCompleted(seq=1, ts=time.time(), screen="IN_RUN", duration_ms=1.0))
    state.apply(events.Tapped(seq=2, ts=time.time(), action="Damage", x=1, y=2, score=0.9))
    state.apply(events.BotError(seq=3, ts=time.time(), message="boom"))
    assert state.snapshot()["scans"] == 1

    state.reset()
    after = state.snapshot()
    assert after["scans"] == 0
    assert after["taps"] == {}
    assert after["last_error"] is None
    assert after["run"] is None
    assert after["uptime"] < 1.0


def test_reset_is_safe_while_events_are_arriving() -> None:
    import threading
    import time

    import events
    from sinks.state import BotState

    state = BotState()
    stop = threading.Event()

    def feed() -> None:
        while not stop.is_set():
            state.apply(
                events.ScanCompleted(seq=1, ts=time.time(), screen="IN_RUN", duration_ms=1.0)
            )

    thread = threading.Thread(target=feed)
    thread.start()
    try:
        for _ in range(100):
            state.reset()
            state.snapshot()
    finally:
        stop.set()
        thread.join()


def test_an_autopilot_decision_is_kept_until_the_run_ends() -> None:
    state = BotState()
    state.apply(stamped(events.RunStarted(run_id=3)))
    state.apply(stamped(events.AutopilotDecided(phase="buying", reason="cheapest attack",
                                                upgrade_id="damage")))

    decision = state.snapshot()["decision"]
    assert (decision["phase"], decision["reason"], decision["upgrade_id"]) == (
        "buying", "cheapest attack", "damage")
    assert decision["at"] > 0

    state.apply(stamped(events.RunEnded(run_id=3, duration=10.0)))
    assert state.snapshot()["decision"] is None


@pytest.mark.parametrize(("event", "label"), [
    (events.Navigated(target="RETRY"), "Navigating · RETRY"),
    (events.ShoppingStarted(visit=2, dry_run=False), "Shopping"),
    (events.ShoppingStarted(visit=3, dry_run=True), "Shopping · dry run"),
    (events.ClaimStarted(target="mail"), "Claiming · mail"),
])
def test_navigation_shopping_and_claims_set_the_current_activity(
        event: events.Event, label: str) -> None:
    state = BotState()
    published = stamped(event)

    state.apply(published)

    assert state.snapshot()["activity"] == {"label": label, "at": published.ts}
    # These events always wrote a feed line; recording them must not stop that.
    assert len(state.snapshot()["tail"]) == 1


def test_the_wave_follows_in_run_scans_and_clears_off_the_run() -> None:
    state = BotState()
    assert state.snapshot()["wave"] is None

    state.apply(stamped(events.ScanCompleted(screen="IN_RUN", duration_ms=9.0, wave=4812)))
    assert state.snapshot()["wave"] == 4812
    # An IN_RUN scan that read no wave keeps the last one.
    state.apply(stamped(events.ScanCompleted(screen="IN_RUN", duration_ms=9.0)))
    assert state.snapshot()["wave"] == 4812

    state.apply(stamped(events.ScanCompleted(screen="GAME_OVER", duration_ms=9.0)))
    assert state.snapshot()["wave"] is None


def test_reset_forgets_the_decision_the_activity_and_the_wave() -> None:
    state = BotState()
    state.apply(stamped(events.ScanCompleted(screen="IN_RUN", duration_ms=9.0, wave=12)))
    state.apply(stamped(events.AutopilotDecided(phase="saving", reason="x")))
    state.apply(stamped(events.ClaimStarted(target="missions")))

    state.reset()

    snapshot = state.snapshot()
    assert (snapshot["wave"], snapshot["decision"], snapshot["activity"]) == (None, None, None)
