"""What each in-battle menu icon's badge looked like when last handled.

Settings' dot keeps the hamburger red forever and the Event star's count
survives a visit, so "badged" alone cannot mean "go". An icon is due when its
badge is new or changed, or its cooldown has run out.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import config
from battle_menu import Badge, Icon, IconReading

logger = logging.getLogger(__name__)


class BattleMenuState:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._icons: dict[str, dict] = {}
        self._last_menu: dict[str, str | None] | None = None
        self._last_session = float("-inf")
        self._load()

    # -- queries ---------------------------------------------------------------
    def due(self, readings: dict[Icon, IconReading], now: float) -> list[Icon]:
        return [icon for icon, reading in readings.items()
                if reading.badge is not None and self._due(icon, reading.badge.color, now)]

    def session_allowed(self, now: float) -> bool:
        return now - self._last_session >= config.BATTLE_MENU_MIN_SESSION_GAP

    def worth_opening(self, now: float) -> bool:
        if self._last_menu is None:
            return True
        return any(color is not None and self._due(icon, color, now)
                   for icon, color in self._last_menu.items())

    # -- updates ---------------------------------------------------------------
    def session_started(self, now: float) -> None:
        self._last_session = now
        self._save()

    def remember(self, readings: dict[Icon, IconReading]) -> None:
        self._last_menu = {i: (r.badge.color if r.badge else None) for i, r in readings.items()}
        self._save()

    def handled(self, icon: Icon, badge: Badge | None, now: float) -> None:
        self._icons[icon] = {"badge": badge.color if badge else None,
                             "until": now + config.BATTLE_MENU_COOLDOWNS[icon], "failures": 0}
        self._save()

    def failed(self, icon: Icon, badge: Badge | None, now: float) -> None:
        failures = self._icons.get(icon, {}).get("failures", 0) + 1
        wait = min(config.BATTLE_MENU_COOLDOWNS[icon] * 2 ** failures,
                   config.BATTLE_MENU_MAX_BACKOFF)
        self._icons[icon] = {"badge": badge.color if badge else None,
                             "until": now + wait, "failures": failures}
        self._save()

    # -- internals -------------------------------------------------------------
    def _due(self, icon: str, color: str, now: float) -> bool:
        entry = self._icons.get(icon)
        return entry is None or entry["badge"] != color or now >= entry["until"]

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            self._icons = dict(data.get("icons", {}))
            self._last_menu = data.get("last_menu")
            last = data.get("last_session")
            self._last_session = float("-inf") if last is None else float(last)
        except (ValueError, TypeError, AttributeError, OSError):
            logger.warning("battle menu state unreadable; starting fresh: %s", self._path)
            self._icons, self._last_menu, self._last_session = {}, None, float("-inf")

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"icons": self._icons, "last_menu": self._last_menu,
                                       "last_session": (None if self._last_session == float("-inf")
                                                        else self._last_session)}))
            tmp.replace(self._path)
        except OSError:
            logger.warning("battle menu state not persisted; keeping in-memory state: %s", self._path)
