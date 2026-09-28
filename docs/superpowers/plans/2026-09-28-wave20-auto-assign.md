# Wave-20 Auto-Assignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** New and rerolled pool accounts run the opening strategy until their best tier-1 wave reaches 20, then get one of the three turtle variants in round-robin, with no operator action.

**Architecture:** A pure `plan()` function decides assignments from settings, the route and per-worker facts. An `AutoAssigner` thread, owned by the fleet API next to the existing worker monitor, runs `plan()` every 60s and writes through the existing `FleetSetupService.assign_strategy` with `actor="auto"`.

**Tech Stack:** Python 3, stdlib threading/json, pytest. Run tests with `.venv/bin/pytest`.

**Spec:** `docs/superpowers/specs/2026-09-28-wave20-auto-assign-design.md`

**Workspace:** The main checkout has concurrent editors. Execute in a dedicated git worktree on branch `feat/wave20-auto-assign` (created with `superpowers:using-git-worktrees`, branch pushed with its own upstream, never tracking `origin/main`). Ask the user before every commit.

## Global Constraints

- Settings file: `<fleet root>/auto-assign.json`; keys exactly `enabled`, `opening`, `threshold_wave`, `rotation`. Missing file = disabled.
- Invalid settings (unknown id, empty rotation, duplicate ids in rotation, opening inside rotation, threshold < 1, wrong types) disable the rule and log one error per distinct error; never raise out of the loop.
- Each assignment pins the strategy's latest version at assignment time; templates are version 1.
- Manual assignments (anything other than the opening) are never changed.
- Publishes made by the rule use actor `"auto"`; operator API publishes keep `"operator"`.
- Only the test files named in this plan are run locally; never the whole suite.

## Review Focus

- A worker whose `build-route-facts.json` still carries the *previous* account after a reroll must not graduate the new account → covered by `test_facts_for_another_account_do_not_graduate`.
- `best_tier_1_wave` is `null` in facts for a fresh account → treated as no facts, worker stays on the opening → `test_null_wave_does_not_graduate`.
- Two workers crossing wave 20 in the same cycle must land on different turtles → `test_two_graduates_in_one_cycle_get_different_turtles`.
- A route edit by the operator between read and write must not be overwritten → `test_conflict_defers_the_cycle`.
- A worker hidden from the pool (or no longer registered) must be ignored even if it has an opening assignment → `test_hidden_and_unregistered_workers_are_ignored`.

---

### Task 1: `assign_strategy` records who assigned

**Files:**
- Modify: `fleet/setup.py:321-368` (`FleetSetupService.assign_strategy`)
- Test: `tests/test_build_route_api.py` (append one test)

**Interfaces:**
- Produces: `FleetSetupService.visible_workers() -> set[str]` (visible active pool members, same set `assign_strategy` validates against), and `FleetSetupService.assign_strategy(*, expected_revision: int, strategy_id: str, strategy_version: int, workers: list[dict[str, str]], actor: str = "operator") -> RouteDocument`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_build_route_api.py`; `_registered_worker` already exists there)

```python
def test_assignment_actor_is_recorded(tmp_path: Path) -> None:
    _registered_worker(tmp_path, "ACCOUNT-A", "ACCOUNT-A")
    fleet = FleetSetupService(tmp_path, qualification_root=tmp_path / "qualifications")
    fleet._reroll_pool = SimpleNamespace(members=lambda: [{"name": "Air_38"}])
    fleet._reroll_runs = SimpleNamespace(hidden_names=lambda: set())
    assert fleet.visible_workers() == {"Air_38"}
    fleet.assign_strategy(expected_revision=0, strategy_id="opening", strategy_version=1,
                          workers=[{"worker": "Air_38", "account_id": "ACCOUNT-A"}], actor="auto")
    audit = json.loads((tmp_path / "build-route-history" / "1.json").read_text())["audit"]
    assert audit["actor"] == "auto"
```

- [ ] **Step 2: Run it and see it fail**

Run: `.venv/bin/pytest tests/test_build_route_api.py::test_assignment_actor_is_recorded -q`
Expected: FAIL with `AttributeError: ... 'visible_workers'`.

- [ ] **Step 3: Implement**

In `fleet/setup.py`, add above `assign_strategy`:

```python
    def visible_workers(self) -> set[str]:
        """Active pool members the operator can see; only these take assignments."""
        return ({member["name"] for member in self._manual_pool().members()}
                - self._runs().hidden_names())
```

Change the `assign_strategy` signature and body:

```python
    def assign_strategy(self, *, expected_revision: int, strategy_id: str,
                        strategy_version: int, workers: list[dict[str, str]],
                        actor: str = "operator") -> Any:
```

replace the `visible = (...)` expression with `visible = self.visible_workers()`, and change the publish call to

```python
        published = store.publish(replace(current, assignments=assignments, overrides=overrides),
                                  expected_revision, actor)
```

The web endpoint is unchanged (`StrategyAssignRequest` has no `actor`, so API calls keep `"operator"`).

- [ ] **Step 4: Run the new test and the existing assignment tests**

Run: `.venv/bin/pytest tests/test_build_route_api.py -q -k "assign"`
Expected: all PASS.

- [ ] **Step 5: Commit (ask the user first)**

```bash
git add fleet/setup.py tests/test_build_route_api.py
git commit -m "Record who published a strategy assignment"
```

---

### Task 2: Settings and the pure assignment rule

**Files:**
- Create: `fleet/auto_assign.py`
- Test: `tests/test_auto_assign.py`

**Interfaces:**
- Produces:
  - `SETTINGS_FILE = "auto-assign.json"`
  - `AutoAssignSettings(opening: str, threshold_wave: int, rotation: tuple[str, ...])` (frozen dataclass)
  - `load_settings(root: Path, known_ids: set[str]) -> AutoAssignSettings | None` — `None` when the file is missing or `enabled` is false; raises `ValueError` with a readable message when invalid.
  - `WorkerView(name: str, account_id: str, assigned: tuple[str, str] | None, facts: tuple[str, int] | None)` — `assigned` is `(assignment.account_id, assignment.strategy_id)`, `facts` is `(facts.account_id, best_tier_1_wave)`.
  - `Pick(worker: str, account_id: str, strategy_id: str)`
  - `plan(settings: AutoAssignSettings, workers: list[WorkerView]) -> list[Pick]`

- [ ] **Step 1: Write the failing tests** — `tests/test_auto_assign.py`

```python
"""Opening until wave 20, then the least-used turtle; manual choices stay."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.auto_assign import (AutoAssignSettings, Pick, WorkerView, load_settings, plan)

SETTINGS = AutoAssignSettings("opening", 20, ("turtle", "guide", "eco"))


def _worker(name: str, *, strategy: str | None = None, assigned_to: str = "A",
            wave: int | None = None, facts_for: str = "A", account: str = "A") -> WorkerView:
    return WorkerView(name, account,
                      None if strategy is None else (assigned_to, strategy),
                      None if wave is None else (facts_for, wave))


def test_new_account_gets_the_opening() -> None:
    assert plan(SETTINGS, [_worker("w1")]) == [Pick("w1", "A", "opening")]


def test_rerolled_account_gets_the_opening() -> None:
    worker = _worker("w1", strategy="eco", assigned_to="OLD")
    assert plan(SETTINGS, [worker]) == [Pick("w1", "A", "opening")]


def test_opening_below_threshold_stays() -> None:
    assert plan(SETTINGS, [_worker("w1", strategy="opening", wave=19)]) == []


def test_opening_at_threshold_gets_least_used_turtle() -> None:
    workers = [_worker("t1", strategy="turtle", account="B", assigned_to="B"),
               _worker("w1", strategy="opening", wave=20)]
    assert plan(SETTINGS, workers) == [Pick("w1", "A", "guide")]


def test_tie_goes_to_rotation_order() -> None:
    assert plan(SETTINGS, [_worker("w1", strategy="opening", wave=25)]) == [
        Pick("w1", "A", "turtle")]


def test_two_graduates_in_one_cycle_get_different_turtles() -> None:
    workers = [_worker("w1", strategy="opening", wave=20),
               _worker("w2", strategy="opening", wave=30, account="B", assigned_to="B",
                       facts_for="B")]
    assert [pick.strategy_id for pick in plan(SETTINGS, workers)] == ["turtle", "guide"]


def test_manual_and_turtle_assignments_are_untouched() -> None:
    workers = [_worker("w1", strategy="eco", wave=90),
               _worker("w2", strategy="some-manual-choice", wave=5)]
    assert plan(SETTINGS, workers) == []


def test_facts_for_another_account_do_not_graduate() -> None:
    worker = _worker("w1", strategy="opening", wave=40, facts_for="OLD")
    assert plan(SETTINGS, [worker]) == []


def test_missing_facts_do_not_graduate() -> None:
    assert plan(SETTINGS, [_worker("w1", strategy="opening")]) == []


def test_stale_assignments_do_not_count_toward_rotation() -> None:
    workers = [_worker("old", strategy="turtle", assigned_to="GONE", account="C"),
               _worker("w1", strategy="opening", wave=20)]
    picks = plan(SETTINGS, workers)
    assert Pick("w1", "A", "turtle") in picks


def _write(root: Path, value: object) -> None:
    (root / "auto-assign.json").write_text(json.dumps(value))


KNOWN = {"opening", "turtle", "guide", "eco"}
VALID = {"enabled": True, "opening": "opening", "threshold_wave": 20,
         "rotation": ["turtle", "guide", "eco"]}


def test_missing_or_disabled_settings_mean_off(tmp_path: Path) -> None:
    assert load_settings(tmp_path, KNOWN) is None
    _write(tmp_path, {**VALID, "enabled": False})
    assert load_settings(tmp_path, KNOWN) is None


def test_valid_settings_load(tmp_path: Path) -> None:
    _write(tmp_path, VALID)
    assert load_settings(tmp_path, KNOWN) == SETTINGS


@pytest.mark.parametrize("change", [
    {"rotation": []},
    {"rotation": ["turtle", "turtle"]},
    {"rotation": ["opening", "turtle"]},
    {"rotation": ["turtle", "unknown"]},
    {"opening": "unknown"},
    {"threshold_wave": 0},
    {"threshold_wave": True},
    {"enabled": "yes"},
    {"extra": 1},
])
def test_invalid_settings_raise(tmp_path: Path, change: dict[str, object]) -> None:
    _write(tmp_path, {**VALID, **change})
    with pytest.raises(ValueError):
        load_settings(tmp_path, KNOWN)


def test_unreadable_settings_raise(tmp_path: Path) -> None:
    (tmp_path / "auto-assign.json").write_text("{not json")
    with pytest.raises(ValueError):
        load_settings(tmp_path, KNOWN)
```

- [ ] **Step 2: Run and see them fail**

Run: `.venv/bin/pytest tests/test_auto_assign.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fleet.auto_assign'`.

- [ ] **Step 3: Implement** — `fleet/auto_assign.py`

```python
"""Hand every pool account the opening, then a turtle once its best wave is high enough.

Only accounts with no live assignment or still on the opening are touched, so an
operator's manual choice always wins.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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
```

- [ ] **Step 4: Run and see them pass**

Run: `.venv/bin/pytest tests/test_auto_assign.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit (ask the user first)**

```bash
git add fleet/auto_assign.py tests/test_auto_assign.py
git commit -m "Decide opening and wave-20 turtle assignments"
```

---

### Task 3: The runner, wired into the fleet monitor

**Files:**
- Modify: `fleet/auto_assign.py` (append `AutoAssigner`)
- Modify: `fleet/setup.py:582-591` (`start_monitor` / `stop_monitor`)
- Test: `tests/test_auto_assign.py` (append)

**Interfaces:**
- Consumes: `FleetSetupService.visible_workers()`, `FleetSetupService.assign_strategy(..., actor=...)` (Task 1); `load_settings`, `plan`, `WorkerView`, `Pick` (Task 2); `StrategyLibrary.read() -> {"templates": [...], "strategies": [latest row per id]}`; `BuildRouteStore.read() -> RouteDocument`; `web.account_catalog.registered_worker(path) -> AccountChoice | None` (`.account_id`).
- Produces: `AutoAssigner(root: Path, service: FleetSetupService, *, interval: float = 60.0)` with `check_once() -> list[Pick]` (the picks it applied), `start()`, `stop()`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_auto_assign.py`)

```python
from types import SimpleNamespace
from uuid import uuid4

import db
from fleet.auto_assign import AutoAssigner
from fleet.build_route_store import RouteConflict
from fleet.setup import FleetSetupService


def _registered(root: Path, name: str, account_id: str) -> None:
    worker = root / "workers" / name
    checkpoints = worker / "checkpoints"
    checkpoints.mkdir(parents=True)
    binding = checkpoints / f"{uuid4().hex}.json"
    binding.write_text(json.dumps({"worker_id": name, "account_id": account_id,
                                   "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    (worker / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": name, "account_id": account_id,
        "web_port": 8081, "binding": str(binding), "endpoint": "127.0.0.1:5555",
        "lease_id": "lease"}))
    db.bind_account(worker / "tower_bot.db", account_id)


def _facts(root: Path, name: str, account_id: str, wave: int | None) -> None:
    (root / "workers" / name / "build-route-facts.json").write_text(json.dumps(
        {"account_id": account_id, "best_tier_1_wave": wave}))


def _fleet(root: Path, names: tuple[str, ...], hidden: set[str] = frozenset()) -> FleetSetupService:
    fleet = FleetSetupService(root, qualification_root=root / "qualifications")
    fleet._reroll_pool = SimpleNamespace(members=lambda: [{"name": n} for n in names])
    fleet._reroll_runs = SimpleNamespace(hidden_names=lambda: set(hidden))
    _write(root, {"enabled": True, "opening": "opening", "threshold_wave": 20,
                  "rotation": ["turtle", "labs_gems"]})
    return fleet


def _assigned(fleet: FleetSetupService) -> dict[str, str]:
    route = fleet.build_route_store().read()
    return {name: a.strategy_id for name, a in route.assignments.items()}


def test_runner_opens_then_graduates(tmp_path: Path) -> None:
    _registered(tmp_path, "w1", "ACCOUNT-A")
    fleet = _fleet(tmp_path, ("w1",))
    runner = AutoAssigner(tmp_path, fleet)
    runner.check_once()
    assert _assigned(fleet) == {"w1": "opening"}
    _facts(tmp_path, "w1", "ACCOUNT-A", 19)
    assert runner.check_once() == []
    _facts(tmp_path, "w1", "ACCOUNT-A", 20)
    runner.check_once()
    assert _assigned(fleet) == {"w1": "turtle"}
    history = sorted((tmp_path / "build-route-history").glob("*.json"))
    assert json.loads(history[-1].read_text())["audit"]["actor"] == "auto"


def test_null_wave_does_not_graduate(tmp_path: Path) -> None:
    _registered(tmp_path, "w1", "ACCOUNT-A")
    fleet = _fleet(tmp_path, ("w1",))
    runner = AutoAssigner(tmp_path, fleet)
    runner.check_once()
    _facts(tmp_path, "w1", "ACCOUNT-A", None)
    assert runner.check_once() == []
    assert _assigned(fleet) == {"w1": "opening"}


def test_hidden_and_unregistered_workers_are_ignored(tmp_path: Path) -> None:
    _registered(tmp_path, "shown", "ACCOUNT-A")
    _registered(tmp_path, "hidden", "ACCOUNT-B")
    fleet = _fleet(tmp_path, ("shown", "hidden", "ghost"), hidden={"hidden"})
    AutoAssigner(tmp_path, fleet).check_once()
    assert _assigned(fleet) == {"shown": "opening"}


def test_conflict_defers_the_cycle(tmp_path: Path) -> None:
    _registered(tmp_path, "w1", "ACCOUNT-A")
    fleet = _fleet(tmp_path, ("w1",))
    real = fleet.assign_strategy

    def conflicting(**kwargs: object) -> object:
        raise RouteConflict(fleet.build_route_store().read())

    fleet.assign_strategy = conflicting
    assert AutoAssigner(tmp_path, fleet).check_once() == []
    fleet.assign_strategy = real
    assert _assigned(fleet) == {}


def test_invalid_settings_disable_the_runner(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _registered(tmp_path, "w1", "ACCOUNT-A")
    fleet = _fleet(tmp_path, ("w1",))
    _write(tmp_path, {**VALID, "rotation": []})
    runner = AutoAssigner(tmp_path, fleet)
    assert runner.check_once() == []
    assert runner.check_once() == []
    assert _assigned(fleet) == {}
    assert sum("auto-assign disabled" in r.message for r in caplog.records) == 1
```

- [ ] **Step 2: Run and see them fail**

Run: `.venv/bin/pytest tests/test_auto_assign.py -q`
Expected: the new tests FAIL with `ImportError: cannot import name 'AutoAssigner'`.

- [ ] **Step 3: Implement** — append to `fleet/auto_assign.py`, and add `import logging`, `import threading` and `from typing import Any` to its imports plus `logger = logging.getLogger(__name__)` below them.

```python
class AutoAssigner:
    """Applies plan() every interval through the operator's assignment path."""

    def __init__(self, root: Path, service: Any, *, interval: float = 60.0) -> None:
        self.root = Path(root)
        self.service = service
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

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
        revision = route.revision
        for strategy_id, picks in groups.items():
            try:
                published = self.service.assign_strategy(
                    expected_revision=revision, strategy_id=strategy_id,
                    strategy_version=latest[strategy_id],
                    workers=[{"worker": p.worker, "account_id": p.account_id} for p in picks],
                    actor="auto")
            except (RouteConflict, ValueError) as exc:
                logger.info("auto-assign deferred to next check: %s", exc)
                break
            revision = published.revision
            applied.extend(picks)
        return applied

    def _view(self, name: str, route: Any) -> WorkerView | None:
        from web.account_catalog import registered_worker

        worker = self.root / "workers" / name
        registration = registered_worker(worker)
        if registration is None or not registration.account_id:
            return None
        assignment = route.assignments.get(name)
        assigned = None if assignment is None else (assignment.account_id, assignment.strategy_id)
        facts = None
        try:
            raw = json.loads((worker / "build-route-facts.json").read_text(encoding="utf-8"))
            if isinstance(raw.get("account_id"), str) and type(raw.get("best_tier_1_wave")) is int:
                facts = (raw["account_id"], raw["best_tier_1_wave"])
        except (OSError, ValueError, AttributeError):
            pass
        return WorkerView(name, registration.account_id, assigned, facts)

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
```

In `fleet/setup.py`, add `self._auto_assigner: Any | None = None` in `FleetSetupService.__init__` right after `self._worker_monitor: Any | None = None` (`fleet/setup.py:132`), and extend the monitor pair:

```python
    def start_monitor(self) -> None:
        """Own an independent monitor for the lifetime of the fleet API."""
        if self._worker_monitor is None:
            from fleet.worker_monitor import WorkerMonitor
            self._worker_monitor = WorkerMonitor(self.root, self._manual_supervisor())
        self._worker_monitor.start()
        if self._auto_assigner is None:
            from fleet.auto_assign import AutoAssigner
            self._auto_assigner = AutoAssigner(self.root, self)
        self._auto_assigner.start()

    def stop_monitor(self) -> None:
        if self._auto_assigner is not None:
            self._auto_assigner.stop()
        if self._worker_monitor is not None:
            self._worker_monitor.stop()
```

- [ ] **Step 4: Run the plan's test files**

Run: `.venv/bin/pytest tests/test_auto_assign.py tests/test_build_route_api.py tests/test_fleet_setup.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit (ask the user first)**

```bash
git add fleet/auto_assign.py fleet/setup.py tests/test_auto_assign.py
git commit -m "Auto-assign the opening and wave-20 turtles from the fleet monitor"
```

---

### Task 4: Rollout (operational, after the PR merges)

- [ ] **Step 1:** Write `~/.local/share/thetowerbot/fleet/auto-assign.json`:

```json
{"enabled": true, "opening": "opening", "threshold_wave": 20,
 "rotation": ["turtle",
              "strategy-3e323059b77b41e683fce3366c821578",
              "strategy-a168a84394d5445696810e057d45b3a1"]}
```

- [ ] **Step 2:** Ask the user to restart the fleet web process (`tower_bot.py --web`, port 8765) so `start_monitor` starts the runner.

- [ ] **Step 3:** Verify within ~2 minutes: `build-route.json` revision unchanged for Tiramisu64_56/57/58 (all past wave 20 on a turtle), and the coordinator log shows no `auto-assign disabled` line. The first rerolled account should show `Strategy assigned: <worker> → Opening v1` in the log.
