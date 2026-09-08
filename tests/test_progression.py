from __future__ import annotations

import sqlite3
from pathlib import Path

import db
import events
from progression import compare_tiers
from sinks.store import StoreSink


def test_existing_database_adds_purpose_and_defaults_legacy_runs_to_farm(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as legacy:
        legacy.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at REAL NOT NULL)")
        legacy.execute("INSERT INTO runs VALUES (1, 100)")
    conn = db.connect(path)
    try:
        assert db.list_runs(conn)[0]["purpose"] == "farm"
        conn.close()
        conn = db.connect(path)
        assert db.list_runs(conn)[0]["purpose"] == "farm"
    finally:
        conn.close()


def test_started_milestone_purpose_survives_run_completion(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "bot.db")
    sink = StoreSink(tmp_path / "bot.db")
    sink._conn = conn
    try:
        sink.handle(events.RunStarted(run_id=1, seq=1, ts=100, purpose="milestone"))
        sink.handle(events.RunEnded(run_id=1, seq=2, ts=200, duration=100, tier=2, coins=1000, wave=100))
        run = db.list_runs(conn)[0]
        assert run["purpose"] == "milestone"
        assert run["ended_at"] == 200
        assert db.run_events(conn, 1)[0]["detail"]["purpose"] == "milestone"
    finally:
        conn.close()


def test_legacy_started_events_and_direct_database_call_default_to_farm(tmp_path: Path) -> None:
    assert events.RunStarted(run_id=1).purpose == "farm"
    conn = db.connect(tmp_path / "bot.db")
    try:
        db.start_run(conn, 1, 100)
        assert db.list_runs(conn)[0]["purpose"] == "farm"
    finally:
        conn.close()


def test_milestone_runs_do_not_bias_farming_recommendations() -> None:
    runs = [dict(tier=1, started_at=0, ended_at=3600, coins=100, wave=100) for _ in range(3)]
    runs += [dict(tier=2, started_at=0, ended_at=1, coins=1000, wave=200, purpose="milestone") for _ in range(3)]
    comparison = compare_tiers(runs)
    assert comparison["recommended_tier"] == 1
    assert [tier["tier"] for tier in comparison["tiers"]] == [1]
    assert comparison["tiers"][0]["coins_per_hour"] == 100


def test_only_milestone_runs_cannot_establish_a_farming_baseline() -> None:
    runs = [dict(tier=2, started_at=0, ended_at=60, coins=1000, purpose="milestone") for _ in range(3)]
    assert compare_tiers(runs)["recommended_tier"] is None


# -- rates ---------------------------------------------------------------
def run(tier: int = 1, coins: int = 100, hours: float = 1.,
        purpose: str = "farm", **overrides: object) -> dict:
    row = {"tier": tier, "coins": coins, "started_at": 0.,
           "ended_at": hours * 3600, "abandoned": 0, "wave": 16,
           "purpose": purpose}
    row.update(overrides)
    return row


def test_three_farm_runs_establish_a_rate() -> None:
    from progression import rates

    result = rates([run(), run(), run()], tier=1)
    assert result.coins_per_hour == 100.
    assert result.samples == 3


def test_fewer_than_three_runs_yields_no_rate_rather_than_a_noisy_one() -> None:
    """The same bar compare_tiers already sets for a recommendation. Two runs
    is not a baseline, and a horizon computed from one is a guess."""
    from progression import rates

    result = rates([run(), run()], tier=1)
    assert result.coins_per_hour is None
    assert result.samples == 2
    assert "three" in result.reason


def test_no_runs_at_all_yields_no_rate() -> None:
    from progression import rates

    assert rates([], tier=1).coins_per_hour is None


def test_a_measured_zero_income_rate_is_zero_not_unmeasured() -> None:
    """Three or more farm runs that all earned nothing is a real
    measurement, not a missing one: the horizon at this rate is infinite,
    which is an actionable "will never pay off", not "we don't know yet"."""
    from progression import rates

    result = rates([run(coins=0), run(coins=0), run(coins=0)], tier=1)
    assert result.coins_per_hour == 0.0
    assert result.samples == 3
    assert "zero" in result.reason.lower()


def test_measured_zero_and_unmeasured_are_distinguishable_by_reason() -> None:
    """The same coins_per_hour question - "can I plan against this?" - gets
    two different None-adjacent answers depending on whether we measured at
    all. samples < MIN_SAMPLES means "go gather more data" (None); samples
    >= MIN_SAMPLES with zero pooled coins means "this will never pay off at
    this rate" (0.0). The two must not share a reason string."""
    from progression import rates

    unmeasured = rates([run(), run()], tier=1)
    measured_zero = rates([run(coins=0), run(coins=0), run(coins=0)], tier=1)
    assert unmeasured.coins_per_hour is None
    assert measured_zero.coins_per_hour == 0.0
    assert unmeasured.reason != measured_zero.reason
    assert "three" in unmeasured.reason
    assert "three" not in measured_zero.reason


def test_milestone_pushes_do_not_pollute_the_farming_rate() -> None:
    """compare_tiers already filters purpose != farm; rates must agree, or a
    push run's low payout would depress the horizon it is being used to plan."""
    from progression import rates

    result = rates([run(), run(), run(), run(coins=0, purpose="milestone")], tier=1)
    assert result.coins_per_hour == 100.
    assert result.samples == 3


def test_another_tier_s_runs_are_not_counted() -> None:
    from progression import rates

    result = rates([run(tier=1), run(tier=1), run(tier=1),
                    run(tier=2, coins=1000)], tier=1)
    assert result.coins_per_hour == 100.


def test_an_unspecified_tier_uses_the_most_recently_run_one() -> None:
    from progression import rates

    result = rates([run(tier=2, coins=200), run(tier=2, coins=200),
                    run(tier=2, coins=200)])
    assert result.tier == 2
    assert result.coins_per_hour == 200.


def test_abandoned_and_impossible_runs_are_excluded() -> None:
    """The same guards compare_tiers applies: end <= start, negative coins, a
    null field, an abandoned run."""
    from progression import rates

    good = [run(), run(), run()]
    assert rates(good + [run(abandoned=1, coins=99999)], tier=1).samples == 3
    assert rates(good + [run(ended_at=0.)], tier=1).samples == 3
    assert rates(good + [run(coins=-5)], tier=1).samples == 3
    assert rates(good + [run(coins=None)], tier=1).samples == 3


def test_compare_tiers_still_answers_exactly_as_before() -> None:
    """rates is additive. The existing caller - GET /api/autopilot - must not
    shift by a single field."""
    from progression import compare_tiers

    result = compare_tiers([run(), run(), run()])
    assert result["recommended_tier"] == 1
    assert result["tiers"][0]["coins_per_hour"] == 100.
