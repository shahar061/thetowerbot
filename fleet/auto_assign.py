"""Hand every pool account the opening, then a turtle once its best wave is high enough.

Only accounts with no live assignment or still on the opening are touched, so an
operator's manual choice always wins.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_FILE = "auto-assign.json"
_KEYS = {"enabled", "opening", "threshold_wave", "rotation"}


@dataclass(frozen=True)
class AutoAssignSettings:
    opening: str
    threshold_wave: int
    rotation: tuple[str, ...]


@dataclass(frozen=True)
class WorkerView:
    name: str
    account_id: str
    assigned: tuple[str, str] | None
    facts: tuple[str, int] | None
    # A live per-account override is the operator's manual tuning; assigning
    # would delete it, so it counts as a manual choice.
    overridden: bool = False


@dataclass(frozen=True)
class Pick:
    worker: str
    account_id: str
    strategy_id: str


def load_settings(root: Path, known_ids: set[str]) -> AutoAssignSettings | None:
    path = root / SETTINGS_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{SETTINGS_FILE} unreadable: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise ValueError(f"{SETTINGS_FILE} needs exactly {sorted(_KEYS)}")
    if type(raw["enabled"]) is not bool:
        raise ValueError("enabled must be true or false")
    if not raw["enabled"]:
        return None
    opening, threshold, rotation = raw["opening"], raw["threshold_wave"], raw["rotation"]
    if type(threshold) is not int or threshold < 1:
        raise ValueError("threshold_wave must be a positive integer")
    if (not isinstance(rotation, list) or not rotation
            or not all(isinstance(sid, str) for sid in rotation)
            or len(set(rotation)) != len(rotation)):
        raise ValueError("rotation must be a non-empty list of distinct strategy ids")
    if opening in rotation:
        raise ValueError("the opening cannot also be in the rotation")
    unknown = [sid for sid in [opening, *rotation] if sid not in known_ids]
    if unknown:
        raise ValueError(f"unknown strategy ids: {unknown}")
    return AutoAssignSettings(opening, threshold, tuple(rotation))


def plan(settings: AutoAssignSettings, workers: list[WorkerView]) -> list[Pick]:
    def live(worker: WorkerView) -> str | None:
        if worker.assigned is None or worker.assigned[0] != worker.account_id:
            return None
        return worker.assigned[1]

    counts = {sid: 0 for sid in settings.rotation}
    for worker in workers:
        if live(worker) in counts:
            counts[live(worker)] += 1
    picks: list[Pick] = []
    for worker in workers:
        current = live(worker)
        if worker.overridden:
            continue
        if current is None:
            picks.append(Pick(worker.name, worker.account_id, settings.opening))
        elif (current == settings.opening and worker.facts is not None
              and worker.facts[0] == worker.account_id
              and worker.facts[1] >= settings.threshold_wave):
            # min() keeps the first of equal counts, so ties follow rotation order.
            choice = min(settings.rotation, key=counts.__getitem__)
            counts[choice] += 1
            picks.append(Pick(worker.name, worker.account_id, choice))
    return picks


class AutoAssigner:
    """Applies plan() every interval through the operator's assignment path."""

    def __init__(self, root: Path, service: Any, *, interval: float = 60.0) -> None:
        self.root = Path(root)
        self.service = service
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_skip: str | None = None

    def check_once(self) -> list[Pick]:
        from fleet.build_route_store import RouteConflict

        library = self.service.strategy_library().read()
        latest = {row["id"]: row["version"] for row in library["strategies"]}
        latest.update({row["id"]: 1 for row in library["templates"]})
        try:
            settings = load_settings(self.root, set(latest))
        except ValueError as exc:
            if str(exc) != self._last_error:
                logger.error("auto-assign disabled: %s", exc)
                self._last_error = str(exc)
            return []
        self._last_error = None
        if settings is None:
            return []
        route = self.service.build_route_store().read()
        workers = [view for name in sorted(self.service.visible_workers())
                   if (view := self._view(name, route)) is not None]
        groups: dict[str, list[Pick]] = {}
        for pick in plan(settings, workers):
            groups.setdefault(pick.strategy_id, []).append(pick)
        applied: list[Pick] = []
        skipped: list[str] = []
        revision = route.revision
        for strategy_id, picks in groups.items():
            # One publish per worker, so a worker whose binding cannot be
            # verified is skipped without holding back the rest.
            for pick in picks:
                try:
                    published = self.service.assign_strategy(
                        expected_revision=revision, strategy_id=strategy_id,
                        strategy_version=latest[strategy_id],
                        workers=[{"worker": pick.worker, "account_id": pick.account_id}],
                        actor="auto")
                except RouteConflict:
                    logger.info("auto-assign: route changed; retrying next check")
                    return applied
                except ValueError as exc:
                    skipped.append(f"{pick.worker}: {exc}")
                    continue
                revision = published.revision
                applied.append(pick)
        summary = "; ".join(skipped) or None
        if summary is not None and summary != self._last_skip:
            logger.warning("auto-assign skipped %s", summary)
        self._last_skip = summary
        return applied

    def _view(self, name: str, route: Any) -> WorkerView | None:
        from web.account_catalog import registered_worker

        worker = self.root / "workers" / name
        registration = registered_worker(worker)
        if registration is None or not registration.account_id:
            return None
        assignment = route.assignments.get(name)
        assigned = None if assignment is None else (assignment.account_id, assignment.strategy_id)
        override = route.overrides.get(name)
        overridden = override is not None and override.account_id == registration.account_id
        facts = None
        try:
            raw = json.loads((worker / "build-route-facts.json").read_text(encoding="utf-8"))
            if isinstance(raw.get("account_id"), str) and type(raw.get("best_tier_1_wave")) is int:
                facts = (raw["account_id"], raw["best_tier_1_wave"])
        except (OSError, ValueError, AttributeError):
            pass
        return WorkerView(name, registration.account_id, assigned, facts, overridden)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fleet-auto-assign", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("auto-assign check failed")
            self._stop.wait(self.interval)
