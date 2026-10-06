from __future__ import annotations

import pytest

from screens import ScreenReading, ScreenState, ScreenTracker


def reading(state: ScreenState) -> ScreenReading:
    return ScreenReading(state, confidence=1.0, scores={})


def test_starts_with_nothing_confirmed() -> None:
    """UNKNOWN is the tracker's starting *value*, never an observation.

    Everything that reacts to UNKNOWN - snapshotting the frame, publishing an
    UnknownScreen event - must be able to tell "I have not looked yet" from
    "I looked and did not recognise it", or scan 1 of every launch snapshots
    a perfectly recognisable screen and poisons the diagnostic trail.
    """
    tracker = ScreenTracker()

    assert tracker.confirmed is False
    assert tracker.state is ScreenState.UNKNOWN


def test_a_confirmed_unknown_is_distinguishable_from_the_initial_one() -> None:
    tracker = ScreenTracker(confirmations=2)

    tracker.observe(reading(ScreenState.UNKNOWN))
    assert tracker.confirmed is False  # one reading is not a confirmation

    assert tracker.observe(reading(ScreenState.UNKNOWN)) is ScreenState.UNKNOWN
    assert tracker.confirmed is True
    assert tracker.state is ScreenState.UNKNOWN


def test_confirming_any_state_flips_confirmed() -> None:
    tracker = ScreenTracker(confirmations=2)

    tracker.observe(reading(ScreenState.MAIN_MENU))
    assert tracker.confirmed is False

    tracker.observe(reading(ScreenState.MAIN_MENU))
    assert tracker.confirmed is True


def test_a_single_reading_does_not_transition() -> None:
    tracker = ScreenTracker(confirmations=2)
    assert tracker.observe(reading(ScreenState.MAIN_MENU)) is None
    assert tracker.state is ScreenState.UNKNOWN


def test_two_consecutive_readings_confirm_the_transition() -> None:
    tracker = ScreenTracker(confirmations=2)
    tracker.observe(reading(ScreenState.MAIN_MENU))

    assert tracker.observe(reading(ScreenState.MAIN_MENU)) is ScreenState.MAIN_MENU
    assert tracker.state is ScreenState.MAIN_MENU


def test_an_interrupted_sequence_does_not_transition() -> None:
    """A mid-fade frame between two stable ones must not slip through."""
    tracker = ScreenTracker(confirmations=2)
    tracker.observe(reading(ScreenState.MAIN_MENU))
    tracker.observe(reading(ScreenState.MAIN_MENU))

    tracker.observe(reading(ScreenState.IN_RUN))
    tracker.observe(reading(ScreenState.GAME_OVER))  # interrupts the run of IN_RUN

    assert tracker.state is ScreenState.MAIN_MENU


def test_staying_on_the_same_screen_reports_no_transition() -> None:
    tracker = ScreenTracker(confirmations=2)
    tracker.observe(reading(ScreenState.IN_RUN))
    tracker.observe(reading(ScreenState.IN_RUN))

    assert tracker.observe(reading(ScreenState.IN_RUN)) is None
    assert tracker.observe(reading(ScreenState.IN_RUN)) is None


def test_consecutive_transitions_both_fire() -> None:
    tracker = ScreenTracker(confirmations=2)
    for _ in range(2):
        tracker.observe(reading(ScreenState.MAIN_MENU))
    for _ in range(2):
        result = tracker.observe(reading(ScreenState.IN_RUN))

    assert result is ScreenState.IN_RUN
    assert tracker.state is ScreenState.IN_RUN


def test_unknown_is_a_state_like_any_other() -> None:
    """UNKNOWN still needs confirming, so one bad frame cannot halt the bot."""
    tracker = ScreenTracker(confirmations=2)
    for _ in range(2):
        tracker.observe(reading(ScreenState.IN_RUN))

    assert tracker.observe(reading(ScreenState.UNKNOWN)) is None
    assert tracker.state is ScreenState.IN_RUN

    assert tracker.observe(reading(ScreenState.UNKNOWN)) is ScreenState.UNKNOWN


def test_returning_to_current_state_resets_a_partial_streak() -> None:
    """Returning to confirmed state mid-streak must reset the pending count.

    A buggy implementation that skips resetting _pending/_streak would wrongly
    commit a transition on the next reading of the candidate. This test detects
    that regression.
    """
    tracker = ScreenTracker(confirmations=2)

    # Step 1: confirm state A
    tracker.observe(reading(ScreenState.MAIN_MENU))
    tracker.observe(reading(ScreenState.MAIN_MENU))
    assert tracker.state is ScreenState.MAIN_MENU

    # Step 2: observe B once
    tracker.observe(reading(ScreenState.IN_RUN))

    # Step 3: return to current state A; MUST reset the pending streak
    tracker.observe(reading(ScreenState.MAIN_MENU))

    # Step 4: observe B once; must be treated as FRESH streak, not a continuation
    assert tracker.observe(reading(ScreenState.IN_RUN)) is None
    assert tracker.state is ScreenState.MAIN_MENU

    # Step 5: only now does the second B reading fire the transition
    assert tracker.observe(reading(ScreenState.IN_RUN)) is ScreenState.IN_RUN


def test_pending_is_true_only_while_a_streak_builds() -> None:
    tracker = ScreenTracker(confirmations=2)
    assert tracker.pending is False
    tracker.observe(reading(ScreenState.MAIN_MENU))
    assert tracker.pending is True
    tracker.observe(reading(ScreenState.MAIN_MENU))
    assert tracker.pending is False
