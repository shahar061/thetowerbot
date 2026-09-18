"""Account-bound age and coin-rate readings from the game's Stats screen."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from fleet.reroll_lifetime import read_lifetime


_MONTHS = {name: index for index, name in enumerate((
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
), start=1)}


def game_started_date(raw: str) -> str | None:
    """Accept only an unambiguous English date from the game, at day precision."""
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", raw.strip())
    if match is None or match[1] not in _MONTHS:
        return None
    try:
        return date(int(match[3]), _MONTHS[match[1]], int(match[2])).isoformat()
    except ValueError:
        return None


def account_metrics(root: Path, account_id: str, *, today: date | None = None) -> dict[str, Any]:
    """Read saved Stats evidence for a verified account without guessing gaps."""
    record = read_lifetime(root, account_id)
    result: dict[str, Any] = {
        "game_started": None, "account_age_days": None,
        "recent_cps": None, "lifetime_coins": None,
        "lifetime_coins_incomplete": False,
    }
    if record is None:
        return result
    result["lifetime_coins"] = record["lifetime_coins"]
    result["lifetime_coins_incomplete"] = record["coins_incomplete"]
    started = record.get("game_started")
    if isinstance(started, str):
        try:
            started_date = date.fromisoformat(started)
            elapsed = ((today or date.today()) - started_date).days
            if elapsed >= 0:
                result["game_started"] = started
                result["account_age_days"] = elapsed
        except ValueError:
            pass
    hourly = record.get("recent_coins_per_hour")
    if isinstance(hourly, (int, float)) and not isinstance(hourly, bool) and 0 <= hourly < float("inf"):
        result["recent_cps"] = hourly / 3600
    return result
