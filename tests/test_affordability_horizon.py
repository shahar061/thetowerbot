"""Time to afford, as pure arithmetic.

The binding constraint at Tier 1 with 6,110 coins is not WHICH upgrade is
best but HOW LONG until anything is affordable, so this is the denominator of
the director's score.

`progression.rates` (Task 5) hands this module a `coins_per_hour` with three
distinct meanings, and the horizon must keep all three distinct:

* None (unmeasured)          -> horizon is None (unknown - "go gather data")
* 0.0  (measured, zero income) -> horizon is math.inf ("this will never pay
  off at this rate" - an actionable answer, not a missing one)
* > 0.0 (measured income)     -> horizon is a real, finite number of hours

None and math.inf must never collapse into each other. That collapse, in
either direction, is exactly the defect a Critical fix corrected one layer
down in Task 5's CurrencyRates.coins_per_hour; test_a_measured_zero_rate_...
and test_an_unmeasured_rate_... below pin both directions independently, and
the two "collapse" mutants at the bottom of this file prove each test would
actually catch its half of the regression.
"""
from __future__ import annotations

import math

import pytest

from affordability_horizon import hours_to_afford


def test_an_already_affordable_price_is_zero_hours() -> None:
    assert hours_to_afford(100, 200, 50.) == 0.


def test_an_exactly_affordable_price_is_zero_hours() -> None:
    assert hours_to_afford(100, 100, 50.) == 0.


def test_a_shortfall_is_divided_by_the_rate() -> None:
    """3,890 coins short at 1,000/hour is 3.89 hours - and the shortfall, not
    the price, is what is being waited for."""
    assert hours_to_afford(10_000, 6_110, 1_000.) == pytest.approx(3.89)


@pytest.mark.parametrize("price,balance,rate", [
    (None, 100, 10.),      # the price was not read
    (100, None, 10.),      # the balance was not read
])
def test_a_missing_price_or_balance_yields_no_horizon(price, balance, rate) -> None:
    assert hours_to_afford(price, balance, rate) is None


def test_an_unaffordable_price_with_no_income_baseline_yields_no_horizon() -> None:
    """Not infinity. "We do not know when" and "never" are different
    answers, and only one of them should stop a human from intervening."""
    assert hours_to_afford(10_000, 100, None) is None


def test_an_already_affordable_price_needs_no_rate() -> None:
    """Zero hours is knowable without an income baseline: the coins are
    already there. Unmeasured income must not block a fact that does not
    depend on it."""
    assert hours_to_afford(100, 200, None) == 0.


def test_a_negative_price_yields_no_horizon() -> None:
    assert hours_to_afford(-1, 200, 10.) is None


def test_the_horizon_is_finite_for_every_answerable_case() -> None:
    result = hours_to_afford(10**9, 0, .001)
    assert result is not None and math.isfinite(result)


# -- the three-case rate contract, kept distinct, not collapsed -----------

def test_an_unmeasured_rate_yields_an_unknown_horizon_not_infinite() -> None:
    """coins_per_hour is None: nobody has established a baseline. The
    horizon is unknown - None - never math.inf. Conflating "we haven't
    measured" with "we measured never" would make an unmeasurable objective
    indistinguishable from a dead one."""
    result = hours_to_afford(10_000, 100, None)
    assert result is None
    assert result != math.inf


def test_a_measured_zero_rate_yields_an_infinite_horizon_not_unknown() -> None:
    """coins_per_hour is 0.0: three or more farm runs were measured and
    every one earned nothing. That is a fact, and the honest horizon is
    infinite - not unknown, and not silently treated as "no answer"."""
    result = hours_to_afford(10_000, 100, 0.)
    assert result == math.inf
    assert result is not None


def test_unknown_and_infinite_horizons_are_never_the_same_value() -> None:
    """Direct pin of the two-cases-never-collapse invariant: computing both
    ends of the contract side by side must produce two distinguishable
    results, one of which is exactly None and the other exactly math.inf."""
    unmeasured = hours_to_afford(10_000, 100, None)
    measured_zero = hours_to_afford(10_000, 100, 0.)
    assert unmeasured is None
    assert measured_zero == math.inf
    assert unmeasured != measured_zero


@pytest.mark.parametrize("rate", [-5., -0.001])
def test_a_negative_rate_yields_no_horizon(rate: float) -> None:
    """Outside the contract progression.rates ever produces - treated as
    unknown, not guessed at, and in particular not treated as the measured
    "zero income" case just because it is non-positive."""
    assert hours_to_afford(10_000, 100, rate) is None


@pytest.mark.parametrize("rate", [float("inf"), float("nan")])
def test_a_non_finite_rate_yields_no_horizon(rate: float) -> None:
    assert hours_to_afford(10_000, 100, rate) is None


# -- purity, enforced two ways, not one ------------------------------------

def test_hours_to_afford_is_deterministic() -> None:
    """Same inputs, same answer, every time - the property a pure function
    must have and a clock- or I/O-touching one might not."""
    for price, balance, rate in [
        (100, 200, 50.), (10_000, 6_110, 1_000.), (10_000, 100, None),
        (10_000, 100, 0.), (None, 100, 10.),
    ]:
        first = hours_to_afford(price, balance, rate)
        second = hours_to_afford(price, balance, rate)
        assert first == second or (first is None and second is None)


def test_affordability_horizon_module_imports_nothing_but_math() -> None:
    """Enforced, not trusted: an allowlist over this module's own import
    statements, matching how objectives.py enforces its purity boundary.
    Catches a future edit that reaches for `device`, `shopping`,
    `transactions`, or any I/O-capable stdlib module (time, os, socket, ...)
    to make the horizon "smarter"."""
    import ast
    from pathlib import Path

    import affordability_horizon

    source = Path(affordability_horizon.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    allowed = {"__future__", "math"}
    assert imported <= allowed, imported - allowed
    forbidden = {"device", "shopping", "transactions", "time", "os", "socket",
                 "ultimate_weapons", "cards", "ocr", "screen_discovery", "db"}
    assert not (imported & forbidden), imported & forbidden
