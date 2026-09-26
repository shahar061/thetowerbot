"""One fleet-wide menu scan pace, applied to every reroll worker's Strategy.

The fleet owns this one field; every other Strategy field stays each
worker's own. Written to the coordinator root, applied to a worker's active
profile at launch, and pushed live to running workers when it changes.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable
from urllib.request import Request, urlopen

import config
from strategy import MAX_INTERVAL, MIN_INTERVAL, ControlError, StrategyStore, _in_range

logger = logging.getLogger(__name__)

FILE_NAME = "reroll-timing.json"
PUSH_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class FleetTiming:
    menu_interval: float = config.SCAN_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        # bool subclasses int, and True would otherwise read as one second.
        if isinstance(self.menu_interval, bool) or not isinstance(
                self.menu_interval, (int, float)):
            raise ControlError("menu_interval", "menu_interval must be a number")
        _in_range("menu_interval", self.menu_interval, MIN_INTERVAL, MAX_INTERVAL)

    def to_dict(self) -> dict[str, float]:
        return {"menu_interval": self.menu_interval}


def load(root: Path) -> FleetTiming | None:
    """The saved fleet pace, or None when the fleet has not chosen one."""
    path = root / FILE_NAME
    if not path.exists():
        return None
    try:
        return FleetTiming(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, ControlError) as exc:
        # Launching with each worker's own pace beats refusing to launch.
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return None


def save(root: Path, timing: FleetTiming) -> None:
    path = root / FILE_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(timing.to_dict()), encoding="utf-8")
    os.replace(tmp, path)


def apply(store: StrategyStore, timing: FleetTiming) -> None:
    """Write the fleet pace into the worker's active profile, nothing else."""
    active = store.ensure_seeded()
    if active.menu_interval != timing.menu_interval:
        store.save(replace(active, menu_interval=timing.menu_interval))


def _patch(port: int, body: dict[str, float]) -> None:
    request = Request(f"http://127.0.0.1:{port}/api/control", method="PATCH",
                      data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=PUSH_TIMEOUT_SECONDS):
        pass


def push(ports: Iterable[int], timing: FleetTiming,
         patch: Callable[[int, dict[str, float]], None] | None = None) -> list[int]:
    """PATCH each worker dashboard live; return the ports that did not answer.

    A stopped worker is not an error - it takes the pace at its next launch.
    The worker's PATCH also persists into its active profile.
    """
    send = patch or _patch
    unreachable: list[int] = []
    for port in ports:
        try:
            send(port, timing.to_dict())
        except OSError:
            unreachable.append(port)
    return unreachable
