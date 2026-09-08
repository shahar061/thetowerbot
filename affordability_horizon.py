"""How long until a price is affordable, in hours.

The denominator of the director's score. At Tier 1 with 6,110 coins the
binding constraint is not which upgrade is best but how long until anything
is affordable, so an objective that is theoretically optimal but eleven days
away loses to one that lands this afternoon.

``progression.rates`` (Task 5) hands this module a ``coins_per_hour`` that
carries three distinct meanings, and the horizon keeps all three distinct
rather than collapsing any pair of them:

* ``None``  - unmeasured. Nobody has established an income baseline for this
  tier yet. The horizon is unknown (``None``), and the objective it gates is
  blocked: not satisfied, not ready, just not yet knowable. This is a
  "go gather data" answer.
* ``0.0``   - measured, and this tier earns nothing. The horizon is
  ``math.inf``: an explicit, actionable "this will never pay off at this
  rate", not a missing observation. This is a "stop waiting on this,
  something else has to change" answer.
* ``> 0.0`` - measured income. The horizon is a real, finite number of
  hours.

``None`` and ``math.inf`` are trivially distinguishable in the ``float |
None`` return type (``result is None`` vs. ``math.isinf(result)``), so no
sentinel object or richer return shape is needed to keep "unknown" and
"infinite" apart - unlike ``coins_per_hour`` itself, which needs the
adjoining ``reason`` string on ``CurrencyRates`` to explain *why* it is
``None`` vs. ``0.0``. Here the two callers of a horizon ("is this ready?"
and "how long do I wait?") only need the numeric distinction, so nothing
richer is warranted.

Deliberately never produced by letting a ``ZeroDivisionError`` happen and
translating that into infinity: a zero rate is turned into ``math.inf``
explicitly, before any division is attempted, so the infinite case is a
decision in the code, not an artifact of catching an exception.

An earlier draft of this module (recorded in the task brief) treated *any*
non-positive rate - ``None`` (unmeasured) and ``0.0`` (measured, zero
income) alike - as "no horizon" (``None``). That is the same collapse Task
5's own reference implementation made for ``CurrencyRates.coins_per_hour``
and was corrected for; doing it here would reintroduce the defect one layer
up, silently telling the director "go gather more data" about a tier that
has already been measured to be a dead end. This implementation keeps the
two apart: ``rate_per_hour is None`` returns ``None``; ``rate_per_hour ==
0.0`` returns ``math.inf``.

A negative or non-finite (``nan``, ``inf``) rate is outside the contract
``progression.rates`` ever produces - defensively treated as unknown
(``None``) rather than guessed at, the same stance this module takes for any
other missing or unreadable observation.

Pure arithmetic: no I/O, no clock, no device, no database. Every input is a
parameter. ``tests/test_affordability_horizon.py`` enforces this with an
import allowlist over this module's own ``import`` statements (not trusting
the docstring) and a determinism test, matching how ``objectives.py``
enforces its own purity boundary.
"""
from __future__ import annotations

import math


def hours_to_afford(price: int | None, balance: int | None,
                     rate_per_hour: float | None) -> float | None:
    """Hours until `balance` reaches `price` at `rate_per_hour`.

    Returns ``None`` when the answer is unknowable: `price` or `balance`
    was never read, `price` is negative (unreadable/invalid), or the price
    is not yet affordable and there is no usable rate to project from
    (`rate_per_hour` is `None`, negative, or non-finite).

    Returns ``0.0`` when `balance` already covers `price` - knowable
    without any rate at all, because the coins are already there.

    Returns ``math.inf`` when the price is not yet affordable and the
    measured rate is exactly `0.0`: a real measurement that this tier earns
    nothing, so no amount of waiting closes the gap.

    Otherwise returns the shortfall divided by the rate: the number of
    hours of the measured rate needed to close the gap between `balance`
    and `price`.
    """
    if price is None or balance is None:
        return None
    if price < 0:
        return None

    shortfall = price - balance
    if shortfall <= 0:
        # Already affordable. Knowable without a rate - the coins are there.
        return 0.

    if rate_per_hour is None:
        # Unmeasured: no income baseline exists yet. Unknown, not infinite.
        return None
    if not math.isfinite(rate_per_hour) or rate_per_hour < 0:
        # Outside the contract progression.rates ever produces. Treated as
        # unknown rather than guessed at.
        return None
    if rate_per_hour == 0.:
        # Measured, and this tier earns nothing. Explicit, not a
        # ZeroDivisionError laundered into infinity.
        return math.inf

    return shortfall / rate_per_hour
