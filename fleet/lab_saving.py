"""Just-in-time lab saving: how many coins Workshop must leave for upcoming labs. Pure."""

from __future__ import annotations

import math
from typing import Any


def income_rate(facts: Any, rules: Any) -> float | None:
    """Coins per hour after the safety margin; None when unread or not positive."""
    rate = getattr(facts, "coins_per_hour", None)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
        return None
    return rate * rules.labs.saving.income_margin_pct / 100
