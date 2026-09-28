# Lab Slot Unlock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot unlocks lab slots 2–5 with gems by itself. A fleet-wide rollout record (dry run → canary → fleet, or halted) replaces the recorded-route `unlock_gate`. Slot 2 ships first; slots 3–5 use the same code.

**Architecture:**
- **Reader.** `lab_screen.read_next_locked` reads the first "Unlock Nth lab" tile as `LockedSlot(slot, price, point, tile)`. `LabHomeReading.next_locked` carries it.
- **Rollout record.** A new root module, `lab_unlock_rollout.py`, owns `<fleet root>/lab-unlock-rollout.json`. It uses the same `fcntl.flock` pattern as `fleet/build_route_store.py`. Every change is a read-modify-write under the lock. Promotions check the stage they expect while holding that lock.
- **Executor.** `LabVisit._unlock_slot` runs on the way out of a Labs visit:
  - it rehearses (dry run: no transaction, no tap);
  - or it prepares, acts and taps once (canary or fleet), then sorts the post-tap reads into one of three outcomes: bought, not landed, or uncertain;
  - or it skips with a reason.
- **Per-account slot state.** `lab-slots.json` holds each account's slot ownership. The gem lane's `_gem_met` and `next_unlock_slot` read it, and so does `RerollProgress.lab_visit_options()`, which names the slot a visit may unlock.
- **Visibility.**
  - `evaluate_resources` writes the Fleet State strings.
  - A "Lab slot rollout" panel on the Labs & Gems page lists the four slots and offers **Reset** on a halted slot.

**Tech Stack:**
- Python 3.12 (uv, pytest);
- sqlite (the transaction journal);
- FastAPI;
- Next.js 15 / React 19 with vitest.

**Spec:** `docs/superpowers/specs/2026-09-28-lab-slot-unlock-design.md`. Read it first. Every task argues from it.

## Global Constraints

- **One spend path.** A gem unlock is spent only through `LabVisit._prepare('lab_unlock', …)`, which runs `journal.prepare` and then `record_action`, and then exactly one tap. A rehearsal never prepares a transaction and never taps.
- **Unknown is never zero.** A missing catalog price, gem balance, price box, strip read or scope means no rehearsal and no tap. It never means "0" or "met".
- **Rollout file.**
  - Path: `<fleet root>/lab-unlock-rollout.json`. Lock: `<fleet root>/.lab-unlock-rollout.lock`. `schema_version` is 1.
  - Stages are exactly `dry_run`, `canary`, `fleet` and `halted`. A slot with no entry is `dry_run`.
  - A missing file means every slot is `dry_run`.
  - A corrupt file is renamed to `lab-unlock-rollout.json.corrupt-<ts>`, logged, and then treated as all `dry_run`. It is never silently overwritten.
- **Fleet root.** A worker root is `<fleet root>/workers/<worker id>`, so the fleet root is `worker_root.parent.parent` and the worker id is `worker_root.name`. Use the rollout only when `worker_root.parent.name == "workers"`.
- **`may_tap(slot, worker)`** is true only at stage `fleet`, or at stage `canary` when `worker == canary_worker`.
- **Promotion to canary.** Dry run → canary needs 2 clean rehearsals on the same worker and the same account, at least 600 s apart, at the same price. That price must equal `lab_catalog.lab_slot_gems(slot)`, which is 100 / 400 / 1400 / 3000 for slots 2–5.
- **Promotion to fleet and halting.**
  - Canary → fleet only after a proven bought unlock by `canary_worker`.
  - A second missed canary tap halts the slot.
  - A price that disagrees with the catalog halts the slot.
- **The route manifest stays for research.** `lab_routes.research_gate`, `catalog/lab-routes.v1.json` and the research-route validator stay unchanged. Only the unlock gate is removed.
- **Fleet State gem-step strings, verbatim:**
  - `Rehearsing slot N · k/2 dry runs`
  - `Canary: <worker> unlocks slot N next visit`
  - `Waiting for canary`
  - `Unlocking slot N`
  - `Save X more gems`
  - `Halted: <reason>`
  - `Slot N owned · waiting for lab starter`
  - `Slot N price unknown`
- **Events:**
  - `LabUnlockRehearsed(slot, price, gems)`
  - `LabUnlockPromoted(slot, stage)`
  - `LabUnlockHalted(slot, reason)`
  - `LabSlotUnlocked(slot, price, …)` with real values, published only through the journal's `recovery_event`.
- **Tests.**
  - Never run the whole suite, and never a whole directory.
  - Run only the test files named in each task: `uv run pytest -p no:allure_pytest -q <files>`, with a long Bash timeout (600000 ms).
  - Run UI tests as `cd web/ui && npx vitest run <file>`.
  - Before calling a failure pre-existing, reproduce it on `origin/main` in a separate worktree (`git worktree add /tmp/tb-main origin/main`), running the same file. `tests/test_m05_clone_qualification.py` has 19 known pre-existing failures on main.
- **Git.**
  - Work in a dedicated worktree. Never check out or restore files you did not create. Never use a bare `git stash`.
  - Every task ends with a commit. Commit messages end with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - `docs/` is gitignored, so use `git add -f` for files under it.
- **Branch drift.** `origin/main` moved after this branch was cut (it touches `tower_bot.py` and `web/app.py`). Rebase before Task 7 and before Task 9, and re-read the line ranges named there.

## Review Focus

1. **One unreadable frame right after the tap.** A transition animation or a flash must not halt the canary. Only three unclassified post-tap reads (or eight post-tap scans) do. Task 7 tests this: `test_one_transitional_frame_after_the_tap_does_not_halt`.
2. **The canary worker is reassigned to another account before it taps.** The slot goes back to `dry_run`. It must not stay pinned to a worker that now plays a different account. Task 2 tests this: `test_a_canary_bound_to_another_account_is_released`.
3. **A stale `lab-slots.json` still says slot 2 is locked, but the screen shows slot 2 owned and slot 3 locked.** The visit neither rehearses nor taps slot 3 under slot 2's name, and the visit result corrects the record. Task 7 tests this: `test_a_stale_slot_record_corrects_itself_without_a_tap`.
4. **The gem header cannot be read.** It may be missing, abbreviated or low-confidence. There is no rehearsal and no tap. Task 7 tests this: `test_an_unreadable_gem_balance_never_rehearses_or_taps`.
5. **Reset hits a slot that is no longer halted** (two clicks, or a stale page). The API answers 409, the record is unchanged, and the panel shows the refusal. Tasks 2 and 9 test this: `test_halt_keeps_the_first_reason_and_reset_returns_to_dry_run`, `test_reset_endpoint_refuses_a_slot_that_is_not_halted` and `a refused reset is shown, not swallowed`.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `lab_screen.py` | modify | `LockedSlot`, `read_next_locked`, `LabHomeReading.next_locked`; later drop the `slot2_*` fields |
| `lab_unlock_rollout.py` | create | Rollout record: model, lock, transitions, canary presence, Fleet State status line, dashboard rows |
| `events.py` | modify | `LabUnlockRehearsed`, `LabUnlockPromoted`, `LabUnlockHalted` |
| `web/ui/lib/types.ts`, `web/ui/lib/format.ts` | modify | Event feed types and lines for the three new events |
| `lab_plan.py` | modify | `lab-slots.json` (`slot_records`, `slot_owned`, `slot_due`, `note_slots`); `LabVisitOptions(unlock_slots, keep_gems)` |
| `lab_runtime.py` | modify | The legacy import reads every slot from `lab-slots.json` |
| `fleet/resource_blocks.py` | modify | `LabFacts.slot_ownership/owned_floor/rollout/worker`; `_gem_met` for every slot; `GemStep.slot`; `gem_lane_blocks`; `next_unlock_slot`; `gem_automated(block, rollout)` |
| `fleet/lab_facts.py` | modify | `persisted_lab_facts` passes the slot ownership |
| `fleet/reroll_progress.py` | modify | `note_lab_slots`, `next_unlock_slot`, `lab_visit_options`, `lab_due`, `fleet_root`, `worker_id`, `unlock_rollout`, and the rollout in the plans |
| `transactions.py` | modify | `refute_unlanded_unlock`, which settles a provably unlanded unlock tap as not charged |
| `lab_visit.py` | modify | `_unlock_slot` (rehearse, tap or skip), `_settle_own_unlock` (three outcomes), evidence frames, recovery for slot N, `LabVisitResult.slot_status/unlocked_slot` |
| `tower_bot.py` | modify | LabVisit wiring (rollout, worker, evidence dir); `_authorize_lab` follows the rollout; `_finish_lab_visit` |
| `lab_routes.py` | modify | Remove `unlock_gate`, `_validate_unlock`, `recorded_unlocks`, `load_recorded_unlocks`; unlock records enable nothing |
| `fleet/build_route_eval.py` | modify | `RouteFacts.lab_slot_status`; gem step from the rollout (`evaluate_resources(route, facts, rollout)`) |
| `fleet/build_route_preview_facts.py`, `fleet/setup.py`, `fleet/labs_view.py` | modify | Pass slot status and the rollout; `labs_snapshot` adds `unlock_rollout` |
| `web/app.py` | modify | `POST /api/fleet/labs/unlock-rollout/{slot}/reset` |
| `web/ui/lib/labs.ts`, `web/ui/lib/api.ts`, `web/ui/app/fleet/reroll/fleetOverview.ts` | modify | Rollout row type, reset call, validation |
| `web/ui/app/fleet/reroll/labs/UnlockRolloutPanel.tsx` | create | "Lab slot rollout" panel with Reset on halted slots |
| `web/ui/app/fleet/reroll/labs/page.tsx` | modify | Render the panel |
| `tests/test_lab_unlock_rollout.py`, `tests/test_lab_slot_unlock.py` | create | Rollout record; executor end to end with recorded frames |

---

### Task 1: Locked-slot reader

**Files:**
- Modify: `lab_screen.py`: the `LabHomeReading` dataclass (lines 22–33), `read_slots` (the inline `ordinal` dict at line 170), and `read_home` (lines 215–295)
- Test: `tests/test_lab_screen.py` (append)

**Interfaces:**
- **Consumes:** nothing new.
- **Produces:**
  - `lab_screen.LockedSlot(slot: int, price: int | None, point: tuple[int, int] | None, tile: tuple[int, int, int, int] | None = None)`. It is a frozen dataclass. `tile` is `(x, y, w, h)`.
  - `lab_screen.read_next_locked(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LockedSlot | None`.
  - `LabHomeReading.next_locked: LockedSlot | None = None`, as the last field. The `slot2_*` fields stay until Task 10.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lab_screen.py`:

```python
import pytest


def _owned_two_locked_three(price: str | None = "400") -> tuple[ocr.TextBox, ...]:
    """Lab 2 owned and idle, Lab 3 locked: label-parsing coverage until a live capture exists."""
    base = [box for box in recorded("menu_labs_slot1_idle") if box.text not in {"Unlock Znd lab", "100"}]
    extra = [ocr.TextBox("Lab Offline", .99, config.Rect(394, 832, 291, 52)),
             ocr.TextBox("Lab 3", .99, config.Rect(25, 1046, 97, 39)),
             ocr.TextBox("Unlock 3rd lab", .99, config.Rect(351, 1174, 378, 51))]
    if price is not None:
        extra.append(ocr.TextBox(price, .99, config.Rect(536, 1271, 101, 53)))
    return tuple(base + extra)


@pytest.mark.parametrize("name", ["menu_labs_slot1_idle", "menu_labs_slot1_affordable",
                                  "menu_labs_game_speed_running"])
def test_new_accounts_read_lab_two_as_the_next_locked_slot(name: str) -> None:
    from lab_screen import read_home, read_next_locked

    locked = read_next_locked(frame(name), recorded(name))
    assert locked is not None
    assert (locked.slot, locked.price, locked.point) == (2, 100, (586, 906))
    x, y, w, h = locked.tile
    assert x <= locked.point[0] < x + w and y <= locked.point[1] < y + h
    assert read_home(frame(name), recorded(name)).next_locked == locked


def test_five_owned_slots_have_no_locked_tile() -> None:
    from lab_screen import read_next_locked, read_slots

    assert read_next_locked(frame("menu_labs_active"), recorded("menu_labs_active")) is None
    strip = read_slots(frame("menu_labs_active"), recorded("menu_labs_active"), observed_at=1.)
    assert strip.strip_read() and strip.slots_owned == 5


def test_the_first_locked_tile_is_read_for_later_slots() -> None:
    from lab_screen import read_next_locked, read_slots

    image = frame("menu_labs_slot1_idle")
    locked = read_next_locked(image, _owned_two_locked_three())
    assert locked is not None
    assert (locked.slot, locked.price, locked.point) == (3, 400, (586, 1297))
    assert read_slots(image, _owned_two_locked_three(), observed_at=1.).slots_owned == 2


def test_a_locked_tile_without_one_readable_price_offers_no_point() -> None:
    from lab_screen import read_next_locked

    image = frame("menu_labs_slot1_idle")
    missing = read_next_locked(image, _owned_two_locked_three(price=None))
    assert missing is not None and (missing.slot, missing.price, missing.point) == (3, None, None)
    doubled = _owned_two_locked_three() + (ocr.TextBox("1400", .99, config.Rect(560, 1330, 120, 50)),)
    ambiguous = read_next_locked(image, doubled)
    assert ambiguous is not None and ambiguous.price is None and ambiguous.point is None


def test_a_label_under_the_wrong_header_or_without_the_page_title_reads_nothing() -> None:
    from lab_screen import read_next_locked

    image = frame("menu_labs_slot1_idle")
    wrong = tuple(ocr.TextBox("Unlock 3rd lab", box.confidence, box.rect) if box.text == "Unlock Znd lab"
                  else box for box in recorded("menu_labs_slot1_idle"))
    assert read_next_locked(image, wrong) is None
    untitled = tuple(box for box in recorded("menu_labs_slot1_idle") if box.text != "LAB")
    assert read_next_locked(image, untitled) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_screen.py`
Expected: FAIL with `ImportError: cannot import name 'read_next_locked'`.

- [ ] **Step 3: Implement**

In `lab_screen.py`, add these after `_MIN_CONFIDENCE`:

```python
# "Unlock Nth lab" labels as OCR reads them ("2nd" is sometimes "Znd").
_ORDINALS = {2: ("2ND", "ZND"), 3: ("3RD",), 4: ("4TH",), 5: ("5TH",)}
# One Labs card spans about 392 px of a 2400 px frame. It bounds the last
# visible tile when no header follows it.
_TILE_PITCH = .165
```

Add this dataclass above `LabHomeReading`:

```python
@dataclass(frozen=True)
class LockedSlot:
    """The first locked lab tile: its slot, gem price and the price's centre."""
    slot: int
    price: int | None
    point: tuple[int, int] | None
    tile: tuple[int, int, int, int] | None = None
```

Add this as the last field of `LabHomeReading`:

```python
    next_locked: LockedSlot | None = None
```

In `read_slots`, delete the local `ordinal = {…}` line. Change the `locks` comprehension to use `_ORDINALS.get(slot, ())`.

Add this function below `read_slots`:

```python
def read_next_locked(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LockedSlot | None:
    """The first "Unlock Nth lab" tile (N = 2..5), its gem price and the price's centre.

    Slots unlock in order, so only the first locked tile matters. None means no
    locked tile was read. With a complete strip read, that means every slot is owned.
    A tile whose price box is missing or ambiguous keeps its slot but offers no point.
    """
    height, width = screen.shape[:2]
    trusted = [box for box in boxes if _trusted(box)]
    titles = [box for box in trusted if box.text.strip().upper() == "LAB"
              and box.rect.x < width * .2 and box.rect.y < height * .1]
    if len(titles) != 1:
        return None
    headers: dict[int, list[ocr.TextBox]] = {}
    for box in trusted:
        match = re.fullmatch(r"Lab\s+([1-5])", box.text.strip(), re.I)
        if match and box.rect.x < width * .25 and box.rect.y > titles[0].rect.y:
            headers.setdefault(int(match[1]), []).append(box)
    tops = sorted(h.rect.y for group in headers.values() for h in group)
    for slot in sorted(set(headers) & set(_ORDINALS)):
        if len(headers[slot]) != 1:
            return None
        heading = headers[slot][0]
        bottom = min((top for top in tops if top > heading.rect.y),
                     default=min(height, heading.rect.y + round(height * _TILE_PITCH)))
        within = [box for box in trusted if heading.rect.y + heading.rect.h <= box.rect.y
                  and box.rect.y + box.rect.h <= bottom]
        labels = [box for box in within
                  if _normalized(box.text) in {f"UNLOCK{word}LAB" for word in _ORDINALS[slot]}]
        if not labels:
            continue
        if len(labels) != 1:
            return None
        label = labels[0]
        tile = (0, heading.rect.y, width, bottom - heading.rect.y)
        prices = [box for box in within if box.rect.y >= label.rect.y + label.rect.h
                  and width * .43 < box.rect.x < width * .65
                  and ocr.parse_number(box.text) is not None]
        if len(prices) != 1:
            return LockedSlot(slot, None, None, tile)
        price = prices[0]
        return LockedSlot(slot, ocr.parse_number(price.text),
                          (price.rect.x + price.rect.w // 2, price.rect.y + price.rect.h // 2), tile)
    return None
```

In `read_home`, add `next_locked = read_next_locked(screen, boxes)` after `gem_balance = _gem_balance(...)`. Pass `next_locked=next_locked` as a keyword to each of the four `LabHomeReading(True, …)` returns. The `LabHomeReading(False, …)` return stays unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_screen.py tests/test_lab_visit.py`
Expected: PASS. `tests/test_lab_visit.py` guards `read_home` against regressions.

- [ ] **Step 5: Commit**

```bash
git add lab_screen.py tests/test_lab_screen.py
git commit -m "Read the first locked lab tile as its slot, price and price point

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Rollout record

**Files:**
- Create: `lab_unlock_rollout.py`
- Test: `tests/test_lab_unlock_rollout.py` (create)

**Interfaces:**
- **Consumes:**
  - `lab_catalog.lab_slot_gems(slot) -> int | None`;
  - `fleet.build_route_store._write_json_atomic(path, value)`;
  - `web.account_catalog.registered_worker(worker_root) -> AccountChoice | None`.
- **Produces:**
  - Constants: `SCHEMA_VERSION = 1`, `SLOTS = (2, 3, 4, 5)`, `STAGES`, `PROMOTION_SPACING_SECONDS = 600.`, `OUTCOMES = ("bought", "not_charged")`.
  - `class RolloutError(ValueError)`.
  - `DryRun(worker: str, at: float, price: int, gems: int, account_id: str | None = None)`.
  - `SlotRollout`, with these fields and methods:
    - `stage="dry_run"`, `canary_worker=None`, `canary_account=None`, `dry_runs=()`, `unlock=None`, `halted_reason=None`, `evidence=()`;
    - `.rehearsals(worker) -> int`, which returns 0, 1 or 2;
    - `.to_dict()` and `SlotRollout.from_dict(raw)`.
  - `RolloutChange(slot, before, after)`, with `.promoted -> str | None` (`"canary"` or `"fleet"`) and `.halted -> bool`.
  - `LabUnlockRollout(fleet_root)`, with `.path`, `.lock_path` and these methods:
    - `.slots(*, quarantine=True) -> dict[int, SlotRollout]`, `.slot(slot)`, `.may_tap(slot, worker)`;
    - `.note_dry_run(slot, worker, price, gems, at, *, account_id=None) -> RolloutChange`;
    - `.note_unlock(slot, worker, transaction_key, outcome, *, at) -> RolloutChange`;
    - `.halt(slot, reason, evidence=()) -> RolloutChange`;
    - `.release_canary(slot, *, expected_worker=None) -> RolloutChange`;
    - `.release_absent_canary(slot) -> RolloutChange`;
    - `.reset(slot) -> RolloutChange`, which raises `RolloutError("slot_not_halted")`;
    - `.snapshot() -> list[dict]`.
  - `canary_present(fleet_root, worker, account_id) -> bool | None`.
  - `rollout_status(state: SlotRollout, slot: int, worker: str | None) -> str`.
- **Schema additions to the spec's record:**
  - `canary_account`, so a reassigned canary can be detected;
  - `account_id` on each dry run;
  - `unlock`, which holds the last canary attempt `{worker, transaction_key, outcome, at}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lab_unlock_rollout.py`:

```python
"""The fleet's lab-slot unlock rollout: dry run, canary, fleet, halted."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from lab_unlock_rollout import (DryRun, LabUnlockRollout, RolloutError, SlotRollout,
                                canary_present, rollout_status)
from tests.test_labs_view import _registered


def canary(rollout: LabUnlockRollout, worker: str = "Air_1", account: str = "account-a") -> None:
    rollout.note_dry_run(2, worker, 100, 150, 1000., account_id=account)
    rollout.note_dry_run(2, worker, 100, 150, 1600., account_id=account)


def _pool(root: Path, *names: str) -> None:
    (root / "reroll-pool.json").write_text(json.dumps(
        [{"name": name, "endpoint": "127.0.0.1:5555", "lease_id": "lease"} for name in names]))


def test_a_missing_file_leaves_every_slot_at_dry_run(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    assert rollout.slots() == {slot: SlotRollout() for slot in (2, 3, 4, 5)}
    assert not rollout.may_tap(2, "Air_1")
    assert not (tmp_path / "lab-unlock-rollout.json").exists()


def test_two_rehearsals_ten_minutes_apart_promote_that_worker_to_canary(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    assert rollout.note_dry_run(2, "Air_1", 100, 150, 1000., account_id="account-a").promoted is None
    assert rollout.slot(2).rehearsals("Air_1") == 1
    assert rollout.note_dry_run(2, "Air_1", 100, 151, 1599., account_id="account-a").promoted is None
    assert rollout.note_dry_run(2, "Air_2", 100, 300, 1700., account_id="account-b").promoted is None
    change = rollout.note_dry_run(2, "Air_1", 100, 152, 1600., account_id="account-a")
    assert change.promoted == "canary"
    state = LabUnlockRollout(tmp_path).slot(2)
    assert (state.stage, state.canary_worker, state.canary_account) == ("canary", "Air_1", "account-a")
    assert rollout.may_tap(2, "Air_1") and not rollout.may_tap(2, "Air_2")
    assert not rollout.may_tap(3, "Air_1")


def test_a_worker_moved_to_another_account_starts_its_rehearsals_again(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    rollout.note_dry_run(2, "Air_1", 100, 150, 1000., account_id="account-a")
    assert rollout.note_dry_run(2, "Air_1", 100, 150, 2000., account_id="account-b").promoted is None


def test_a_rehearsal_price_that_disagrees_with_the_catalog_halts_the_slot(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    change = rollout.note_dry_run(3, "Air_1", 450, 500, 1000., account_id="account-a")
    assert change.halted
    assert change.after.halted_reason == "Rehearsal read 450 gems for slot 3; the catalog says 400"
    assert rollout.note_dry_run(3, "Air_1", 400, 500, 2000., account_id="account-a").after.stage == "halted"


def test_a_proven_canary_unlock_promotes_the_slot_to_fleet(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    assert rollout.note_unlock(2, "Air_2", "k0", "bought", at=1.).after.stage == "canary"
    change = rollout.note_unlock(2, "Air_1", "k1", "bought", at=2000.)
    assert change.promoted == "fleet"
    assert change.after.unlock == {"worker": "Air_1", "transaction_key": "k1",
                                   "outcome": "bought", "at": 2000.}
    assert rollout.may_tap(2, "Air_7")
    later = rollout.note_unlock(2, "Air_7", "k2", "bought", at=3000.)
    assert later.after.unlock["transaction_key"] == "k1"  # the fleet stage records nothing new


def test_a_second_missed_canary_tap_halts_the_slot(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    once = rollout.note_unlock(2, "Air_1", "k1", "not_charged", at=2000.)
    assert once.after.stage == "canary" and not once.halted
    twice = rollout.note_unlock(2, "Air_1", "k2", "not_charged", at=3000.)
    assert twice.halted and twice.after.halted_reason == "The canary's unlock tap did not land twice"
    with pytest.raises(ValueError):
        rollout.note_unlock(2, "Air_1", "k3", "maybe", at=1.)


def test_halt_keeps_the_first_reason_and_reset_returns_to_dry_run(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    with pytest.raises(RolloutError, match="slot_not_halted"):
        rollout.reset(2)
    assert rollout.slot(2).stage == "canary"
    first = rollout.halt(2, "Post-tap screen was not understood", ("a.png", "b.png"))
    assert first.halted and first.after.evidence == ("a.png", "b.png")
    again = rollout.halt(2, "another reason", ("c.png",))
    assert not again.halted
    assert again.after.halted_reason == "Post-tap screen was not understood"
    assert again.after.evidence == ("a.png", "b.png", "c.png")
    assert not rollout.may_tap(2, "Air_1")
    assert rollout.reset(2).after == SlotRollout()


def test_release_canary_only_releases_the_expected_worker(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    assert rollout.release_canary(2, expected_worker="Air_2").after.stage == "canary"
    assert rollout.release_canary(2, expected_worker="Air_1").after == SlotRollout()


def test_a_canary_that_left_the_pool_is_released(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    _registered(tmp_path, "Air_1", "account-a")
    _pool(tmp_path, "Air_1")
    assert rollout.release_absent_canary(2).after.stage == "canary"
    _pool(tmp_path)
    assert rollout.release_absent_canary(2).after.stage == "dry_run"


def test_a_canary_bound_to_another_account_is_released(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    _registered(tmp_path, "Air_1", "account-b")
    _pool(tmp_path, "Air_1")
    assert canary_present(tmp_path, "Air_1", "account-a") is False
    assert rollout.release_absent_canary(2).after.stage == "dry_run"


def test_an_unreadable_pool_never_releases_a_canary(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    (tmp_path / "reroll-pool.json").write_text("{bad")
    assert canary_present(tmp_path, "Air_1", "account-a") is None
    assert rollout.release_absent_canary(2).after.stage == "canary"


def test_a_corrupt_file_is_moved_aside_and_every_slot_is_dry_run(tmp_path: Path) -> None:
    path = tmp_path / "lab-unlock-rollout.json"
    path.write_text('{"schema_version": 1, "slots": {"2": {"stage": "bogus"}}}')
    rollout = LabUnlockRollout(tmp_path)
    assert rollout.slots(quarantine=False)[2] == SlotRollout()
    assert path.exists()  # a dashboard read never moves it
    assert rollout.slot(2) == SlotRollout()
    assert not path.exists()
    (aside,) = tmp_path.glob("lab-unlock-rollout.json.corrupt-*")
    assert "bogus" in aside.read_text()
    rollout.note_dry_run(2, "Air_1", 100, 150, 1., account_id="a")
    assert json.loads(path.read_text())["slots"]["2"]["dry_runs"][0]["worker"] == "Air_1"


def test_concurrent_writers_never_lose_a_rehearsal(tmp_path: Path) -> None:
    workers = [f"Air_{index}" for index in range(8)]

    def rehearse(worker: str) -> None:
        LabUnlockRollout(tmp_path).note_dry_run(2, worker, 100, 150, 1000., account_id=worker)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(rehearse, workers))
    assert sorted(run.worker for run in LabUnlockRollout(tmp_path).slot(2).dry_runs) == sorted(workers)


def test_a_lost_promotion_race_sees_the_winner(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    with ThreadPoolExecutor(max_workers=2) as pool:
        changes = list(pool.map(lambda key: LabUnlockRollout(tmp_path).note_unlock(
            2, "Air_1", key, "bought", at=2000.), ["k1", "k2"]))
    assert sorted(change.promoted is not None for change in changes) == [False, True]
    assert rollout.slot(2).stage == "fleet"


def test_status_lines_for_fleet_state() -> None:
    assert rollout_status(SlotRollout(), 2, "Air_1") == "Rehearsing slot 2 · 0/2 dry runs"
    one = SlotRollout(dry_runs=(DryRun("Air_1", 1., 100, 150, "a"),))
    assert rollout_status(one, 2, "Air_1") == "Rehearsing slot 2 · 1/2 dry runs"
    held = SlotRollout(stage="canary", canary_worker="Air_1")
    assert rollout_status(held, 2, "Air_1") == "Canary: Air_1 unlocks slot 2 next visit"
    assert rollout_status(held, 2, "Air_2") == "Waiting for canary"
    assert rollout_status(SlotRollout(stage="fleet"), 3, "Air_2") == "Unlocking slot 3"
    assert rollout_status(SlotRollout(stage="halted", halted_reason="x"), 2, None) == "Halted: x"


def test_snapshot_rows_for_the_dashboard(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    rows = rollout.snapshot()
    assert [row["slot"] for row in rows] == [2, 3, 4, 5]
    assert rows[0] == {"slot": 2, "stage": "canary", "canary_worker": "Air_1", "dry_runs": 2,
                       "price": 100, "halted_reason": None, "evidence": []}
    assert [row["price"] for row in rows] == [100, 400, 1400, 3000]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_unlock_rollout.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'lab_unlock_rollout'`.

- [ ] **Step 3: Implement**

Create `lab_unlock_rollout.py`:

```python
"""Fleet-wide rollout of the bot's own lab-slot unlocks: dry run, canary, fleet.

One JSON file under the fleet root records, for each slot 2-5, how far the
unlock has been proven. Workers read it before a rehearsal or a tap. They
write it only under an exclusive file lock, and every promotion re-checks the
stage it expects while holding that lock, so a worker that loses a race simply
reads the newer record. A missing or corrupt file means every slot is back at
dry run, so nothing taps.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator, Mapping

import lab_catalog
from fleet.build_route_store import _write_json_atomic

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SLOTS = (2, 3, 4, 5)
STAGES = ("dry_run", "canary", "fleet", "halted")
PROMOTION_REHEARSALS = 2
PROMOTION_SPACING_SECONDS = 600.
MAX_DRY_RUNS = 20
MAX_EVIDENCE = 32
OUTCOMES = ("bought", "not_charged")


class RolloutError(ValueError):
    """An owner action that does not apply to the slot's current stage."""


@dataclass(frozen=True)
class DryRun:
    worker: str
    at: float
    price: int
    gems: int
    account_id: str | None = None


def _qualifies(runs: list[DryRun]) -> bool:
    """Two rehearsals, same price and account, at least ten minutes apart."""
    return any(first.price == second.price and first.account_id == second.account_id
               and abs(second.at - first.at) >= PROMOTION_SPACING_SECONDS
               for index, first in enumerate(runs) for second in runs[index + 1:])


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _whole(value: object, low: int) -> bool:
    return type(value) is int and value >= low


@dataclass(frozen=True)
class SlotRollout:
    stage: str = "dry_run"
    canary_worker: str | None = None
    canary_account: str | None = None
    dry_runs: tuple[DryRun, ...] = ()
    unlock: Mapping[str, Any] | None = None
    halted_reason: str | None = None
    evidence: tuple[str, ...] = ()

    def rehearsals(self, worker: str | None) -> int:
        """Clean rehearsals by `worker` counted toward promotion: 0, 1 or 2."""
        mine = [run for run in self.dry_runs if run.worker == worker]
        if not mine:
            return 0
        return PROMOTION_REHEARSALS if _qualifies(mine) else 1

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "canary_worker": self.canary_worker,
                "canary_account": self.canary_account,
                "dry_runs": [asdict(run) for run in self.dry_runs],
                "unlock": dict(self.unlock) if self.unlock is not None else None,
                "halted_reason": self.halted_reason, "evidence": list(self.evidence)}

    @classmethod
    def from_dict(cls, raw: object) -> SlotRollout:
        if not isinstance(raw, dict):
            raise ValueError("slot entry must be an object")
        stage = raw.get("stage", "dry_run")
        canary, account = raw.get("canary_worker"), raw.get("canary_account")
        if (stage not in STAGES or canary is not None and not isinstance(canary, str)
                or account is not None and not isinstance(account, str)
                or stage == "canary" and not canary):
            raise ValueError("bad stage or canary")
        runs = []
        for item in raw.get("dry_runs") or []:
            if (not isinstance(item, dict) or not isinstance(item.get("worker"), str)
                    or not _finite(item.get("at")) or not _whole(item.get("price"), 1)
                    or not _whole(item.get("gems"), 0)
                    or item.get("account_id") is not None and not isinstance(item.get("account_id"), str)):
                raise ValueError("bad dry run")
            runs.append(DryRun(item["worker"], float(item["at"]), item["price"], item["gems"],
                               item.get("account_id")))
        unlock, reason, evidence = raw.get("unlock"), raw.get("halted_reason"), raw.get("evidence") or []
        if (unlock is not None and not isinstance(unlock, dict)
                or reason is not None and not isinstance(reason, str)
                or not isinstance(evidence, list) or any(not isinstance(path, str) for path in evidence)):
            raise ValueError("bad unlock, reason or evidence")
        return cls(stage, canary, account, tuple(runs), unlock, reason, tuple(evidence))


@dataclass(frozen=True)
class RolloutChange:
    slot: int
    before: SlotRollout
    after: SlotRollout

    @property
    def promoted(self) -> str | None:
        forward = {("dry_run", "canary"), ("canary", "fleet")}
        return self.after.stage if (self.before.stage, self.after.stage) in forward else None

    @property
    def halted(self) -> bool:
        return self.after.stage == "halted" and self.before.stage != "halted"


def _check_slot(slot: int) -> None:
    if slot not in SLOTS:
        raise ValueError(f"lab slot must be one of {SLOTS}")


def canary_present(fleet_root: Path, worker: str, account_id: str | None) -> bool | None:
    """Whether `worker` is still a pool member bound to `account_id`. None when unreadable."""
    from web.account_catalog import registered_worker
    try:
        members = json.loads((Path(fleet_root) / "reroll-pool.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    if not isinstance(members, list):
        return None
    if not any(isinstance(item, dict) and item.get("name") == worker for item in members):
        return False
    registration = registered_worker(Path(fleet_root) / "workers" / worker)
    if registration is None:
        return False
    return account_id is None or registration.account_id == account_id


def rollout_status(state: SlotRollout, slot: int, worker: str | None) -> str:
    """The Fleet State line for a worker whose next gem step unlocks `slot`."""
    if state.stage == "halted":
        return f"Halted: {state.halted_reason or 'no reason recorded'}"
    if state.stage == "canary":
        return (f"Canary: {state.canary_worker} unlocks slot {slot} next visit"
                if worker == state.canary_worker else "Waiting for canary")
    if state.stage == "fleet":
        return f"Unlocking slot {slot}"
    return f"Rehearsing slot {slot} · {state.rehearsals(worker)}/{PROMOTION_REHEARSALS} dry runs"


class LabUnlockRollout:
    """The shared rollout file under the fleet root, serialized by an OS file lock."""

    def __init__(self, fleet_root: Path) -> None:
        self.root = Path(fleet_root)
        self.path = self.root / "lab-unlock-rollout.json"
        self.lock_path = self.root / ".lab-unlock-rollout.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load(self, *, quarantine: bool) -> dict[int, SlotRollout]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("Lab unlock rollout %s unreadable (%s); every slot is at dry run", self.path, exc)
            return {}
        try:
            document = json.loads(text)
            if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported rollout schema")
            raw = document.get("slots")
            if not isinstance(raw, dict) or any(key not in {str(slot) for slot in SLOTS} for key in raw):
                raise ValueError("slots must be keyed 2-5")
            return {int(key): SlotRollout.from_dict(value) for key, value in raw.items()}
        except (ValueError, TypeError, AttributeError) as exc:
            if quarantine:
                aside = self.path.with_name(f"{self.path.name}.corrupt-{time.time_ns()}")
                os.replace(self.path, aside)
                logger.warning("Lab unlock rollout %s is corrupt (%s); moved to %s; every slot "
                               "is back at dry run", self.path, exc, aside)
            else:
                logger.warning("Lab unlock rollout %s is corrupt (%s); reading every slot as dry run",
                               self.path, exc)
            return {}

    def slots(self, *, quarantine: bool = True) -> dict[int, SlotRollout]:
        """Every slot 2-5. A dashboard read passes quarantine=False and never moves a file."""
        if quarantine:
            with self._locked():
                stored = self._load(quarantine=True)
        else:
            stored = self._load(quarantine=False)
        return {slot: stored.get(slot, SlotRollout()) for slot in SLOTS}

    def slot(self, slot: int) -> SlotRollout:
        _check_slot(slot)
        return self.slots()[slot]

    def may_tap(self, slot: int, worker: str) -> bool:
        state = self.slot(slot)
        return state.stage == "fleet" or state.stage == "canary" and state.canary_worker == worker

    def _update(self, slot: int, change: Callable[[SlotRollout], SlotRollout]) -> RolloutChange:
        _check_slot(slot)
        with self._locked():
            stored = self._load(quarantine=True)
            before = stored.get(slot, SlotRollout())
            after = change(before)
            if after != before:
                stored[slot] = after
                _write_json_atomic(self.path, {
                    "schema_version": SCHEMA_VERSION,
                    "slots": {str(key): value.to_dict() for key, value in sorted(stored.items())}})
        return RolloutChange(slot, before, after)

    def note_dry_run(self, slot: int, worker: str, price: int, gems: int, at: float, *,
                     account_id: str | None = None) -> RolloutChange:
        """Record one clean rehearsal. A price that disagrees with the catalog halts the slot."""
        catalog = lab_catalog.lab_slot_gems(slot)

        def change(current: SlotRollout) -> SlotRollout:
            if current.stage != "dry_run" or catalog is None:
                return current
            if price != catalog:
                return replace(current, stage="halted", halted_reason=(
                    f"Rehearsal read {price} gems for slot {slot}; the catalog says {catalog}"))
            runs = (*current.dry_runs, DryRun(worker, float(at), price, gems, account_id))[-MAX_DRY_RUNS:]
            mine = [run for run in runs
                    if run.worker == worker and run.account_id == account_id and run.price == price]
            if _qualifies(mine):
                return replace(current, stage="canary", canary_worker=worker,
                               canary_account=account_id, dry_runs=runs)
            return replace(current, dry_runs=runs)
        return self._update(slot, change)

    def note_unlock(self, slot: int, worker: str, transaction_key: str, outcome: str, *,
                    at: float) -> RolloutChange:
        """The canary's settled tap. Bought promotes to fleet; a second miss halts."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        record = {"worker": worker, "transaction_key": transaction_key, "outcome": outcome, "at": float(at)}

        def change(current: SlotRollout) -> SlotRollout:
            if current.stage != "canary" or current.canary_worker != worker:
                return current  # fleet stage, or another worker lost the race
            if outcome == "bought":
                return replace(current, stage="fleet", unlock=record)
            if current.unlock is not None and current.unlock.get("outcome") == "not_charged":
                return replace(current, stage="halted", unlock=record,
                               halted_reason="The canary's unlock tap did not land twice")
            return replace(current, unlock=record)
        return self._update(slot, change)

    def halt(self, slot: int, reason: str, evidence: tuple[str, ...] = ()) -> RolloutChange:
        def change(current: SlotRollout) -> SlotRollout:
            kept = (*current.evidence, *evidence)[-MAX_EVIDENCE:]
            if current.stage == "halted":
                return replace(current, evidence=kept)
            return replace(current, stage="halted", halted_reason=reason, evidence=kept)
        return self._update(slot, change)

    def release_canary(self, slot: int, *, expected_worker: str | None = None) -> RolloutChange:
        def change(current: SlotRollout) -> SlotRollout:
            if current.stage != "canary" or (expected_worker is not None
                                             and current.canary_worker != expected_worker):
                return current
            return SlotRollout()
        return self._update(slot, change)

    def release_absent_canary(self, slot: int) -> RolloutChange:
        """Back to dry run when the canary left the pool or now plays another account."""
        state = self.slot(slot)
        if (state.stage != "canary" or state.canary_worker is None
                or canary_present(self.root, state.canary_worker, state.canary_account) is not False):
            return RolloutChange(slot, state, state)
        return self.release_canary(slot, expected_worker=state.canary_worker)

    def reset(self, slot: int) -> RolloutChange:
        """The owner's action on a halted slot."""
        def change(current: SlotRollout) -> SlotRollout:
            if current.stage != "halted":
                raise RolloutError("slot_not_halted")
            return SlotRollout()
        return self._update(slot, change)

    def snapshot(self) -> list[dict[str, Any]]:
        """Dashboard rows for slots 2-5. This read never moves a corrupt file."""
        return [{"slot": slot, "stage": state.stage, "canary_worker": state.canary_worker,
                 "dry_runs": len(state.dry_runs), "price": lab_catalog.lab_slot_gems(slot),
                 "halted_reason": state.halted_reason, "evidence": list(state.evidence)}
                for slot, state in self.slots(quarantine=False).items()]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_unlock_rollout.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lab_unlock_rollout.py tests/test_lab_unlock_rollout.py
git commit -m "Add the fleet's lab-slot unlock rollout record

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Rollout events

**Files:**
- Modify: `events.py`: add the new events after `LabSlotUnlocked` (lines 86–92)
- Modify: `web/ui/lib/types.ts`: add to the `BotEvent` union, next to `FloatingGemClaimed` (line 43)
- Modify: `web/ui/lib/format.ts`: `splitEvent`, before `case "PageChanged"`
- Test: `tests/test_ledger.py` (append), `web/ui/lib/format.test.ts` (append)

**Interfaces:**
- **Consumes:** nothing.
- **Produces:**
  - `events.LabUnlockRehearsed(slot: int, price: int, gems: int)`;
  - `events.LabUnlockPromoted(slot: int, stage: str)`;
  - `events.LabUnlockHalted(slot: int, reason: str)`.
- **Not in the ledger.** These three events move no currency. They must not enter `ledger._REPLAYABLE`, which mirrors `web/ui/lib/ledger.ts::LEDGER_EVENTS` and is pinned by `tests/test_ledger.py`. `LabSlotUnlocked` already records the gem debit for any slot.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ledger.py`:

```python
def test_a_later_lab_slot_unlock_debits_its_own_price() -> None:
    (line,) = ledger.classify(events.LabSlotUnlocked(
        slot=3, price=400, gems_before=500, gems_after=100, seq=16, ts=1000.))
    assert (line.item, line.currency, line.delta, line.price) == ("Lab slot 3", "gems", -400, 400)


def test_rollout_events_are_not_account_history() -> None:
    for event in (events.LabUnlockRehearsed(slot=2, price=100, gems=150),
                  events.LabUnlockPromoted(slot=2, stage="canary"),
                  events.LabUnlockHalted(slot=2, reason="Post-tap screen was not understood")):
        assert ledger.classify(event) == ()
        assert event.type not in ledger._REPLAYABLE
```

Append to `web/ui/lib/format.test.ts`:

```ts
group("lab unlock rollout events", () => {
  it("reads the rehearsal, promotion and halt", () => {
    expect(describe({ type: "LabUnlockRehearsed", seq: 1, ts: 0, slot: 2, price: 100, gems: 150 }))
      .toContain("rehearsed Lab 2 unlock: 100 gems, wallet 150");
    expect(describe({ type: "LabUnlockPromoted", seq: 2, ts: 0, slot: 2, stage: "canary" }))
      .toContain("Lab 2 unlock promoted to canary");
    expect(describe({ type: "LabUnlockHalted", seq: 3, ts: 0, slot: 3, reason: "price 120" }))
      .toContain("Lab 3 unlock halted: price 120");
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_ledger.py`
Expected: FAIL with `AttributeError: module 'events' has no attribute 'LabUnlockRehearsed'`.

Run: `cd web/ui && npx vitest run lib/format.test.ts`
Expected: FAIL. The type check rejects the unknown event type, or the line lacks the text.

- [ ] **Step 3: Implement**

In `events.py`, after `LabSlotUnlocked`:

```python
@dataclass(frozen=True, kw_only=True)
class LabUnlockRehearsed(Event):
    """A clean lab-slot unlock rehearsal: read twice, nothing tapped or prepared."""
    slot: int
    price: int
    gems: int


@dataclass(frozen=True, kw_only=True)
class LabUnlockPromoted(Event):
    """The fleet's rollout for this slot moved forward, to canary or fleet."""
    slot: int
    stage: str


@dataclass(frozen=True, kw_only=True)
class LabUnlockHalted(Event):
    """Nobody taps this slot until the owner resets it."""
    slot: int
    reason: str
```

In `web/ui/lib/types.ts`, add three members to the `BotEvent` union, directly after the `FloatingGemClaimed` member:

```ts
  | (EventBase & { type: "LabUnlockRehearsed"; slot: number; price: number; gems: number })
  | (EventBase & { type: "LabUnlockPromoted"; slot: number; stage: string })
  | (EventBase & { type: "LabUnlockHalted"; slot: number; reason: string })
```

In `web/ui/lib/format.ts` `splitEvent`, before `case "PageChanged":`:

```ts
    case "LabUnlockRehearsed":
      return { kind: "LAB", body: `rehearsed Lab ${event.slot} unlock: ${event.price} gems, wallet ${event.gems}` };
    case "LabUnlockPromoted":
      return { kind: "LAB", body: `Lab ${event.slot} unlock promoted to ${event.stage}` };
    case "LabUnlockHalted":
      return { kind: "LAB!", body: `Lab ${event.slot} unlock halted: ${event.reason}` };
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -p no:allure_pytest -q tests/test_ledger.py`
Run: `cd web/ui && npx vitest run lib/format.test.ts lib/ledger.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add events.py tests/test_ledger.py web/ui/lib/types.ts web/ui/lib/format.ts web/ui/lib/format.test.ts
git commit -m "Add the lab unlock rehearsal, promotion and halt events

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Per-account slot state and the gem lane

**Files:**
- Modify: `lab_plan.py`: `LabCadence` (lines 84–199)
- Modify: `lab_runtime.py`: `_legacy` (lines 308–340)
- Modify: `fleet/resource_blocks.py`, in these places:
  - `LabFacts` (lines 428–446) and `GemStep` (lines 486–493);
  - `_slot2_now` (lines 561–569), `_gem_met` (lines 765–772) and `_gem_plan` (lines 783–814);
  - `_slot_context` (lines 845–846) and `evaluate_lab_plan` (lines 872–873).
- Modify: `fleet/lab_facts.py`: `persisted_lab_facts` (lines 78–87)
- Modify: `fleet/reroll_progress.py`: next to `note_lab_slot2` (line 153), and `lab_strategy_plan` (lines 164–187)
- Test: `tests/test_lab_plan.py`, `tests/test_lab_runtime.py`, `tests/test_resource_blocks.py`, `tests/test_reroll_progress.py` (append)

**Interfaces:**
- **Consumes:** nothing from earlier tasks.
- **Produces:**
  - `lab_plan.SLOT_STATES = frozenset({"locked", "owned"})`.
  - On `LabCadence`:
    - `.slots_path` (`lab-slots.json`);
    - `.slot_records() -> dict[int, dict[str, object]]`, each entry being `{status, wallet_gems, observed_at}`;
    - `.slot_owned(slot: int) -> bool`;
    - `.slot_due(slot: int, now: float, wallet_gems: int | None = None, min_gems: int | None = None) -> bool`;
    - `.note_slots(statuses: Mapping[int, str], wallet_gems: int | None, now: float) -> None`.
  - `lab-slots.json` has the shape `{"account_id": "...", "slots": {"2": {"status", "wallet_gems", "observed_at"}}}`. The first `note_slots` merges an existing `lab-slot2-cadence.json` in as slot 2. After that, the old file is never read.
  - The `slot2_owned`, `slot2_due`, `note_slot2` and `route_observation` wrappers keep working until Task 10.
  - `fleet.resource_blocks`:
    - `LabFacts.slot_ownership: Mapping[int, Mapping[str, Any]] | None = None` and `LabFacts.owned_floor: int | None = None`;
    - `GemStep.slot: int | None = None`, as the last field;
    - `_gem_met(block, ownership, owned_floor=None) -> bool | None`;
    - `gem_lane_blocks(gems) -> tuple[dict, ...]`;
    - `next_unlock_slot(blocks, ownership, owned_floor=None) -> int | None`.
  - `RerollProgress.note_lab_slots(statuses, wallet_gems, now=None)` and `RerollProgress.next_unlock_slot() -> int | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lab_plan.py`:

```python
def test_lab_slots_file_records_each_slot_and_reads_the_old_slot_two_file_once(tmp_path: Path) -> None:
    import json
    from lab_plan import LabCadence

    (tmp_path / "lab-slot2-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "status": "owned", "wallet_gems": 19, "observed_at": 900.}))
    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    assert cadence.slot_records() == {2: {"status": "owned", "wallet_gems": 19, "observed_at": 900.}}
    assert cadence.slot_owned(2) and not cadence.slot_owned(3)
    cadence.note_slots({3: "locked"}, 120, 1000.)
    assert json.loads((tmp_path / "lab-slots.json").read_text()) == {
        "account_id": "ACCOUNT-A", "slots": {
            "2": {"status": "owned", "wallet_gems": 19, "observed_at": 900.},
            "3": {"status": "locked", "wallet_gems": 120, "observed_at": 1000.}}}
    (tmp_path / "lab-slot2-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "status": "locked", "observed_at": 2000.}))
    assert LabCadence(tmp_path, "ACCOUNT-A").slot_owned(2)  # the old file is never read again
    assert LabCadence(tmp_path, "ACCOUNT-B").slot_records() == {}


def test_slot_due_waits_for_gems_then_rechecks_hourly(tmp_path: Path) -> None:
    from lab_plan import LabCadence

    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    assert cadence.slot_due(3, 1000., wallet_gems=10, min_gems=400)  # never read
    cadence.note_slots({3: "locked"}, 120, 1000.)
    assert not cadence.slot_due(3, 1100., wallet_gems=399, min_gems=400)
    assert cadence.slot_due(3, 1100., wallet_gems=400, min_gems=400)
    cadence.note_slots({3: "locked"}, 400, 1150.)
    assert not cadence.slot_due(3, 1200., wallet_gems=400, min_gems=400)
    assert cadence.slot_due(3, 1150. + 3600, wallet_gems=400, min_gems=400)
    cadence.note_slots({3: "owned"}, 0, 5000.)
    assert not cadence.slot_due(3, 100_000.)


def test_note_slots_ignores_unknown_slots_and_statuses(tmp_path: Path) -> None:
    from lab_plan import LabCadence

    LabCadence(tmp_path, "ACCOUNT-A").note_slots({1: "owned", 6: "locked", 2: "unknown"}, 5, 1.)
    assert not (tmp_path / "lab-slots.json").exists()
```

Append to `tests/test_lab_runtime.py`:

```python
def test_lab_slots_file_is_historical_for_every_locked_or_owned_slot(tmp_path: Path) -> None:
    from lab_plan import LabCadence
    from lab_runtime import LabRuntime

    LabCadence(tmp_path, "ACCOUNT-A").note_slots({2: "owned", 3: "locked"}, 50, 1000.)
    snapshot = LabRuntime(tmp_path, "ACCOUNT-A").snapshot()
    assert [slot.state for slot in snapshot.slots[1:3]] == ["owned_unread", "locked"]
    assert all(slot.evidence_status == "historical" and not slot.confirmed
               for slot in snapshot.slots[1:3])
    assert snapshot.slots_owned == 2
```

Append to `tests/test_resource_blocks.py`:

```python
def test_gem_lane_reads_every_slot_from_slot_ownership() -> None:
    owned = {"status": "owned", "wallet_gems": 500, "observed_at": 900.}
    locked = {"status": "locked", "wallet_gems": 500, "observed_at": 900.}
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_gems=500,
                                                        slot_ownership={2: owned, 3: locked}))
    assert [step.state for step in plan.gems.steps][:3] == ["done", "current", "next"]
    assert (plan.gems.next.block_id, plan.gems.next.slot, plan.gems.price) == ("gems.lab3", 3, 400)
    assert plan.slots[2].now.state == "locked"


def test_a_complete_strip_proves_the_lower_slots_owned() -> None:
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_gems=5, owned_floor=3))
    assert plan.gems.next.block_id == "gems.lab4"


def test_next_unlock_slot_follows_the_gem_lane() -> None:
    blocks = rb.template_gem_blocks()
    assert rb.next_unlock_slot(blocks, {}) == 2
    assert rb.next_unlock_slot(blocks, {2: {"status": "owned"}}) == 3
    assert rb.next_unlock_slot(blocks, {}, owned_floor=5) is None  # the next step is card slots
    cards_first = rb.validate_gems([
        {"id": "lab2", "type": "unlock_lab_slot", "slot": 2},
        {"id": "cards", "type": "buy_cards", "purpose": "card_missions"},
        {"id": "lab3", "type": "unlock_lab_slot", "slot": 3}])
    assert rb.next_unlock_slot(cards_first, {2: {"status": "owned"}}) is None
    assert rb.next_unlock_slot(rb.legacy_gem_blocks(("unlock_lab_slot_2",)),
                               {2: {"status": "owned"}}) is None
```

Append to `tests/test_reroll_progress.py`:

```python
def test_slot_notes_and_the_next_unlock_slot_are_account_bound(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    assert progress.next_unlock_slot() == 2
    progress.note_lab_slots({2: "owned", 3: "locked"}, 120, now=1000.)
    assert progress.next_unlock_slot() is None  # the default gem path unlocks only Lab 2
    assert progress.lab_cadence.slot_records()[3] == {"status": "locked", "wallet_gems": 120,
                                                      "observed_at": 1000.}
    assert worker(tmp_path / "other", "ACCOUNT-B").lab_cadence.slot_records() == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_plan.py tests/test_lab_runtime.py tests/test_resource_blocks.py tests/test_reroll_progress.py`
Expected: FAIL. `slot_records`, `note_slots`, `slot_ownership`, `next_unlock_slot` and `note_lab_slots` are missing.

- [ ] **Step 3: Implement `lab-slots.json` in `lab_plan.py`**

Add `from typing import Mapping` to the imports. Add this after `LAB2_GEMS`:

```python
# Per-slot ownership a Labs visit can prove: an owned card or the "Unlock Nth lab" tile.
SLOT_STATES = frozenset({"locked", "owned"})
```

In `LabCadence.__init__`, add `self.slots_path = Path(root) / "lab-slots.json"`. Replace `slot2_owned`, `route_observation`, `slot2_due` and `note_slot2` with the following:

```python
    @staticmethod
    def _slot_entry(raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict) or raw.get("status") not in SLOT_STATES:
            return None
        gems, observed = raw.get("wallet_gems"), raw.get("observed_at")
        return {"status": raw["status"],
                "wallet_gems": gems if type(gems) is int else None,
                "observed_at": (float(observed) if isinstance(observed, (int, float))
                                and not isinstance(observed, bool) else None)}

    def slot_records(self) -> dict[int, dict[str, object]]:
        """Slots 2-5 this account was seen owning or locked, from lab-slots.json.

        Until that file exists, an older lab-slot2-cadence.json stands in for slot 2.
        """
        data = self._read(self.slots_path)
        if data is None:
            legacy = self._slot_entry(self._read(self.slot2_path))
            return {2: legacy} if legacy is not None else {}
        raw = data.get("slots")
        records: dict[int, dict[str, object]] = {}
        for key, value in (raw.items() if isinstance(raw, dict) else ()):
            entry = self._slot_entry(value)
            if key in {"2", "3", "4", "5"} and entry is not None:
                records[int(key)] = entry
        return records

    def slot_owned(self, slot: int) -> bool:
        record = self.slot_records().get(slot)
        return record is not None and record["status"] == "owned"

    def slot_due(self, slot: int, now: float, wallet_gems: int | None = None,
                 min_gems: int | None = None) -> bool:
        """Revisit a locked slot once the gems cover it, else hourly. Never an owned slot."""
        record = self.slot_records().get(slot)
        if record is None:
            return True
        if record["status"] == "owned":
            return False
        if type(wallet_gems) is int and type(min_gems) is int:
            if wallet_gems < min_gems:
                return False
            previous = record.get("wallet_gems")
            if type(previous) is int and previous < min_gems:
                return True
        observed = record.get("observed_at")
        return not isinstance(observed, (int, float)) or now >= observed + 3600

    def note_slots(self, statuses: Mapping[int, str], wallet_gems: int | None, now: float) -> None:
        valid = {slot: status for slot, status in statuses.items()
                 if slot in (2, 3, 4, 5) and status in SLOT_STATES}
        if not valid:
            return
        records = self.slot_records()
        for slot, status in valid.items():
            records[slot] = {"status": status, "wallet_gems": wallet_gems, "observed_at": now}
        self._write(self.slots_path, {"account_id": self.account_id,
                                      "slots": {str(slot): records[slot] for slot in sorted(records)}})

    # Slot-2 names kept until the remaining callers move (Task 10).
    def slot2_owned(self) -> bool:
        return self.slot_owned(2)

    def route_observation(self) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        """Account-bound saved Lab decisions for the fleet route display."""
        return self._record(), self.slot_records().get(2)

    def slot2_due(self, now: float, wallet_gems: int | None = None,
                  min_gems: int = LAB2_GEMS) -> bool:
        return self.slot_due(2, now, wallet_gems, min_gems)

    def note_slot2(self, status: str, wallet_gems: int | None, now: float) -> None:
        self.note_slots({2: status}, wallet_gems, now)
```

- [ ] **Step 4: Implement the runtime's legacy import in `lab_runtime.py`**

In `_legacy`, change `for slot in (1, 2):` to `for slot in (1,):` and delete the whole `else:` branch that handles slot 2. Then insert the following before the final `self._snapshot = replace(…)`:

```python
        from lab_plan import LabCadence
        records = LabCadence(self.root, self.scope.account_id).slot_records()
        locked: list[int] = []
        for slot, record in records.items():
            observed = record.get("observed_at")
            if not _finite(observed):
                continue
            state = "owned_unread" if record["status"] == "owned" else "locked"
            slots[slot - 1] = LabJobRecord(self.scope, slot, state=state, observed_at=observed,
                                           evidence_status="historical")
            if state == "locked":
                locked.append(slot)
        owned = min(locked) - 1 if locked else None
```

- [ ] **Step 5: Implement the gem lane in `fleet/resource_blocks.py`**

Add these two fields as the last fields of `LabFacts`:

```python
    # Slots 2-5 from lab-slots.json; None falls back to the legacy `slot2` record.
    slot_ownership: Mapping[int, Mapping[str, Any]] | None = None
    # A complete strip read proves every slot up to here owned.
    owned_floor: int | None = None
```

Add `slot: int | None = None` as the last field of `GemStep`.

Rename `_slot2_now` to `_ownership_now` and keep its body. Replace `_gem_met` with the following, and add `_ownership`, `gem_lane_blocks` and `next_unlock_slot` next to it:

```python
def _ownership(facts: LabFacts) -> dict[int, Mapping[str, Any]]:
    """Per-slot ownership (slots 2-5). Without it, the legacy slot-2 record."""
    if facts.slot_ownership is not None:
        return {slot: record for slot, record in facts.slot_ownership.items()
                if slot in (2, 3, 4, 5) and isinstance(record, Mapping)}
    return {2: facts.slot2} if isinstance(facts.slot2, Mapping) else {}


def _gem_met(block: Mapping[str, Any], ownership: Mapping[int, Mapping[str, Any]],
             owned_floor: int | None = None) -> bool | None:
    """True or False when read. None when we cannot know, which is never a guess.

    Slots unlock in order: an owned higher slot proves this one owned, and a
    locked lower slot proves this one locked.
    """
    if block["type"] != "unlock_lab_slot":
        return False
    slot = block["slot"]
    if type(owned_floor) is int and slot <= owned_floor:
        return True
    status = {number: record.get("status") for number, record in ownership.items()}
    if status.get(slot) == "owned" or any(status.get(n) == "owned" for n in range(slot + 1, 6)):
        return True
    if status.get(slot) == "locked" or any(status.get(n) == "locked" for n in range(2, slot)):
        return False
    return None


def gem_lane_blocks(gems: Any) -> tuple[dict[str, Any], ...]:
    """The gem lane as blocks, whether the route stores blocks or legacy steps."""
    return gems.blocks if gems.mode == "blocks" else legacy_gem_blocks(gems.steps)


def next_unlock_slot(blocks: Sequence[Mapping[str, Any]], ownership: Mapping[int, Mapping[str, Any]],
                     owned_floor: int | None = None) -> int | None:
    """The slot the gem lane unlocks next, or None when its next step is not a slot unlock."""
    for block in blocks:
        if _gem_met(block, ownership, owned_floor) is True:
            continue
        return block["slot"] if block["type"] == "unlock_lab_slot" else None
    return None
```

Make these changes in `_gem_plan`:
- Add `ownership = _ownership(facts)` before the loop.
- Change `met = _gem_met(block, facts.slot2)` to `met = _gem_met(block, ownership, facts.owned_floor)`.
- Build each step as `GemStep(block["id"], block["type"], block.get("label") or _gem_label(block), state, _gem_price(block), automated, block["slot"] if block["type"] == "unlock_lab_slot" else None)`.

Make these changes in `_slot_context`:
- Delete `later = SlotNow("unknown")`.
- Build `nows` as:

```python
    ownership = _ownership(facts)
    nows = {1: _slot1_now(facts.slot1, facts.now),
            **{slot: _ownership_now(ownership.get(slot), facts.now) for slot in (2, 3, 4, 5)}}
```

In `evaluate_lab_plan`, replace the `gem_blocks = (…)` expression with `gem_blocks = gem_lane_blocks(route.gems)`.

- [ ] **Step 6: Implement the fleet readers**

In `fleet/lab_facts.py` `persisted_lab_facts`:

```python
    cadence = LabCadence(worker_root, account_id)
    slot1, slot2 = cadence.route_observation()
```

Then add `slot_ownership=cadence.slot_records()` to the `LabFacts(…)` call.

In `fleet/reroll_progress.py`:
- Add `from fleet.build_route import GemRoute` to the existing `fleet.build_route` import.
- Add `from fleet.resource_blocks import gem_lane_blocks, next_unlock_slot`, extending the existing `fleet.resource_blocks` import.
- Add these methods next to `note_lab_slot2`:

```python
    def note_lab_slots(self, statuses: Mapping[int, str], wallet_gems: int | None,
                       now: float | None = None) -> None:
        self.lab_cadence.note_slots(statuses, wallet_gems, time.time() if now is None else now)

    def _effective_gems(self) -> GemRoute:
        """This account's gem lane; today's default when no route applies."""
        if self.route_runtime is None:
            return GemRoute()
        try:
            return resolve_route(self.route_runtime.current(), self.root.name, self.account_id).gems
        except (RouteUnavailable, ValueError, TypeError, KeyError):
            return GemRoute()

    def next_unlock_slot(self) -> int | None:
        """The slot the gem lane unlocks next, from this account's slot record."""
        return next_unlock_slot(gem_lane_blocks(self._effective_gems()), self.lab_cadence.slot_records())
```

In `lab_strategy_plan`, extend the `replace(facts, …)` call with the following:

```python
                        slot_ownership=self.lab_cadence.slot_records(),
                        owned_floor=(getattr(runtime, "slots_owned", None)
                                     if type(getattr(runtime, "slots_owned", None)) is int else None),
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_plan.py tests/test_lab_runtime.py tests/test_resource_blocks.py tests/test_reroll_progress.py tests/test_lab_facts.py tests/test_labs_view.py tests/test_r5_prices.py`
Expected: PASS. The last three are neighbours of the changed readers.

- [ ] **Step 8: Commit**

```bash
git add lab_plan.py lab_runtime.py fleet/resource_blocks.py fleet/lab_facts.py fleet/reroll_progress.py \
  tests/test_lab_plan.py tests/test_lab_runtime.py tests/test_resource_blocks.py tests/test_reroll_progress.py
git commit -m "Record ownership for every lab slot and let the gem lane read it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Settle an unlanded unlock tap as not charged

**Why this is a new journal method.** The spec says a tap that did not land "resolves as not charged, the same way shopping handles a missed tap". For scoped (prepared) transactions that is not what the code does. `TransactionJournal.reconcile` settles an acted scoped row only as BOUGHT or FREE, and a REFUTED judgement becomes UNPROVEN and stays open. Shopping's scoped missed tap therefore stays pending: see `tests/test_shopping_autopilot.py::test_unchanged_purchase_stays_pending_without_another_buy`. The canary needs a narrow, proof-checked way to settle "slot still locked, gems unchanged".

**Files:**
- Modify: `transactions.py`: add a method after `cancel_before_input` (ends at line 477)
- Test: `tests/test_lab_transactions.py` (append)

**Interfaces:**
- **Consumes:** `Stage`, `Verdict`, `Outcome`, `RecoveryEvidence`, and `self.currencies.scope_matches(scope, currency, conn)`.
- **Produces:**
  - `transactions.UNLANDED_SETTLE_SECONDS = 2.`
  - `TransactionJournal.refute_unlanded_unlock(key: str, evidence: RecoveryEvidence, *, now: float) -> Outcome`. It returns `REFUTED` with `spent=0` only for an acted, scoped `lab_unlock` with same-scope evidence that meets all of these:
    - `effect_changed is False`;
    - `wallet_after == wallet_before`;
    - the same slot;
    - a digest;
    - `acted_at + 2 <= observed_at <= now <= observed_at + 30`.

    Any other input returns `UNPROVEN` and leaves the row open. It writes no ledger line. It releases the gem commitment and discards gem wallet observations.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lab_transactions.py`:

```python
def _unlanded(scope: FactScope, **overrides: object) -> RecoveryEvidence:
    return RecoveryEvidence(**{"category": "LABS", "currency": "gems", "wallet_after": 613,
        "effect_changed": False, "observed_at": 13., "frame_digest": "after", "scope": scope,
        "operation": "lab_unlock", "slot": 2, **overrides})


def test_an_unlanded_unlock_tap_is_settled_as_not_charged(tmp_path: Path) -> None:
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope, operation='lab_unlock')  # acted at 11
    outcome = journal.refute_unlanded_unlock(txn.key, _unlanded(scope), now=13.)
    assert (outcome.verdict, outcome.spent) == (Verdict.REFUTED, 0)
    assert journal.open_transactions() == ()
    assert journal.currencies.committed('gems') == 0
    with db.reader(journal.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
    assert journal.refute_unlanded_unlock(txn.key, _unlanded(scope), now=14.) == outcome


@pytest.mark.parametrize("overrides", [
    {"observed_at": 12.5},     # too soon after the tap
    {"wallet_after": 513},     # the gems moved
    {"effect_changed": None},  # the slot was not read
    {"slot": 3},               # another slot
    {"frame_digest": ""},      # no frame behind the read
])
def test_an_unlanded_unlock_needs_complete_proof(tmp_path: Path, overrides: dict) -> None:
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope, operation='lab_unlock')
    outcome = journal.refute_unlanded_unlock(txn.key, _unlanded(scope, **overrides), now=13.)
    assert outcome.verdict == Verdict.UNPROVEN and outcome.spent is None
    assert journal.open_transactions()[0].key == txn.key


def test_only_a_lab_unlock_can_be_refuted_as_unlanded(tmp_path: Path) -> None:
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope, operation='lab_start')
    evidence = _unlanded(scope, currency='coins', operation='lab_start', slot=1)
    assert journal.refute_unlanded_unlock(txn.key, evidence, now=13.).verdict == Verdict.UNPROVEN
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_transactions.py -k unlanded`
Expected: FAIL with `AttributeError: 'TransactionJournal' object has no attribute 'refute_unlanded_unlock'`.

- [ ] **Step 3: Implement**

In `transactions.py`, add this constant below `_recovery_fences`:

```python
# A lab-slot tap is judged unlanded only on a read at least this long after it.
UNLANDED_SETTLE_SECONDS = 2.
```

Add this method after `cancel_before_input`:

```python
    @_recovery_mutation
    def refute_unlanded_unlock(self, key: str, evidence: RecoveryEvidence, *, now: float) -> Outcome:
        """Settle a lab-slot tap that provably did nothing as not charged (spent 0).

        Only for an acted, scoped `lab_unlock` whose own visit read the same slot
        still locked and the gem wallet unchanged, at least UNLANDED_SETTLE_SECONDS
        after the tap. Anything less returns UNPROVEN and leaves the row open. No
        ledger line is written, because nothing was spent.
        """
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM transactions WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            detail = json.loads(row["detail"] or "{}")
            if row["stage"] == Stage.RESOLVED.value:
                return Outcome(key=key, verdict=Verdict(row["outcome"]), spent=row["spent"],
                               reason=detail.get("reason"))
            txn = _transaction(row)
            acted = txn.acted_at
            proven = (
                txn.operation == 'lab_unlock' and txn.stage == Stage.ACTED and txn.scope is not None
                and acted is not None and evidence.scope is not None and evidence.scope == txn.scope
                and self.currencies.scope_matches(evidence.scope, txn.currency, conn)
                and evidence.operation == 'lab_unlock' and evidence.slot == txn.before.get('slot')
                and evidence.category == txn.category and txn.currency == evidence.currency == 'gems'
                and evidence.effect_changed is False and bool(evidence.frame_digest)
                and evidence.wallet_after is not None and evidence.wallet_after == txn.wallet_before
                and acted + UNLANDED_SETTLE_SECONDS <= evidence.observed_at <= now
                and now - evidence.observed_at <= 30)
            if not proven:
                return Outcome(key=key, verdict=Verdict.UNPROVEN, spent=None,
                               reason='unlanded unlock not proven')
            reason = 'lab unlock tap did not land: slot still locked and gems unchanged'
            detail.update(reason=reason, reconciliation=asdict(evidence))
            conn.execute("UPDATE transactions SET stage = ?, outcome = ?, spent = 0, resolved_at = ?, "
                         "detail = ? WHERE key = ?",
                         (Stage.RESOLVED.value, Verdict.REFUTED.value, now, json.dumps(detail), key))
            conn.execute("DELETE FROM currency_commitments WHERE owner=? AND currency=?",
                         (f'purchase:{key}', txn.currency))
            conn.execute("DELETE FROM currency_observations WHERE currency=?", (txn.currency,))
            return Outcome(key=key, verdict=Verdict.REFUTED, spent=0, reason=reason)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_transactions.py tests/test_transactions.py tests/test_reconcile_transaction.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add transactions.py tests/test_lab_transactions.py
git commit -m "Settle a provably unlanded lab-slot tap as not charged

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `LabVisitOptions` names the slots a visit may unlock

**Files:**
- Modify: `lab_plan.py`: `LabVisitOptions` (lines 20–25)
- Modify: `lab_visit.py`, in three places:
  - `request` (line 122);
  - `_prepare`'s reserve (line 205);
  - `_unlock_lab_two`'s condition (lines 357–360).
- Modify: `fleet/reroll_progress.py`: `lab_due` (lines 105–115), `lab_visit_options` (lines 117–121), and the `from lab_plan import LAB2_GEMS, …` line
- Modify: `tower_bot.py`: `_authorize_lab` (lines 1334–1336), and the two `LabVisitOptions(start_research=False, unlock_slot2=False)` calls (lines ~1972 and ~2635)
- Test: update the call sites listed in Step 5; add a test to `tests/test_build_route_integration.py`

**Interfaces:**
- **Consumes:**
  - `RerollProgress.next_unlock_slot()` and `LabCadence.slot_due` (Task 4);
  - `lab_catalog.lab_slot_gems`.
- **Produces:**
  - `LabVisitOptions(start_research: bool = True, unlock_slots: tuple[int, ...] = (), keep_gems: int = 0)`. By default nothing is unlocked.
  - `RerollProgress.lab_visit_options()` returns `unlock_slots=(next_unlock_slot,)` when auto-unlock is on and the gem lane's next step is a slot unlock. Otherwise it returns `()`. `keep_gems` is `rules.gems.keep`.
  - `RerollProgress.lab_due()` checks `slot_due(slot, min_gems=price + keep)` for that slot.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_build_route_integration.py`, and add `from fleet.resource_blocks import template_gem_blocks` to its imports:

```python
def _gem_blocks_route(root: Path, expected: int = 0) -> RouteDocument:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["gems"].update(mode="blocks", blocks=list(template_gem_blocks()))
    return BuildRouteStore(root).publish(RouteDocument.from_dict(raw), expected, "operator")


def test_lab_three_is_the_visit_unlock_once_lab_two_is_owned(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _gem_blocks_route(tmp_path)
    progress.note_lab_unlocked("labs_tab", now=999.)
    _game_speed_waits(progress)
    progress.note_lab_slots({2: "owned", 3: "locked"}, 120, now=1000.)
    assert progress.lab_visit_options() == LabVisitOptions(unlock_slots=(3,), keep_gems=0)
    assert not progress.lab_due(now=1100., wallet_coins=100, wallet_gems=399)
    assert progress.lab_due(now=1100., wallet_coins=100, wallet_gems=400)
```

In the same file, update `test_auto_start_and_auto_unlock_switches_gate_the_lab_visit`:
- `assert progress.lab_visit_options() == LabVisitOptions()` becomes `assert progress.lab_visit_options() == LabVisitOptions(unlock_slots=(2,))`.
- `LabVisitOptions(start_research=False, unlock_slot2=False)` becomes `LabVisitOptions(start_research=False)`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_build_route_integration.py -k "lab"`
Expected: FAIL with `TypeError: LabVisitOptions.__init__() got an unexpected keyword argument 'unlock_slots'`.

- [ ] **Step 3: Implement**

In `lab_plan.py`:

```python
@dataclass(frozen=True)
class LabVisitOptions:
    """What one Labs visit may do. Nothing is unlocked unless a slot is named."""
    start_research: bool = True
    unlock_slots: tuple[int, ...] = ()
    keep_gems: int = 0
```

In `lab_visit.py`, make three changes:
- In `request`: `self._options = options or LabVisitOptions()`.
- In `_prepare`: `reserve=self._options.keep_gems if currency == 'gems' else 0`.
- In `_unlock_lab_two`, replace the first condition with:

```python
        if (not unlock_gate(2).enabled or 2 not in self._options.unlock_slots or self._slot2_tapped
                or home.slot2_status != "locked" or home.slot2_price != 100
                or home.gem_balance is None
                or home.gem_balance < home.slot2_price + self._options.keep_gems
                or home.slot2_point is None):
            return False
```

In `fleet/reroll_progress.py`:
- Change the import to `from lab_plan import LabCadence, LabDecision, LabVisitOptions`.
- Add `import lab_catalog`.
- Replace `lab_due`'s body after `rules = self.resource_rules()`, and replace `lab_visit_options`:

```python
        unlock = False
        slot = self.next_unlock_slot() if rules.gems.auto_unlock_lab_slots else None
        price = lab_catalog.lab_slot_gems(slot) if slot is not None else None
        if price is not None:
            unlock = self.lab_cadence.slot_due(slot, moment, wallet_gems,
                                               min_gems=price + rules.gems.keep)
        research = rules.labs.auto_start and self.lab_cadence.due(moment, wallet_coins)
        return unlock or research

    def lab_visit_options(self) -> LabVisitOptions:
        rules = self.resource_rules()
        slot = self.next_unlock_slot() if rules.gems.auto_unlock_lab_slots else None
        return LabVisitOptions(start_research=rules.labs.auto_start,
                               unlock_slots=(slot,) if slot is not None else (),
                               keep_gems=rules.gems.keep)
```

In `tower_bot.py`:
- `_authorize_lab`'s unlock branch becomes:

```python
        if operation == 'lab_unlock':
            return (unlock_gate(2).enabled and 2 in options.unlock_slots
                    and options.keep_gems == self.lab_visit._options.keep_gems)
```

- Both read-only inspection requests become `LabVisitOptions(start_research=False)`.

- [ ] **Step 4: Migrate the remaining `LabVisitOptions` call sites**

| File:line | Old | New |
|---|---|---|
| `tests/test_lab_plan.py:249` | `LabVisitOptions(start_research=True, unlock_slot2=True, min_gems=100)` | `LabVisitOptions(start_research=True, unlock_slots=(), keep_gems=0)` |
| `tests/test_lab_execution.py:75` | `LabVisitOptions(unlock_slot2=False)` | `LabVisitOptions()` |
| `tests/test_lab_execution.py:99` | `LabVisitOptions(start_research=False, unlock_slot2=False)` | `LabVisitOptions(start_research=False)` |
| `tests/test_lab_execution.py:121` | `LabVisitOptions(min_gems=150)` | `LabVisitOptions(unlock_slots=(2,), keep_gems=50)` |
| `tests/test_lab_transactions.py:39,101` | `LabVisitOptions(unlock_slot2=False)` | `LabVisitOptions()` |
| `tests/test_lab_transactions.py:126` | `LabVisitOptions(start_research=False, unlock_slot2=False)` | `LabVisitOptions(start_research=False)` |
| `tests/test_lab_visit.py:101` | `LabVisitOptions(start_research=False, unlock_slot2=False)` | `LabVisitOptions(start_research=False)` |
| `tests/test_lab_visit.py:379` | `(LabVisitOptions(unlock_slot2=False), LabVisitOptions(min_gems=200))` | `(LabVisitOptions(), LabVisitOptions(unlock_slots=(2,), keep_gems=100))` |
| `tests/test_lab_visit.py:384` | `LabVisitOptions(min_gems=150)` | `LabVisitOptions(unlock_slots=(2,), keep_gems=50)` |
| `tests/test_lab_towerbot.py:46,119,133` | `LabVisitOptions(unlock_slot2=False)` | `LabVisitOptions()` |
| `tests/test_lab_towerbot.py:147,148` | `LabVisitOptions(unlock_slot2=True, min_gems=100)` | `LabVisitOptions(unlock_slots=(2,))` |
| `tests/test_shopping_loop.py:159` | `LabVisitOptions(start_research=False, unlock_slot2=False, min_gems=150)` | `LabVisitOptions(start_research=False, keep_gems=50)` |

Then confirm that nothing is left: `git grep -nwE "unlock_slot2|min_gems" -- '*.py'` must print nothing.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -p no:allure_pytest -q tests/test_build_route_integration.py tests/test_lab_plan.py tests/test_lab_execution.py tests/test_lab_transactions.py tests/test_lab_visit.py tests/test_lab_towerbot.py tests/test_reroll_progress.py`
Run: `uv run pytest -p no:allure_pytest -q tests/test_shopping_loop.py -k lab`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lab_plan.py lab_visit.py fleet/reroll_progress.py tower_bot.py tests/test_build_route_integration.py \
  tests/test_lab_plan.py tests/test_lab_execution.py tests/test_lab_transactions.py tests/test_lab_visit.py \
  tests/test_lab_towerbot.py tests/test_shopping_loop.py
git commit -m "Let a Labs visit unlock the slot the gem lane names, keeping its gem reserve

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Executor (rehearse, tap, three outcomes) and worker wiring

**Files:**
- Modify: `lab_visit.py`, in these places:
  - imports; `LabVisitResult` (lines 31–44); `__init__` (lines 50–99) and `request` (lines 105–141);
  - `_prepare` (lines 167–210); `_recover` (lines 244–287); `_return` (lines 348–353);
  - replace `_unlock_lab_two` (lines 355–377);
  - `advance` (the pending check at lines 428–431, the `confirm_slot2` state at line 542, and the return state at line 556).
- Modify: `fleet/reroll_progress.py`: add `fleet_root`, `worker_id` and `unlock_rollout`
- Modify: `tower_bot.py`, in these places:
  - imports (line 44);
  - after `self.shopping.evidence_dir = self.stall_dir` (line 273);
  - `_authorize_lab` (lines 1334–1336);
  - `_finish_lab_visit` (lines 1441–1447).
- Test: `tests/test_lab_slot_unlock.py` (create). Update `tests/test_lab_execution.py:118-126`, `tests/test_lab_visit.py:376-385` and `tests/test_lab_towerbot.py:146-149`. Append to `tests/test_shopping_loop.py`, `tests/test_reroll_progress.py` and `tests/test_lab_transactions.py`.

**Interfaces:**
- **Consumes:**
  - `LockedSlot` and `LabHomeReading.next_locked` (Task 1);
  - `LabUnlockRollout` with `may_tap`, `slot`, `note_dry_run`, `note_unlock`, `halt` and `release_absent_canary`, plus `RolloutChange` (Task 2);
  - the three events (Task 3);
  - `note_lab_slots` (Task 4);
  - `refute_unlanded_unlock` (Task 5);
  - `LabVisitOptions.unlock_slots/keep_gems` (Task 6).
- **Produces:**
  - `LabVisit(…, rollout: LabUnlockRollout | None = None, worker: str | None = None, evidence_dir: Path | None = None)`. These are also public attributes that TowerBot sets.
  - `LabVisitResult.slot_status: tuple[tuple[int, str], ...] = ()`, which replaces `slot2_status`, and `LabVisitResult.unlocked_slot: int | None = None`.
  - `LabVisit._unlock_slot(home: LabHomeReading, screen: Image, device) -> bool`. True means stay on Labs this scan.
  - Tap action name: `f"unlock_lab_slot_{N}"`. Visit state after the tap: `"confirm_slot"`.
  - Visit result reasons: `slot_unlocked`, `unlock_not_landed`, and `lab_unlock_uncertain`, which also sets `recovery_status`.
  - `Skipped(action='lab_unlock', reason=…)`, where the reason is one of `price_unknown`, `slot_halted` or `waiting_for_canary`.
  - Evidence file names: `evidence/lab-unlock-slot{N}-{int(acted_at)}-{k}.png`.
  - `RerollProgress.fleet_root -> Path | None`, `.worker_id -> str | None` and `.unlock_rollout() -> LabUnlockRollout | None`.
  - `TowerBot._authorize_lab('lab_unlock', LabDecision('unlock_slot', price=P, slot=N), now)`.

- [ ] **Step 1: Write the failing executor tests**

Create `tests/test_lab_slot_unlock.py`:

```python
"""The bot's own lab-slot unlock: rehearsal, canary tap and the three post-tap outcomes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
import db
import events
import ocr
import transactions
from evidence_scope import BalanceInterval
from lab_plan import LabVisitOptions
from lab_runtime import LabRuntime
from lab_screen import read_slots
from lab_unlock_rollout import LabUnlockRollout
from lab_visit import LabVisit
from tests.test_lab_transactions import authority
from tests.test_lab_visit import Device, boxes, frame
from tests.test_labs_view import _registered
import vision

IMAGE = "menu_labs_slot1_idle"
PRICE_POINT = (586, 906)


def locked_boxes(gems: str | None = "150", price: str = "100") -> tuple[ocr.TextBox, ...]:
    """Lab 1 idle, Lab 2 locked for `price` gems, `gems` in the header (None drops it)."""
    result = []
    for box in boxes(IMAGE):
        if box.text == "65":
            if gems is not None:
                result.append(ocr.TextBox(gems, box.confidence, box.rect))
        elif box.text == "100":
            result.append(ocr.TextBox(price, box.confidence, box.rect))
        else:
            result.append(box)
    return tuple(result)


def owned_boxes(gems: str = "50") -> tuple[ocr.TextBox, ...]:
    """Lab 2 owned and idle; Lab 3 is the next locked tile."""
    kept = tuple(ocr.TextBox(gems, box.confidence, box.rect) if box.text == "65" else box
                 for box in boxes(IMAGE) if box.text not in {"Unlock Znd lab", "100"})
    return kept + (ocr.TextBox("Lab Offline", .99, config.Rect(394, 832, 291, 52)),
                   ocr.TextBox("Lab 3", .99, config.Rect(25, 1046, 97, 39)),
                   ocr.TextBox("Unlock 3rd lab", .99, config.Rect(351, 1174, 378, 51)),
                   ocr.TextBox("400", .99, config.Rect(536, 1271, 101, 53)))


def three_owned_boxes(gems: str = "100") -> tuple[ocr.TextBox, ...]:
    """Labs 2 and 3 owned and idle; Lab 4 is the next locked tile."""
    two = tuple(box for box in owned_boxes(gems) if box.text not in {"Unlock 3rd lab", "400"})
    return two + (ocr.TextBox("Lab Offline", .99, config.Rect(395, 1225, 290, 48)),
                  ocr.TextBox("Lab 4", .99, config.Rect(26, 1437, 100, 38)),
                  ocr.TextBox("Unlock 4th lab", .99, config.Rect(351, 1566, 378, 51)))


def dialog_boxes() -> tuple[ocr.TextBox, ...]:
    """A post-tap frame no Labs reader understands, standing in for a gem dialog."""
    return (ocr.TextBox("150", .99, config.Rect(439, 26, 75, 51)),
            ocr.TextBox("Unlock this lab?", .99, config.Rect(300, 900, 480, 60)))


def promote_canary(rollout: LabUnlockRollout, worker: str = "Air_1") -> None:
    rollout.note_dry_run(2, worker, 100, 150, 0., account_id="account-a")
    rollout.note_dry_run(2, worker, 100, 150, 700., account_id="account-a")


class UnlockHarness:
    def __init__(self, fleet: Path, monkeypatch: pytest.MonkeyPatch, worker: str = "Air_1") -> None:
        self.root = fleet / "workers" / worker
        self.root.mkdir(parents=True, exist_ok=True)
        self.account, self.journal, self.scope = authority(self.root)
        self.rollout = LabUnlockRollout(fleet)
        self.time = 10.
        self.events: list[events.Event] = []
        self.device = Device()
        self.runtime = LabRuntime(self.root, 'account-a', lease_id='lease', generation='generation')
        for stamp in (8., 9.):
            self.runtime.observe(read_slots(frame(IMAGE), locked_boxes(), observed_at=stamp))
        self.visit = LabVisit(vision.TemplateCache(Path('templates')), journal=self.journal,
                              account_state=self.account, runtime=self.runtime,
                              wall_clock=lambda: self.time, event_sink=self.events.append,
                              rollout=self.rollout, worker=worker, evidence_dir=self.root / "evidence")
        monkeypatch.setattr('lab_visit.tap', lambda device, x, y: device.taps.append((x, y)))

    def open(self, options: LabVisitOptions = LabVisitOptions(start_research=False,
                                                              unlock_slots=(2,))) -> None:
        assert self.visit.request(options)

    def scan(self, text: tuple[ocr.TextBox, ...], name: str = IMAGE):
        self.time += 1
        return self.visit.advance(frame(name), text, self.device, self.time,
                                  observed_at=self.time, capture_scope=self.scope)

    def leave(self, text: tuple[ocr.TextBox, ...]) -> str | None:
        """Scan Labs until the visit taps the unlock or Battle; return that tap's name."""
        for _ in range(6):
            self.scan(text)
            tap = self.visit.last_tap[0] if self.visit.last_tap else None
            if tap in {"unlock_lab_slot_2", "return_to_battle"}:
                return tap
        return None

    def finish(self):
        return self.scan((), "menu_main_labs_unlocked")

    def of(self, kind: type) -> list:
        return [event for event in self.events if isinstance(event, kind)]

    def transactions(self) -> int:
        with db.reader(self.journal.path) as conn:
            return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def test_a_rehearsal_never_taps_and_never_prepares_a_transaction(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    result = h.finish()
    assert result is not None and result.slot_status == ((2, "locked"),)
    assert PRICE_POINT not in h.device.taps and h.transactions() == 0
    state = h.rollout.slot(2)
    assert state.stage == "dry_run"
    assert [(run.worker, run.price, run.gems) for run in state.dry_runs] == [("Air_1", 100, 150)]
    assert [(e.slot, e.price, e.gems) for e in h.of(events.LabUnlockRehearsed)] == [(2, 100, 150)]


def test_a_second_rehearsal_ten_minutes_later_promotes_the_canary(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.open()
    h.leave(locked_boxes())
    h.finish()
    h.time = 800.
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    assert h.rollout.slot(2).canary_worker == "Air_1"
    assert [e.stage for e in h.of(events.LabUnlockPromoted)] == ["canary"]
    assert h.transactions() == 0


def test_the_canary_taps_once_and_a_proven_unlock_promotes_the_slot_to_fleet(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    assert h.leave(locked_boxes()) == "unlock_lab_slot_2"
    (txn,) = h.journal.open_transactions()
    assert (txn.operation, txn.before["slot"], txn.price, txn.item) == ("lab_unlock", 2, 100, "Lab 2")
    h.scan(owned_boxes())
    h.scan(owned_boxes())
    assert h.journal.open_transactions() == ()
    assert h.leave(owned_boxes()) == "return_to_battle"
    result = h.finish()
    assert (result.unlocked_slot, result.observed_gem_spend) == (2, 100)
    assert result.slot_status == ((2, "owned"), (3, "locked"))
    assert h.device.taps.count(PRICE_POINT) == 1
    state = h.rollout.slot(2)
    assert state.stage == "fleet" and state.unlock["transaction_key"] == txn.key
    assert [(e.slot, e.price, e.gems_before, e.gems_after)
            for e in h.of(events.LabSlotUnlocked)] == [(2, 100, 150, 50)]
    assert [e.stage for e in h.of(events.LabUnlockPromoted)] == ["fleet"]
    with db.reader(h.journal.path) as conn:
        assert [tuple(row) for row in conn.execute(
            "SELECT item, delta FROM ledger WHERE kind='LAB'")] == [("Lab slot 2", -100)]


def test_one_transitional_frame_after_the_tap_does_not_halt(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    h.leave(locked_boxes())
    h.scan(dialog_boxes())
    h.scan(owned_boxes())
    h.scan(owned_boxes())
    assert h.journal.open_transactions() == ()
    assert h.rollout.slot(2).stage == "fleet" and not h.of(events.LabUnlockHalted)


def test_a_tap_that_did_not_land_is_not_charged_and_a_second_miss_halts(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    for _ in range(2):
        h.open()
        assert h.leave(locked_boxes()) == "unlock_lab_slot_2"
        h.scan(locked_boxes())
        h.scan(locked_boxes())
        assert h.journal.open_transactions() == ()
        assert h.leave(locked_boxes()) == "return_to_battle"
        assert h.finish().reason == "unlock_not_landed"
    assert h.rollout.slot(2).stage == "halted"
    assert [e.reason for e in h.of(events.LabUnlockHalted)] == ["The canary's unlock tap did not land twice"]
    assert not h.of(events.LabSlotUnlocked)
    with db.reader(h.journal.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger WHERE kind='LAB'").fetchone()[0] == 0


def test_an_unreadable_post_tap_screen_halts_the_canary_and_keeps_the_hold(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    h.leave(locked_boxes())
    results = [h.scan(dialog_boxes()) for _ in range(3)]
    assert results[-1] is not None and results[-1].reason == "lab_unlock_uncertain"
    assert h.visit.recovery_status == "lab_unlock_uncertain"
    assert h.journal.open_transactions()[0].stage is transactions.Stage.ACTED
    state = h.rollout.slot(2)
    assert (state.stage, state.halted_reason) == ("halted", "post-tap screen was not understood")
    assert len(state.evidence) == 4 and all(Path(path).is_file() for path in state.evidence)
    assert Path(state.evidence[0]).name.startswith("lab-unlock-slot2-")
    assert [e.slot for e in h.of(events.LabUnlockHalted)] == [2]


def test_a_debit_that_does_not_match_the_price_halts_the_canary(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    h.leave(locked_boxes())
    h.scan(owned_boxes(gems="40"))
    result = h.scan(owned_boxes(gems="40"))
    assert result is not None and result.reason == "lab_unlock_uncertain"
    assert h.rollout.slot(2).halted_reason == "gem debit did not match the price"
    assert h.journal.open_transactions()


def test_a_fleet_stage_slot_unlocks_on_a_second_worker(tmp_path, monkeypatch) -> None:
    first = UnlockHarness(tmp_path, monkeypatch, "Air_1")
    promote_canary(first.rollout)
    first.open()
    first.leave(locked_boxes())
    first.scan(owned_boxes())
    first.scan(owned_boxes())
    assert first.rollout.slot(2).stage == "fleet"
    second = UnlockHarness(tmp_path, monkeypatch, "Air_2")
    second.open()
    assert second.leave(locked_boxes()) == "unlock_lab_slot_2"
    second.scan(owned_boxes())
    second.scan(owned_boxes())
    assert second.journal.open_transactions() == ()
    assert second.rollout.slot(2).unlock["worker"] == "Air_1"
    assert not second.of(events.LabUnlockPromoted)


def test_another_workers_canary_holds_this_worker_back(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch, "Air_2")
    promote_canary(h.rollout, "Air_1")
    _registered(tmp_path, "Air_1", "account-a")
    (tmp_path / "reroll-pool.json").write_text(json.dumps(
        [{"name": "Air_1", "endpoint": "127.0.0.1:5555", "lease_id": "lease"}]))
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    assert [(e.action, e.reason) for e in h.of(events.Skipped)] == [("lab_unlock", "waiting_for_canary")]
    assert h.rollout.slot(2).canary_worker == "Air_1" and h.transactions() == 0


def test_a_canary_that_left_the_pool_is_released_and_this_worker_rehearses(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch, "Air_2")
    promote_canary(h.rollout, "Air_1")
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    state = h.rollout.slot(2)
    assert state.stage == "dry_run" and [run.worker for run in state.dry_runs] == ["Air_2"]


def test_a_halted_slot_is_skipped_with_its_reason(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.rollout.halt(2, "operator check")
    h.open()
    assert h.leave(locked_boxes()) == "return_to_battle"
    assert [e.reason for e in h.of(events.Skipped)] == ["slot_halted"]


def test_a_rehearsed_price_off_the_catalog_halts_without_rehearsing(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.open()
    h.leave(locked_boxes(price="120"))
    assert h.rollout.slot(2).stage == "halted"
    assert not h.of(events.LabUnlockRehearsed) and len(h.of(events.LabUnlockHalted)) == 1


@pytest.mark.parametrize("options,text", [
    (LabVisitOptions(start_research=False), locked_boxes()),                    # auto-unlock off
    (LabVisitOptions(start_research=False, unlock_slots=(2,)), locked_boxes(gems="99")),  # short
])
def test_no_rehearsal_without_the_switch_or_the_gems(tmp_path, monkeypatch, options, text) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    h.open(options)
    assert h.leave(text) == "return_to_battle"
    assert h.rollout.slot(2).dry_runs == () and h.transactions() == 0


def test_an_unreadable_gem_balance_never_rehearses_or_taps(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()
    assert h.leave(locked_boxes(gems=None)) == "return_to_battle"
    assert PRICE_POINT not in h.device.taps and h.transactions() == 0


def test_a_stale_slot_record_corrects_itself_without_a_tap(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    promote_canary(h.rollout)
    h.open()  # the options still name slot 2
    assert h.leave(owned_boxes(gems="500")) == "return_to_battle"
    assert h.finish().slot_status == ((2, "owned"), (3, "locked"))
    assert h.transactions() == 0 and h.rollout.slot(3).dry_runs == ()


def test_a_restart_settles_an_open_lab_three_unlock_from_the_next_labs_read(tmp_path, monkeypatch) -> None:
    h = UnlockHarness(tmp_path, monkeypatch)
    intent = transactions.Intent(item='Lab 3', category='LABS', currency='gems', price=400,
        wallet_before=500, ts=10., operation='lab_unlock',
        before={'slot': 3, 'research_id': None, 'source_level': None, 'target_level': None,
                'evidence_digest': 'before'})
    balance = BalanceInterval.from_reading('gems', 500, h.scope, 10., 'before')
    txn = h.journal.prepare(intent, scope=h.scope, balance=balance)
    h.journal.record_action(txn.key, at=10.5)
    h.open(LabVisitOptions(start_research=False))
    for _ in range(3):
        h.scan(three_owned_boxes())
    assert h.journal.open_transactions() == ()
    assert [(e.slot, e.price, e.gems_before, e.gems_after)
            for e in h.of(events.LabSlotUnlocked)] == [(3, 400, 500, 100)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_slot_unlock.py`
Expected: FAIL with `TypeError: LabVisit.__init__() got an unexpected keyword argument 'rollout'`.

- [ ] **Step 3: Implement the result, constructor and helpers in `lab_visit.py`**

Imports:
- Add `from pathlib import Path`, `import lab_catalog`, `from lab_screen import LockedSlot` (extend the existing `lab_screen` import), and `from lab_unlock_rollout import LabUnlockRollout, RolloutChange`.
- Change `from lab_routes import research_gate, unlock_gate` to `from lab_routes import research_gate`.

In `LabVisitResult`, replace `slot2_status: str = "unknown"` with:

```python
    slot_status: tuple[tuple[int, str], ...] = ()
```

and add as the last field:

```python
    unlocked_slot: int | None = None
```

Add these module-level helpers above `class LabVisit`:

```python
# Post-tap reads: an unclassified frame counts toward the limit only after the
# owned or still-locked forms fail. The scan cap bounds a slow settle.
_UNLOCK_STRIKES = 3
_UNLOCK_SCANS = 8


def _inside(point: tuple[int, int], tile: tuple[int, int, int, int]) -> bool:
    x, y, width, height = tile
    return x <= point[0] < x + width and y <= point[1] < y + height


def _slot_status(home: LabHomeReading | None, reading: LabsReading | None) -> tuple[tuple[int, str], ...]:
    """Slots 2-5 this visit proved owned or locked. Nothing when the strip was not read."""
    if home is None or reading is None or not reading.strip_read() or reading.slots_owned is None:
        return ()
    owned = reading.slots_owned
    status = [(slot, "owned") for slot in range(2, owned + 1)]
    if home.next_locked is not None and home.next_locked.slot == owned + 1:
        status.append((owned + 1, "locked"))
    return tuple(status)
```

In `LabVisit.__init__`, add these keyword parameters after `event_sink`:

```python
        rollout: LabUnlockRollout | None = None,
        worker: str | None = None,
        evidence_dir: Path | None = None,
```

Store them as `self.rollout`, `self.worker` and `self.evidence_dir`. Replace the four `self._slot2_*` attributes with the following. In `request`, reset the same fields with the same values, and delete the four `_slot2_*` resets there.

```python
        self._home_seen: tuple[LabHomeReading, LabsReading | None] | None = None
        self._unlock_signature: tuple[LockedSlot, int] | None = None
        self._unlock_reads = 0
        self._unlock_done = False
        self._unlock_tap: tuple[str, int] | None = None
        self._unlock_frames: list[Image] = []
        self._unlock_scans = 0
        self._unlock_strikes = 0
        self._unlanded_signature: tuple[int, LockedSlot, int] | None = None
```

- [ ] **Step 4: Implement `_prepare` for slot N**

Change the signature to `def _prepare(self, operation: str, wallet: int, price: int, *, unlock_slot: int | None = None) -> transactions.Transaction | None:`. Replace the `elif (not unlock_gate(2).enabled …)` block with:

```python
        elif (unlock_slot is None or unlock_slot not in self._options.unlock_slots
                or self.rollout is None or self.worker is None
                or not self.rollout.may_tap(unlock_slot, self.worker)
                or not self._reading.strip_read() or self._reading.slots_owned != unlock_slot - 1
                or self.runtime.snapshot().slots_owned != unlock_slot - 1):
            return None
```

Replace the authorize line and the `Intent` construction with:

```python
        unlock = operation == 'lab_unlock'
        decision = LabDecision('unlock_slot', price=price, slot=unlock_slot) if unlock else self._purchase
        if self.authorize is not None and not self.authorize(operation, decision, now):
            return None
        from concepts import REGISTRY
        intent = transactions.Intent(item=f'Lab {unlock_slot}' if unlock else REGISTRY.by_id(research_id).name,
            category='LABS', currency=currency, price=price, wallet_before=wallet,
            ts=now, operation=operation, before={
                'slot': unlock_slot if unlock else selected_slot,
                'research_id': None if unlock else research_id,
                'source_level': None if unlock else target-1,
                'target_level': None if unlock else target,
                'evidence_digest': self._reading.frame_digest, 'catalog_revision': _catalog_revision()})
```

- [ ] **Step 5: Implement the executor, which replaces `_unlock_lab_two`**

Delete `_unlock_lab_two` and add:

```python
    def _emit(self, event: events.Event) -> None:
        if self.event_sink is not None:
            self.event_sink(event)

    def _publish_change(self, change: RolloutChange) -> None:
        if change.promoted:
            self._emit(events.LabUnlockPromoted(slot=change.slot, stage=change.promoted))
        if change.halted:
            self._emit(events.LabUnlockHalted(slot=change.slot, reason=change.after.halted_reason or ""))

    def _skip(self, slot: int, reason: str) -> None:
        self._emit(events.Skipped(action='lab_unlock', reason=reason, detail=f'Lab {slot}'))

    def _unlock_slot(self, home: LabHomeReading, screen: Image, device: AdbDevice) -> bool:
        """On the way out, rehearse or unlock the gem lane's next slot once per visit.

        True keeps the visit on Labs this scan: a first read, or a tap that needs
        its post-tap reads. The rollout record decides between rehearsal, tap and skip.
        """
        locked, scope = home.next_locked, self._scope()
        if (self._unlock_done or self.rollout is None or self.worker is None or scope is None
                or locked is None or locked.slot not in self._options.unlock_slots):
            return False
        catalog = lab_catalog.lab_slot_gems(locked.slot)
        if catalog is None:
            self._unlock_done = True
            self._skip(locked.slot, 'price_unknown')
            return False
        reading = self._reading
        if (locked.price is None or locked.point is None or locked.tile is None
                or not _inside(locked.point, locked.tile) or home.gem_balance is None
                or home.gem_balance < catalog + self._options.keep_gems
                or reading is None or not reading.strip_read()
                or reading.slots_owned != locked.slot - 1):
            return False
        signature = (locked, home.gem_balance)
        if signature != self._unlock_signature:
            self._unlock_signature, self._unlock_reads = signature, 1
            return True
        self._unlock_reads += 1
        if self._unlock_reads < 2:
            return True
        self._unlock_done = True
        slot, price, gems = locked.slot, locked.price, home.gem_balance
        state = self.rollout.slot(slot)
        if state.stage == 'canary' and state.canary_worker != self.worker:
            state = self.rollout.release_absent_canary(slot).after
        if state.stage == 'halted':
            self._skip(slot, 'slot_halted')
            return False
        if price != catalog:
            self._publish_change(
                self.rollout.note_dry_run(slot, self.worker, price, gems, self.wall_clock(),
                                          account_id=scope.account_id)
                if state.stage == 'dry_run' else
                self.rollout.halt(slot, f"Slot {slot} read {price} gems; the catalog says {catalog}"))
            return False
        if self.rollout.may_tap(slot, self.worker):
            return self._tap_unlock(locked, home, screen, device)
        if state.stage == 'dry_run':
            change = self.rollout.note_dry_run(slot, self.worker, price, gems, self.wall_clock(),
                                               account_id=scope.account_id)
            if not change.halted:
                self._emit(events.LabUnlockRehearsed(slot=slot, price=price, gems=gems))
            self._publish_change(change)
            return False
        self._skip(slot, 'waiting_for_canary')
        return False

    def _tap_unlock(self, locked: LockedSlot, home: LabHomeReading, screen: Image,
                    device: AdbDevice) -> bool:
        txn = self._prepare('lab_unlock', home.gem_balance, locked.price, unlock_slot=locked.slot)
        if txn is None:
            self.recovery_status = 'lab_preparation_refused'
            return False
        self._unlock_tap = (txn.key, locked.slot)
        self._unlock_frames = [screen]
        self._unlock_scans = self._unlock_strikes = 0
        self._unlanded_signature = None
        self._tap(device, locked.point, f"unlock_lab_slot_{locked.slot}")
        self._state = "confirm_slot"
        return True

    def _unlock_evidence(self, txn: transactions.Transaction, home: LabHomeReading,
                         scope: FactScope, changed: bool) -> transactions.RecoveryEvidence:
        return transactions.RecoveryEvidence(category='LABS', currency='gems',
            wallet_after=home.gem_balance, effect_changed=changed, observed_at=self._capture_at,
            frame_digest=self._reading.frame_digest, scope=scope, operation='lab_unlock',
            slot=txn.before['slot'])

    def _settle_own_unlock(self, txn: transactions.Transaction, home: LabHomeReading,
                           screen: Image) -> LabVisitResult | None:
        """Sort this visit's own unlock tap into bought, not landed, or uncertain."""
        key, slot = self._unlock_tap
        if len(self._unlock_frames) < _UNLOCK_SCANS + 1:
            self._unlock_frames.append(screen)
        self._unlock_scans += 1
        scope, reading = self._scope(), self._reading
        strip = (scope is not None and reading is not None and home.page and reading.strip_read())
        if strip and reading.slots_owned >= slot:
            snapshot = self.runtime.snapshot()
            record = snapshot.slots[slot - 1]
            current = (record.confirmed and record.observed_at == self._capture_at
                       and record.started_observed_at is not None and txn.acted_at is not None
                       and record.started_observed_at > txn.acted_at
                       and snapshot.slots_owned is not None and snapshot.slots_owned >= slot)
            if current and home.gem_balance is not None:
                outcome = self.journal.reconcile(key, self._unlock_evidence(txn, home, scope, True),
                                                 now=self.wall_clock())
                if outcome.verdict == transactions.Verdict.BOUGHT and outcome.spent == txn.price:
                    return self._unlock_bought(txn, home, outcome)
                return self._unlock_uncertain(txn, slot, 'gem debit did not match the price')
        elif (strip and reading.slots_owned == slot - 1 and home.next_locked is not None
              and home.next_locked.slot == slot and home.gem_balance == txn.wallet_before):
            signature = (reading.slots_owned, home.next_locked, home.gem_balance)
            if signature == self._unlanded_signature:
                outcome = self.journal.refute_unlanded_unlock(
                    key, self._unlock_evidence(txn, home, scope, False), now=self.wall_clock())
                if outcome.verdict == transactions.Verdict.REFUTED:
                    return self._unlock_missed(txn, home)
            self._unlanded_signature = signature
        else:
            self._unlock_strikes += 1
            self._unlanded_signature = None
        if self._unlock_strikes >= _UNLOCK_STRIKES or self._unlock_scans >= _UNLOCK_SCANS:
            return self._unlock_uncertain(txn, slot, 'post-tap screen was not understood')
        return None

    def _unlock_bought(self, txn: transactions.Transaction, home: LabHomeReading,
                       outcome: transactions.Outcome) -> None:
        slot = txn.before['slot']
        self._restore_receipts()  # publishes LabSlotUnlocked with the journal's values
        self._publish_change(self.rollout.note_unlock(slot, self.worker, txn.key, 'bought',
                                                      at=self.wall_clock()))
        self._unlock_tap = None
        self.recovery_status = 'settled'
        self._return(LabVisitResult('observed', 'slot_unlocked', LabDecision('unknown'),
            slot_status=_slot_status(home, self._reading), gem_balance=home.gem_balance,
            gems_before=txn.wallet_before, observed_gem_spend=outcome.spent,
            unlock_transaction_key=txn.key, unlocked_slot=slot))
        return None

    def _unlock_missed(self, txn: transactions.Transaction, home: LabHomeReading) -> None:
        self._restore_receipts()
        self._publish_change(self.rollout.note_unlock(txn.before['slot'], self.worker, txn.key,
                                                      'not_charged', at=self.wall_clock()))
        self._unlock_tap = None
        self._return(LabVisitResult('observed', 'unlock_not_landed', LabDecision('unknown'),
            slot_status=_slot_status(home, self._reading), gem_balance=home.gem_balance))
        return None

    def _save_unlock_evidence(self, slot: int, txn: transactions.Transaction) -> tuple[str, ...]:
        if self.evidence_dir is None:
            return ()
        import cv2
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(txn.acted_at if txn.acted_at is not None else self.wall_clock())
        saved = []
        for index, image in enumerate(self._unlock_frames):
            path = self.evidence_dir / f"lab-unlock-slot{slot}-{stamp}-{index}.png"
            if cv2.imwrite(str(path), image):
                saved.append(str(path))
        return tuple(saved)

    def _unlock_uncertain(self, txn: transactions.Transaction, slot: int,
                          reason: str) -> LabVisitResult:
        """Keep the transaction open (the worker's read-only hold) and halt a canary's slot."""
        evidence = self._save_unlock_evidence(slot, txn)
        state = self.rollout.slot(slot)
        if state.stage == 'canary' and state.canary_worker == self.worker:
            self._publish_change(self.rollout.halt(slot, reason, evidence))
        self._unlock_tap = None
        self.recovery_status = 'lab_unlock_uncertain'
        return self._finish(LabVisitResult('failed', 'lab_unlock_uncertain', LabDecision('unknown'),
                                           gems_before=txn.wallet_before))
```

`FactScope` is already imported from `evidence_scope`.

- [ ] **Step 6: Wire the executor into `advance`, `_recover` and `_return`**

In `advance`, right after the `confirmation = self.confirmation_reader(...)` line:

```python
        if home.page:
            self._home_seen = (home, self._reading)
```

Replace the pending block with:

```python
        pending = self.pending_transaction
        if pending is not None:
            if self._unlock_tap is not None and pending.key == self._unlock_tap[0]:
                return self._settle_own_unlock(pending, home, screen)
            self._recover(pending, home, picker, confirmation, screen, device)
            return None
```

Make two more changes in `advance`:
- `if self._state in {"confirm", "confirm_slot2"}:` becomes `if self._state in {"confirm", "confirm_slot"}:`.
- In the `return` state, `if self._unlock_lab_two(home, device):` becomes `if self._unlock_slot(home, screen, device):`.

In the `home` state, delete `self._slot2_home = home`.

Make these changes in `_recover`:
- Replace `if home.slot2_status == 'owned' and self.runtime.snapshot().slots_owned >= 2:` with `if self.runtime.snapshot().slots_owned >= txn.before['slot']:`.
- In the unlock `else:` branch, replace `slot2_status='owned'` with `slot_status=_slot_status(home, self._reading), unlocked_slot=txn.before['slot']`.

Replace `_return` with:

```python
    def _return(self, outcome: LabVisitResult) -> None:
        if self._home_seen is not None:
            home, reading = self._home_seen
            if not outcome.slot_status:
                outcome = replace(outcome, slot_status=_slot_status(home, reading))
            if outcome.observed_gem_spend == 0 and outcome.gem_balance is None:
                outcome = replace(outcome, gem_balance=home.gem_balance)
        self._outcome = outcome
        self._state = "return"
```

- [ ] **Step 7: Run the executor tests**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_slot_unlock.py`
Expected: PASS.

- [ ] **Step 8: Write the failing wiring tests and migrate the old slot-2 tests**

In `tests/test_lab_execution.py`, replace `test_unlock_cannot_be_authorized_by_synthetic_post_state` (lines 118–125) with:

```python
def test_unlock_needs_a_rollout_record_and_a_worker() -> None:
    from fleet.resource_blocks import gem_automated
    visit = LabVisit(vision.TemplateCache(Path('templates')))
    visit.request(LabVisitOptions(unlock_slots=(2,)))
    home = lab_screen.LabHomeReading(True, 'idle', None, None, gem_balance=150,
        next_locked=lab_screen.LockedSlot(2, 100, (586, 906), (0, 654, 1080, 396)))
    assert not visit._unlock_slot(home, frame('menu_labs_slot1_idle'), Device())
    assert not gem_automated({'type': 'unlock_lab_slot', 'slot': 2})
```

In `tests/test_lab_visit.py`, delete `test_lab_two_unlock_respects_the_switch_and_the_gem_floor` (lines 376–385). `tests/test_lab_slot_unlock.py::test_no_rehearsal_without_the_switch_or_the_gems` covers it.

In `tests/test_lab_towerbot.py`, replace `test_authorize_refuses_unlock_while_its_route_is_uncalibrated` (lines 146–149) with:

```python
def test_authorize_unlock_follows_the_rollout(tmp_path: Path) -> None:
    from lab_unlock_rollout import LabUnlockRollout
    b = bot(None, options=LabVisitOptions(unlock_slots=(2,)))
    b.lab_visit.request(LabVisitOptions(unlock_slots=(2,)))
    unlock = LabDecision('unlock_slot', price=100, slot=2)
    assert not b._authorize_lab('lab_unlock', unlock, 1000.)  # no rollout record
    b.lab_visit.rollout, b.lab_visit.worker = LabUnlockRollout(tmp_path), 'Air_1'
    assert not b._authorize_lab('lab_unlock', unlock, 1000.)  # a dry run never taps
    b.lab_visit.rollout.note_dry_run(2, 'Air_1', 100, 150, 0., account_id='a')
    b.lab_visit.rollout.note_dry_run(2, 'Air_1', 100, 150, 700., account_id='a')
    assert b._authorize_lab('lab_unlock', unlock, 1000.)  # the canary
    assert not b._authorize_lab('lab_unlock', LabDecision('unlock_slot', price=90, slot=2), 1000.)
    assert not b._authorize_lab('lab_unlock', LabDecision('unlock_slot', price=400, slot=3), 1000.)
    b.lab_visit.worker = 'Air_2'
    assert not b._authorize_lab('lab_unlock', unlock, 1000.)
```

Append to `tests/test_shopping_loop.py`:

```python
def test_finished_lab_visit_notes_slot_ownership_without_minting_an_unlock(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    bot.reroll_progress = Mock()
    bot.lab_state = LabsState(Mock())
    bot._finish_lab_visit(LabVisitResult("observed", "slot_unlocked", LabDecision("unknown"),
        slot_status=((2, "owned"), (3, "locked")), gem_balance=50, gems_before=150,
        observed_gem_spend=100, unlocked_slot=2))
    bot.reroll_progress.note_lab_slots.assert_called_once_with({2: "owned", 3: "locked"}, 50)
    assert not [e for e in bot.bus.published if isinstance(e, events.LabSlotUnlocked)]
```

Append to `tests/test_reroll_progress.py`:

```python
def test_the_rollout_lives_at_the_fleet_root_of_a_fleet_worker(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    assert (progress.fleet_root, progress.worker_id) == (tmp_path, "Tiramisu64_20")
    assert progress.unlock_rollout().path == tmp_path / "lab-unlock-rollout.json"
    solo = tmp_path / "solo"
    solo.mkdir()
    db.bind_account(solo / "tower_bot.db", "ACCOUNT-A")
    loose = RerollProgress(solo, "ACCOUNT-A", AccountState())
    assert loose.fleet_root is None and loose.worker_id is None and loose.unlock_rollout() is None
```

Append to `tests/test_lab_transactions.py`:

```python
def test_production_constructor_wires_the_unlock_rollout(tmp_path: Path) -> None:
    from tower_bot import TowerBot
    from shopping import ShoppingSession
    from tests.conftest import _RecordingBus
    from fleet.reroll_progress import RerollProgress
    import digits
    root = tmp_path / 'workers' / 'Air_1'
    root.mkdir(parents=True)
    db.bind_account(root / 'tower_bot.db', 'account-a')
    account, journal, _ = authority(root)
    bus = _RecordingBus()
    templates = vision.TemplateCache(Path('templates'))
    shopping = ShoppingSession(templates, bus, digits.NumberReader(), journal=journal)
    bot = TowerBot(Device(), templates, bus, account_state=account, shopping=shopping,
                   reroll_progress=RerollProgress(root, 'account-a', account),
                   unknown_dir=root / 'evidence')
    assert bot.lab_visit.rollout.path == tmp_path / 'lab-unlock-rollout.json'
    assert bot.lab_visit.worker == 'Air_1'
    assert bot.lab_visit.evidence_dir == root / 'evidence'
```

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_towerbot.py tests/test_reroll_progress.py tests/test_lab_transactions.py`
Run: `uv run pytest -p no:allure_pytest -q tests/test_shopping_loop.py -k lab`
Expected: FAIL. `fleet_root` is missing, `_authorize_lab` ignores the rollout, `_finish_lab_visit` reads `slot2_status`, and TowerBot does not wire the rollout.

- [ ] **Step 9: Implement the worker wiring**

In `fleet/reroll_progress.py`, add `from lab_unlock_rollout import LabUnlockRollout` and these members:

```python
    @property
    def fleet_root(self) -> Path | None:
        """<fleet root> for a worker root laid out as <fleet root>/workers/<worker id>."""
        return self.root.parent.parent if self.root.parent.name == "workers" else None

    @property
    def worker_id(self) -> str | None:
        return self.root.name if self.fleet_root is not None else None

    def unlock_rollout(self) -> LabUnlockRollout | None:
        root = self.fleet_root
        return LabUnlockRollout(root) if root is not None else None
```

In `tower_bot.py`:
- Imports: `from lab_routes import research_gate`, `import lab_catalog` and `from lab_unlock_rollout import LabUnlockRollout`.
- After `self.shopping.evidence_dir = self.stall_dir`:

```python
        if self.lab_visit is not None:
            # The rollout and the worker id come from the fleet layout. A solo bot has neither, so it never unlocks.
            self.lab_visit.evidence_dir = Path(self.stall_dir) if self.stall_dir is not None else None
            opener = getattr(reroll_progress, 'unlock_rollout', None)
            rollout = opener() if callable(opener) else None
            self.lab_visit.rollout = rollout if isinstance(rollout, LabUnlockRollout) else None
            worker = getattr(reroll_progress, 'worker_id', None)
            self.lab_visit.worker = worker if isinstance(worker, str) else None
```

- In `_authorize_lab`, the unlock branch becomes:

```python
        if operation == 'lab_unlock':
            visit = self.lab_visit
            slot = decision.slot if decision is not None else None
            return (visit is not None and decision is not None and decision.kind == 'unlock_slot'
                    and slot in options.unlock_slots and decision.price is not None
                    and decision.price == lab_catalog.lab_slot_gems(slot)
                    and options.keep_gems == visit._options.keep_gems
                    and visit.rollout is not None and visit.worker is not None
                    and visit.rollout.may_tap(slot, visit.worker))
```

- In `_finish_lab_visit`, replace the `slot2_status` block and the hard-coded `LabSlotUnlocked(slot=2, price=100, …)` publish (lines 1441–1447) with the following. The journal's `recovery_event` already publishes `LabSlotUnlocked` with the real slot and price.

```python
        if result.slot_status:
            self.reroll_progress.note_lab_slots(dict(result.slot_status), result.gem_balance)
```

Make sure `Path` is imported in `tower_bot.py`: `grep -n "^from pathlib" tower_bot.py`.

- [ ] **Step 10: Run the tests to verify they pass**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_slot_unlock.py tests/test_lab_execution.py tests/test_lab_visit.py tests/test_lab_towerbot.py tests/test_reroll_progress.py tests/test_lab_transactions.py tests/test_lab_runtime.py`
Run: `uv run pytest -p no:allure_pytest -q tests/test_shopping_loop.py -k lab`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add lab_visit.py fleet/reroll_progress.py tower_bot.py tests/test_lab_slot_unlock.py tests/test_lab_execution.py \
  tests/test_lab_visit.py tests/test_lab_towerbot.py tests/test_shopping_loop.py tests/test_reroll_progress.py \
  tests/test_lab_transactions.py
git commit -m "Unlock lab slots through the rollout: rehearse, canary tap, three checked outcomes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Retire `unlock_gate`; the gem lane and Fleet State follow the rollout

**Files:**
- Modify: `lab_routes.py`:
  - delete `_UNLOCK_STAGES` (line 49), `_validate_unlock` (lines 241–271), `load_recorded_unlocks`, `recorded_unlocks` and `unlock_gate`;
  - simplify `_load`, `_recorded` and `route_gates`.
- Modify: `fleet/resource_blocks.py`: `gem_automated` (lines 47–49), `LabFacts` (add `rollout` and `worker`), and `_gem_plan` (lines 788–802)
- Modify: `fleet/build_route_eval.py`, in these places:
  - `RouteFacts` (add `lab_slot_status`);
  - replace `evaluate_resources`'s gem block (lines 139–159) and delete `_future_gem_action` (lines 116–122);
  - drop `import lab_routes` if nothing else uses it.
- Modify: `fleet/reroll_progress.py`: `resource_evaluation` (lines 189–213) and `lab_strategy_plan`
- Modify: `fleet/build_route_preview_facts.py`: `_reconstructed_resources` (lines 118–140)
- Modify: `fleet/setup.py`: the preview's `evaluate_resources` calls (lines 497–498)
- Modify: `fleet/labs_view.py`: `labs_snapshot` and `_row` pass the rollout into the facts
- Test: `tests/test_build_route_resources.py`, `tests/test_resource_blocks.py`, `tests/test_lab_execution.py`, `tests/test_fleet_setup.py`, `tests/test_lab_towerbot.py`, `tests/test_build_route_integration.py`, `tests/test_labs_view.py`

**Interfaces:**
- **Consumes:**
  - `SlotRollout`, `rollout_status` and `LabUnlockRollout.slots(quarantine=…)` (Task 2);
  - `_gem_met` and `gem_lane_blocks` (Task 4);
  - `RerollProgress.unlock_rollout()` and `.worker_id` (Task 7).
- **Produces:**
  - `gem_automated(block, rollout: Mapping[int, SlotRollout] | None = None) -> bool`. It is true only for an `unlock_lab_slot` with a catalog price whose stage is `canary` or `fleet`.
  - `LabFacts.rollout: Mapping[int, Any] | None = None` and `LabFacts.worker: str | None = None`.
  - `RouteFacts.lab_slot_status: Mapping[str, str]`, keyed `"2"`–`"5"` so the facts snapshot round-trips as JSON.
  - `evaluate_resources(route, facts, rollout: Mapping[int, Any] | None = None) -> ResourceEvaluation`.
  - `lab_routes` no longer exports `unlock_gate`, `recorded_unlocks` or `load_recorded_unlocks`. `route_gates()` has no `unlock_slot_*` keys.

- [ ] **Step 1: Write the failing tests**

In `tests/test_build_route_resources.py`:
- Delete the `calibrated_unlock` fixture and every `@pytest.mark.usefixtures("calibrated_unlock")`.
- Replace `test_uncalibrated_second_lab_unlock_is_only_planned_at_any_balance` with the tests below.
- Add these imports: `import lab_catalog` and `from lab_unlock_rollout import DryRun, SlotRollout`.

```python
def test_gem_step_follows_the_rollout_stage() -> None:
    route, facts = _route(), replace(_facts(), wallet_gems=150)
    assert evaluate_resources(route, facts).gem_step.reason == "Rehearsing slot 2 · 0/2 dry runs"
    one = SlotRollout(dry_runs=(DryRun("Air_38", 1., 100, 150, "a1"),))
    assert evaluate_resources(route, facts, {2: one}).gem_step.reason == "Rehearsing slot 2 · 1/2 dry runs"
    held = SlotRollout(stage="canary", canary_worker="Air_38")
    step = evaluate_resources(route, facts, {2: held}).gem_step
    assert (step.status, step.reason) == ("supported", "Canary: Air_38 unlocks slot 2 next visit")
    other = evaluate_resources(route, facts, {2: replace(held, canary_worker="Air_39")}).gem_step
    assert (other.status, other.reason) == ("blocked", "Waiting for canary")
    fleet = evaluate_resources(route, facts, {2: SlotRollout(stage="fleet")}).gem_step
    assert (fleet.action, fleet.status, fleet.reason) == ("unlock_lab_slot_2", "supported", "Unlocking slot 2")
    halted = SlotRollout(stage="halted", halted_reason="price 120", evidence=("a.png",))
    step = evaluate_resources(route, facts, {2: halted}).gem_step
    assert (step.status, step.reason) == ("blocked", "Halted: price 120 · 1 evidence frame(s)")
    short = evaluate_resources(route, replace(facts, wallet_gems=60), {2: SlotRollout(stage="fleet")})
    assert short.gem_step.reason == "Save 40 more gems"


def test_owned_slots_wait_for_the_lab_starter() -> None:
    step = evaluate_resources(_route(), replace(_facts(), lab_slot_status={"2": "owned"})).gem_step
    assert (step.action, step.status, step.reason) == (
        "lab_slot_2_owned", "supported", "Slot 2 owned · waiting for lab starter")
    three = evaluate_resources(_route(["unlock_lab_slot_2", "unlock_lab_slot_3"]),
        replace(_facts(), wallet_gems=500, lab_slot_status={"2": "owned", "3": "locked"})).gem_step
    assert (three.action, three.reason) == ("unlock_lab_slot_3", "Rehearsing slot 3 · 0/2 dry runs")


def test_a_slot_without_a_catalog_price_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab_catalog, "lab_slot_gems", lambda slot: None)
    step = evaluate_resources(_route(), replace(_facts(), wallet_gems=500)).gem_step
    assert (step.status, step.reason) == ("blocked", "Slot 2 price unknown")
```

In `tests/test_resource_blocks.py`:
- Replace line 485's assertion with `assert any(line == "gems.lab2: Rehearsing slot 2 · 0/2 dry runs" for line in plan.gems.why)`.
- Append:

```python
def test_gem_automation_follows_the_rollout_stage() -> None:
    from lab_unlock_rollout import SlotRollout
    block = {"type": "unlock_lab_slot", "slot": 2}
    assert not rb.gem_automated(block)
    assert not rb.gem_automated(block, {2: SlotRollout()})
    assert rb.gem_automated(block, {2: SlotRollout(stage="canary", canary_worker="Air_1")})
    assert rb.gem_automated(block, {2: SlotRollout(stage="fleet")})
    assert not rb.gem_automated(block, {2: SlotRollout(stage="halted", halted_reason="x")})
    plan = evaluate_lab_plan(template_route(), LabFacts(
        now=1000., wallet_gems=160, slot_ownership={2: {"status": "locked"}},
        rollout={2: SlotRollout(stage="halted", halted_reason="x")}, worker="Air_38"))
    assert plan.gems.automated is False and "gems.lab2: Halted: x" in plan.gems.why
```

In `tests/test_lab_execution.py`:
- Line 54 becomes `assert not any(key.startswith('unlock_slot_') for key in matrix['route_gates'])`.
- Delete everything from `def _unlock_record` (line 379) through the end of `test_unlock_gate_is_data_driven_but_shipped_manifest_enables_nothing` (line 536).
- Add:

```python
def test_unlock_records_in_the_route_manifest_enable_nothing(tmp_path: Path) -> None:
    import json
    import lab_routes
    manifest = tmp_path / 'lab-routes.v1.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'routes': [{'kind': 'unlock', 'slot': 2}]}))
    assert lab_routes.load_recorded_routes(manifest) == {}
    assert not hasattr(lab_routes, 'unlock_gate') and not hasattr(lab_routes, 'recorded_unlocks')
    assert not any(key.startswith('unlock_slot_') for key in lab_routes.route_gates())
```

In `tests/test_fleet_setup.py`, delete the `import lab_routes` and `monkeypatch.setattr(lab_routes, "unlock_gate", …)` lines (63–65). The expected `"blocked"` still holds, because 20 gems is short of 100.

In `tests/test_lab_towerbot.py`, line 113 becomes `assert not any(line.startswith('gems.lab2:') for line in b.lab_route_pending)`. A slot unlock follows the rollout, not a route calibration.

Append to `tests/test_build_route_integration.py`, and add `from lab_unlock_rollout import LabUnlockRollout` to its imports:

```python
def test_published_gem_step_names_the_canary(tmp_path: Path) -> None:
    progress = _progress(tmp_path)
    _rules_route(tmp_path, {})
    progress.note_lab_slots({2: "locked"}, 150, now=time.time())
    rollout = LabUnlockRollout(tmp_path)
    rollout.note_dry_run(2, "Air_38", 100, 150, 0., account_id="account-a")
    rollout.note_dry_run(2, "Air_38", 100, 150, 700., account_id="account-a")
    progress.resource_evaluation(500, 150)
    published = json.loads((tmp_path / "workers" / "Air_38" / "build-route-resources.json").read_text())
    assert published["gem_step"]["reason"] == "Canary: Air_38 unlocks slot 2 next visit"
    facts = json.loads((tmp_path / "workers" / "Air_38" / "build-route-resource-facts.json").read_text())
    assert facts["lab_slot_status"] == {"2": "locked"}
```

Replace the comment at `tests/test_labs_view.py:39-40` with `# Only Game Speed on slot 1 is a route; lab slot unlocks follow the rollout record.`

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_build_route_resources.py tests/test_resource_blocks.py tests/test_lab_execution.py`
Expected: FAIL. `evaluate_resources` takes no rollout, `gem_automated` takes no rollout, and `unlock_gate` still exists.

- [ ] **Step 3: Remove the unlock gate from `lab_routes.py`**

Delete `_UNLOCK_STAGES`, `_validate_unlock`, `load_recorded_unlocks`, `recorded_unlocks` and `unlock_gate`. Replace `_load`, `load_recorded_routes`, `_recorded`, `recorded_routes` and `route_gates` with:

```python
def _load(manifest: Path, evidence_root: Path | None) -> dict[tuple[int, str], RouteGate]:
    """Malformed, partial and isolated evidence cannot add execution support.

    Lab slot unlocks are never recorded routes: lab_unlock_rollout.py gates them.
    """
    try:
        document = json.loads(manifest.read_text())
        if document.get('schema_version') != 1 or not isinstance(document.get('routes'), list):
            return {}
    except (OSError, ValueError, AttributeError):
        return {}
    root = evidence_root or manifest.parent
    research: dict[tuple[int, str], RouteGate] = {}
    for record in document['routes']:
        try:
            if record['kind'] == 'unlock':
                continue
            research[_validate_record(record, root)] = RouteGate(True, 'validated_recorded_sequence',
                'Recorded semantic sequence and one durable debit verified')
        except _ERRORS:
            continue
    return research


def load_recorded_routes(manifest: Path, *, evidence_root: Path | None = None) -> dict[tuple[int, str], RouteGate]:
    return _load(manifest, evidence_root)


@lru_cache(maxsize=1)
def _recorded() -> dict[tuple[int, str], RouteGate]:
    """Calibration changes are reviewed deployment inputs; reload on restart."""
    return _load(MANIFEST, MANIFEST.parent.parent)


def recorded_routes() -> dict[tuple[int, str], RouteGate]:
    return _recorded()


def route_gates() -> dict[str, dict[str, object]]:
    return {
        'game_speed_slot_1': asdict(LEGACY_GAME_SPEED),
        'general_research': asdict(UNCALIBRATED),
        **{f'research:{slot}:{research}': asdict(gate)
           for (slot, research), gate in recorded_routes().items()},
        'native_repeat': asdict(RouteGate(False, 'missing_recorded_sequence',
            'Repeat controls and persisted on/off state are uncalibrated')),
        **{f'in_battle_{page}': asdict(RouteGate(False, 'missing_recorded_sequence',
            'Pending work waits for MAIN_MENU/GAME_OVER; no calibrated return to the same run'))
           for page in ('labs', 'missions')},
    }
```

Check that nothing else references the removed names: `git grep -nwE "unlock_gate|recorded_unlocks|load_recorded_unlocks|_validate_unlock" -- '*.py'`. The only matches allowed are the three `hasattr` strings in the new test.

- [ ] **Step 4: Make the gem lane follow the rollout in `fleet/resource_blocks.py`**

Add these two fields as the last fields of `LabFacts`:

```python
    # The fleet's lab-unlock rollout per slot, and this worker's id, for the gem lane.
    rollout: Mapping[int, Any] | None = None
    worker: str | None = None
```

Replace `gem_automated` and add `_unlock_line`:

```python
def gem_automated(block: Mapping[str, Any], rollout: Mapping[int, Any] | None = None) -> bool:
    """A slot unlock is automated once its rollout reached canary or fleet."""
    if block.get("type") != "unlock_lab_slot" or lab_catalog.lab_slot_gems(block.get("slot")) is None:
        return False
    state = (rollout or {}).get(block.get("slot"))
    return state is not None and state.stage in ("canary", "fleet")


def _unlock_line(block: Mapping[str, Any], facts: LabFacts) -> str:
    from lab_unlock_rollout import SlotRollout, rollout_status
    slot = block["slot"]
    if lab_catalog.lab_slot_gems(slot) is None:
        return f"Slot {slot} price unknown"
    return rollout_status((facts.rollout or {}).get(slot) or SlotRollout(), slot, facts.worker)
```

In `_gem_plan`, replace the `automated = …` line and the `unlock_gate` block with the following. Also change `if gem_automated(block) and not rules.gems.auto_unlock_lab_slots:` to `if gem_automated(block, facts.rollout) and not rules.gems.auto_unlock_lab_slots:`.

```python
        automated = gem_automated(block, facts.rollout) and rules.gems.auto_unlock_lab_slots
        if block["type"] == "unlock_lab_slot" and not gem_automated(block, facts.rollout):
            why.append(f"{block['id']}: {_unlock_line(block, facts)}")
```

- [ ] **Step 5: Write the Fleet State gem step in `fleet/build_route_eval.py`**

Add this field to `RouteFacts`, after `lab_slot2_owned`:

```python
    # lab-slots.json statuses keyed "2"-"5" (string keys survive the JSON facts snapshot).
    lab_slot_status: Mapping[str, str] = field(default_factory=dict)
```

Delete `_future_gem_action`. Add these helpers above `evaluate_resources` and replace its gem block:

```python
def _slot_ownership(facts: RouteFacts) -> dict[int, dict[str, str]]:
    """Slot 2-5 ownership from the worker's lab-slots record, else the legacy slot-2 flag."""
    if facts.lab_slot_status:
        return {int(slot): {"status": status} for slot, status in facts.lab_slot_status.items()
                if str(slot) in {"2", "3", "4", "5"} and status in {"owned", "locked"}}
    if facts.lab_slot2_owned is None:
        return {}
    return {2: {"status": "owned" if facts.lab_slot2_owned else "locked"}}


def _gem_action(route: EffectiveRoute, block: Mapping[str, Any]) -> str:
    if block["type"] == "unlock_lab_slot":
        return f"unlock_lab_slot_{block['slot']}"
    return block["id"].removeprefix("legacy.gems.") if route.gems.mode != "blocks" else block["type"]


def _gem_step(route: EffectiveRoute, facts: RouteFacts,
              rollout: Mapping[int, Any] | None) -> ResourceStep:
    """Fleet State's gem step: the next slot unlock and where its rollout stands."""
    from fleet.resource_blocks import _gem_met, gem_lane_blocks
    from lab_unlock_rollout import SlotRollout, rollout_status
    blocks = gem_lane_blocks(route.gems)
    ownership = _slot_ownership(facts)
    first = _gem_action(route, blocks[0]) if blocks else "unlock_lab_slot_2"
    if facts.wallet_gems is None:
        return ResourceStep(first, "unknown", "Gem balance or lab ownership unverified")
    pending = next((block for block in blocks if _gem_met(block, ownership) is not True), None)
    if pending is not None and pending["type"] == "unlock_lab_slot":
        slot, action = pending["slot"], _gem_action(route, pending)
        if _gem_met(pending, ownership) is None:
            return ResourceStep(action, "unknown", "Gem balance or lab ownership unverified")
        price = lab_catalog.lab_slot_gems(slot)
        if price is None:
            return ResourceStep(action, "blocked", f"Slot {slot} price unknown")
        if not route.rules.gems.auto_unlock_lab_slots:
            return ResourceStep(action, "planned", "Planned · auto-unlock off")
        state = (rollout or {}).get(slot) or SlotRollout()
        if state.stage == "halted":
            frames = f" · {len(state.evidence)} evidence frame(s)" if state.evidence else ""
            return ResourceStep(action, "blocked", rollout_status(state, slot, facts.worker) + frames)
        need = price + route.rules.gems.keep
        if facts.wallet_gems < need:
            return ResourceStep(action, "blocked", f"Save {need - facts.wallet_gems} more gems")
        waiting = state.stage == "canary" and state.canary_worker != facts.worker
        return ResourceStep(action, "blocked" if waiting else "supported",
                            rollout_status(state, slot, facts.worker))
    owned = max((slot for slot, record in ownership.items() if record["status"] == "owned"), default=None)
    if owned is None:
        return ResourceStep(first, "unknown", "Gem balance or lab ownership unverified")
    starter = f"Slot {owned} owned · waiting for lab starter"
    if pending is not None:
        return ResourceStep(_gem_action(route, pending), "planned", f"{starter} · planned, not automated")
    return ResourceStep(f"lab_slot_{owned}_owned", "supported", starter)


def evaluate_resources(route: EffectiveRoute, facts: RouteFacts,
                       rollout: Mapping[int, Any] | None = None) -> ResourceEvaluation:
    """Describe the gem step from the unlock rollout, and existing lab automation."""
    gem = _gem_step(route, facts, rollout)
```

The `lab` half of `evaluate_resources` stays unchanged. If `git grep -n "lab_routes" fleet/build_route_eval.py` now prints nothing, delete `import lab_routes`.

- [ ] **Step 6: Pass the slot status and the rollout from every caller**

In `fleet/reroll_progress.py` `resource_evaluation`:
- Add `lab_slot_status={str(slot): str(record["status"]) for slot, record in self.lab_cadence.slot_records().items()},` to the `RouteFacts(…)` call.
- Publish with:

```python
            rollout = self.unlock_rollout()
            self.route_runtime.publish_resources(
                evaluate_resources(resolve_route(route, self.root.name, self.account_id), facts,
                                   rollout.slots() if rollout is not None else None), facts)
```

In `lab_strategy_plan`, add the following, and pass `rollout=rollout_slots, worker=self.worker_id` in the `replace(facts, …)` call:

```python
        rollout = self.unlock_rollout()
        rollout_slots = rollout.slots() if rollout is not None else None
```

In `fleet/build_route_preview_facts.py` `_reconstructed_resources`:
- Replace `lab, slot2 = LabCadence(worker_root, account_id).route_observation()` with:

```python
    cadence = LabCadence(worker_root, account_id)
    lab, slot2 = cadence.route_observation()
```

- Add `lab_slot_status={str(slot): str(record["status"]) for slot, record in cadence.slot_records().items()},` to the `RouteFacts(…)` call.

In `fleet/setup.py`, the preview, before the `for worker_root in …` loop:

```python
        from lab_unlock_rollout import LabUnlockRollout
        unlock_rollout = LabUnlockRollout(self.root).slots(quarantine=False)
```

Then change both `evaluate_resources(…, resource_facts)` calls to `evaluate_resources(…, resource_facts, unlock_rollout)`.

In `fleet/labs_view.py`:
- Import `replace` next to `asdict`, and add `from lab_unlock_rollout import LabUnlockRollout`.
- In `labs_snapshot`, add `rollout = LabUnlockRollout(root).slots(quarantine=False)` before the loop and call `_row(root, worker, route, route_error, moment, rollout)`.
- Give `_row` a `rollout: dict[int, Any]` parameter and build its facts with:

```python
    facts = replace(persisted_lab_facts(worker_root, account_id, now=now, coins=coins, gems=gems,
                                        db_path=registration.db_path), rollout=rollout, worker=worker)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest -p no:allure_pytest -q tests/test_build_route_resources.py tests/test_resource_blocks.py tests/test_lab_execution.py tests/test_fleet_setup.py tests/test_lab_towerbot.py tests/test_build_route_integration.py tests/test_labs_view.py tests/test_build_route_api.py tests/test_labs.py`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add lab_routes.py fleet/resource_blocks.py fleet/build_route_eval.py fleet/reroll_progress.py \
  fleet/build_route_preview_facts.py fleet/setup.py fleet/labs_view.py tests/test_build_route_resources.py \
  tests/test_resource_blocks.py tests/test_lab_execution.py tests/test_fleet_setup.py tests/test_lab_towerbot.py \
  tests/test_build_route_integration.py tests/test_labs_view.py
git commit -m "Gate lab slot unlocks by the rollout; Fleet State shows each slot's stage

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Dashboard "Lab slot rollout" panel and Reset

**Files:**
- Modify: `fleet/labs_view.py`: `labs_snapshot`'s return dict
- Modify: `web/app.py`: add a route right after `/api/fleet/labs` (line ~1615)
- Modify: `web/ui/lib/labs.ts` (line 75), `web/ui/lib/api.ts` (line 106), `web/ui/app/fleet/reroll/fleetOverview.ts` (`validatedLabsSnapshot`, lines 64–83), `web/ui/app/fleet/reroll/labs/page.tsx`
- Create: `web/ui/app/fleet/reroll/labs/UnlockRolloutPanel.tsx`
- Test: `tests/test_labs_view.py`, `web/ui/app/fleet/reroll/labs/UnlockRolloutPanel.test.tsx` (create), `web/ui/app/fleet/reroll/fleetOverview.test.ts`

**Interfaces:**
- **Consumes:** `LabUnlockRollout.snapshot()`, `.reset(slot)`, `RolloutError` and `SLOTS` (Task 2).
- **Produces:**
  - `GET /api/fleet/labs` now includes `unlock_rollout: [{slot, stage, canary_worker, dry_runs, price, halted_reason, evidence}]`.
  - `POST /api/fleet/labs/unlock-rollout/{slot}/reset` answers as follows:
    - 200 with `{"unlock_rollout": rows}`;
    - 409 when the slot is not halted;
    - 422 when the slot is not 2–5;
    - 503 without a fleet.
  - The TypeScript type `UnlockRolloutRow`, `LabsSnapshot.unlock_rollout?`, and `resetLabUnlockRollout(slot)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_labs_view.py`, and add `from lab_unlock_rollout import LabUnlockRollout` to its imports:

```python
def test_labs_snapshot_lists_the_unlock_rollout(tmp_path: Path) -> None:
    _registered(tmp_path, "Air_38", "account-a")
    LabUnlockRollout(tmp_path).halt(3, "Post-tap screen was not understood", ("x.png",))
    rows = labs_snapshot(tmp_path, ["Air_38"], now=1000.)["unlock_rollout"]
    assert [(row["slot"], row["stage"]) for row in rows] == [
        (2, "dry_run"), (3, "halted"), (4, "dry_run"), (5, "dry_run")]
    assert rows[1]["evidence"] == ["x.png"] and rows[0]["price"] == 100


def test_reset_endpoint_returns_a_halted_slot_to_dry_run(tmp_path: Path) -> None:
    _registered(tmp_path, "Air_38", "account-a")
    rollout = LabUnlockRollout(tmp_path)
    rollout.halt(2, "operator check")
    response = _client(tmp_path, ("Air_38",)).post("/api/fleet/labs/unlock-rollout/2/reset")
    assert response.status_code == 200
    assert response.json()["unlock_rollout"][0]["stage"] == "dry_run"
    assert rollout.slot(2).stage == "dry_run"


def test_reset_endpoint_refuses_a_slot_that_is_not_halted(tmp_path: Path) -> None:
    _registered(tmp_path, "Air_38", "account-a")
    client = _client(tmp_path, ("Air_38",))
    assert client.post("/api/fleet/labs/unlock-rollout/3/reset").status_code == 409
    assert client.post("/api/fleet/labs/unlock-rollout/7/reset").status_code == 422
    assert LabUnlockRollout(tmp_path).slot(3).stage == "dry_run"
```

Create `web/ui/app/fleet/reroll/labs/UnlockRolloutPanel.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { UnlockRolloutPanel } from "./UnlockRolloutPanel";
import type { UnlockRolloutRow } from "@/lib/labs";

const row = (slot: number, stage: UnlockRolloutRow["stage"], extra: Partial<UnlockRolloutRow> = {}): UnlockRolloutRow =>
  ({ slot, stage, canary_worker: null, dry_runs: 0, price: 100, halted_reason: null, evidence: [], ...extra });

test("reset is offered only on a halted slot", async () => {
  const onReset = vi.fn(async () => {});
  render(<UnlockRolloutPanel onReset={onReset} rows={[
    row(2, "canary", { canary_worker: "Air_1", dry_runs: 2 }),
    row(3, "halted", { price: 400, halted_reason: "Post-tap screen was not understood",
      evidence: ["/fleet/workers/Air_1/evidence/lab-unlock-slot3-1-0.png"] })]} />);
  expect(screen.getByRole("region", { name: "Lab slot rollout" })).toBeInTheDocument();
  expect(screen.getByText("Air_1")).toBeInTheDocument();
  expect(screen.getByText("Post-tap screen was not understood")).toBeInTheDocument();
  expect(screen.getByText("lab-unlock-slot3-1-0.png")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Reset Lab 2 rollout" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reset Lab 3 rollout" }));
  await waitFor(() => expect(onReset).toHaveBeenCalledWith(3));
});

test("a refused reset is shown, not swallowed", async () => {
  const onReset = vi.fn(async () => { throw new Error("slot_not_halted"); });
  render(<UnlockRolloutPanel rows={[row(2, "halted", { halted_reason: "x" })]} onReset={onReset} />);
  fireEvent.click(screen.getByRole("button", { name: "Reset Lab 2 rollout" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("slot_not_halted");
});
```

Append to `web/ui/app/fleet/reroll/fleetOverview.test.ts`, and import `validatedLabsSnapshot` if the file does not already:

```ts
test("a labs snapshot carries a well-formed unlock rollout or is rejected", () => {
  const base = { workers: [], automated: [], reference: { labs: [], game_speed: [], lab_slots: [],
    card_slots: [], card_gems: 0, labs_unlock_wave: 40, sources: [] } };
  const rollout = [{ slot: 2, stage: "halted", canary_worker: null, dry_runs: 1, price: 100,
    halted_reason: "x", evidence: ["a.png"] }];
  expect(validatedLabsSnapshot({ ...base, unlock_rollout: rollout })?.unlock_rollout).toEqual(rollout);
  expect(validatedLabsSnapshot(base)).not.toBeNull();
  expect(validatedLabsSnapshot({ ...base, unlock_rollout: [{ ...rollout[0], stage: "armed" }] })).toBeNull();
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_labs_view.py`
Expected: FAIL with `KeyError: 'unlock_rollout'`, and 404 or 405 from the reset route.

Run: `cd web/ui && npx vitest run app/fleet/reroll/labs/UnlockRolloutPanel.test.tsx app/fleet/reroll/fleetOverview.test.ts`
Expected: FAIL. `UnlockRolloutPanel` does not exist.

- [ ] **Step 3: Implement the API**

In `fleet/labs_view.py`, `labs_snapshot`'s return becomes:

```python
    return {"workers": rows, "automated": automated_list(), "reference": lab_catalog.reference(),
            "unlock_rollout": LabUnlockRollout(root).snapshot()}
```

In `web/app.py`, directly after `fleet_labs_snapshot`, and above the write-verb catch-all:

```python
    @app.post("/api/fleet/labs/unlock-rollout/{slot}/reset")
    def fleet_lab_unlock_reset(slot: int) -> dict[str, Any]:
        """The owner's one rollout action: put a halted slot back to dry run."""
        from lab_unlock_rollout import SLOTS, LabUnlockRollout, RolloutError
        root = _fleet_root()
        if fleet is None or root is None:
            raise HTTPException(status_code=503, detail="fleet_labs_unavailable")
        if slot not in SLOTS:
            raise HTTPException(status_code=422, detail="lab slot must be 2-5")
        rollout = LabUnlockRollout(root)
        try:
            rollout.reset(slot)
        except RolloutError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"unlock_rollout": rollout.snapshot()}
```

- [ ] **Step 4: Implement the UI**

In `web/ui/lib/labs.ts`, replace the `LabsSnapshot` type with:

```ts
export type UnlockStage = "dry_run" | "canary" | "fleet" | "halted";
export type UnlockRolloutRow = { slot: number; stage: UnlockStage; canary_worker: string | null;
  dry_runs: number; price: number | null; halted_reason: string | null; evidence: string[] };
export type LabsSnapshot = { workers: LabsRow[]; automated: AutomatedBlock[]; reference: LabsReference;
  unlock_rollout?: UnlockRolloutRow[] };
```

In `web/ui/lib/api.ts`, add `UnlockRolloutRow` to the `@/lib/labs` (or `./labs`) type import, then add:

```ts
export const resetLabUnlockRollout = (slot: number) =>
  send<{ unlock_rollout: UnlockRolloutRow[] }>(`/api/fleet/labs/unlock-rollout/${slot}/reset`, "POST", undefined, "fleet");
```

In `web/ui/app/fleet/reroll/fleetOverview.ts`, add this helper above `validatedLabsSnapshot`:

```ts
const UNLOCK_STAGES = ["dry_run", "canary", "fleet", "halted"];

function validRolloutRow(item: unknown): boolean {
  return record(item) && Number.isInteger(item.slot) && UNLOCK_STAGES.includes(item.stage as string) &&
    (item.canary_worker === null || typeof item.canary_worker === "string") && Number.isInteger(item.dry_runs) &&
    nullableNumber(item.price) && (item.halted_reason === null || typeof item.halted_reason === "string") &&
    Array.isArray(item.evidence) && item.evidence.every(path => typeof path === "string");
}
```

Insert this before the duplicate-worker check in `validatedLabsSnapshot`:

```ts
  if (value.unlock_rollout !== undefined &&
      (!Array.isArray(value.unlock_rollout) || !value.unlock_rollout.every(validRolloutRow))) return null;
```

Create `web/ui/app/fleet/reroll/labs/UnlockRolloutPanel.tsx`:

```tsx
"use client";

import { useState } from "react";
import type { UnlockRolloutRow } from "@/lib/labs";

const STAGE_LABEL: Record<UnlockRolloutRow["stage"], string> = {
  dry_run: "Dry run", canary: "Canary", fleet: "Fleet", halted: "Halted",
};

/** One row per lab slot 2-5: where the bot's own unlock stands. Reset only on a halted slot. */
export function UnlockRolloutPanel({ rows, onReset }: {
  rows: UnlockRolloutRow[];
  onReset: (slot: number) => Promise<void>;
}): React.JSX.Element {
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reset = async (slot: number): Promise<void> => {
    setBusy(slot);
    setError(null);
    try {
      await onReset(slot);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Reset failed");
    } finally {
      setBusy(null);
    }
  };
  return <section aria-label="Lab slot rollout" className="rounded-xl border border-border p-4">
    <h2 className="text-lg font-semibold">Lab slot rollout</h2>
    <p className="mt-1 text-sm text-muted-foreground">Each slot unlock is rehearsed, proven once by a canary, then used by the fleet. A halted slot waits for you.</p>
    {error && <p role="alert" className="mt-2 text-danger">Reset failed: {error}</p>}
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-sm">
        <thead><tr className="text-left text-muted-foreground">
          <th className="py-1 pr-3">Slot</th><th className="pr-3">Stage</th><th className="pr-3">Canary</th>
          <th className="pr-3">Dry runs</th><th className="pr-3">Evidence</th><th><span className="sr-only">Action</span></th>
        </tr></thead>
        <tbody>{rows.map(row => <tr key={row.slot} className="border-t border-border align-top">
          <td className="py-2 pr-3">Lab {row.slot}{row.price !== null ? ` · ${row.price} gems` : " · price unknown"}</td>
          <td className="pr-3">{STAGE_LABEL[row.stage]}{row.halted_reason && <p className="text-danger">{row.halted_reason}</p>}</td>
          <td className="pr-3">{row.canary_worker ?? "—"}</td>
          <td className="pr-3">{row.dry_runs}</td>
          <td className="pr-3">{row.evidence.length
            ? <ul>{row.evidence.map(path => <li key={path} className="font-mono text-xs">{path.split("/").pop()}</li>)}</ul>
            : "—"}</td>
          <td>{row.stage === "halted" && <button type="button" disabled={busy !== null}
            onClick={() => void reset(row.slot)} aria-label={`Reset Lab ${row.slot} rollout`}
            className="min-h-11 rounded-md border border-border px-3 text-xs font-medium">Reset</button>}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </section>;
}
```

In `web/ui/app/fleet/reroll/labs/page.tsx`:
- Import `resetLabUnlockRollout` from `@/lib/api` and `UnlockRolloutPanel` from `./UnlockRolloutPanel`.
- In `LabsContent`, destructure `refresh` from `useFleetLabs()` and add:

```tsx
  const reset = async (slot: number): Promise<void> => { await resetLabUnlockRollout(slot); await refresh(); };
```

- Render `{snapshot?.unlock_rollout && <UnlockRolloutPanel rows={snapshot.unlock_rollout} onReset={reset} />}` directly after the header.
- Change the header sentence "Only Game Speed in Lab 1 and the Lab 2 unlock are automated today." to "Only Game Speed in Lab 1 is automated; lab slot unlocks follow the rollout below."

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -p no:allure_pytest -q tests/test_labs_view.py tests/test_lifecycle_api.py -k "labs or catch_all"`
Run: `cd web/ui && npx vitest run app/fleet/reroll/labs/UnlockRolloutPanel.test.tsx app/fleet/reroll/fleetOverview.test.ts app/fleet/reroll/labs/page.test.tsx`
Expected: PASS. `tests/test_lifecycle_api.py` pins that new routes sit above the catch-all.

- [ ] **Step 6: Commit**

```bash
git add fleet/labs_view.py web/app.py web/ui/lib/labs.ts web/ui/lib/api.ts web/ui/app/fleet/reroll/fleetOverview.ts \
  web/ui/app/fleet/reroll/fleetOverview.test.ts web/ui/app/fleet/reroll/labs/UnlockRolloutPanel.tsx \
  web/ui/app/fleet/reroll/labs/UnlockRolloutPanel.test.tsx web/ui/app/fleet/reroll/labs/page.tsx tests/test_labs_view.py
git commit -m "Show the lab slot rollout on the Labs page with Reset for halted slots

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Remove the remaining slot-2 names

**Files:**
- Modify: `lab_screen.py`: drop `slot2_status`, `slot2_price` and `slot2_point` from `LabHomeReading`, and drop their computation in `read_home`
- Modify: `lab_plan.py`: drop `LAB2_GEMS` and the `slot2_owned`, `slot2_due` and `note_slot2` wrappers. `route_observation` and the legacy `slot2_path` read stay.
- Modify: `fleet/reroll_progress.py`: in `shopping_policy`, `self.lab_cadence.slot2_owned()` becomes `self.lab_cadence.slot_owned(2)`. Delete `note_lab_slot2`.
- Test: `tests/test_lab_visit.py:340-341`, `tests/test_lab_plan.py:212-225,240-249`, `tests/test_labs_view.py:65,150`, `tests/test_build_route_integration.py:383,406,441,453`, `tests/test_reroll_progress.py:42,96`, `tests/test_strategy_blocks.py:333`

**Interfaces:**
- **Consumes:** `LabHomeReading.next_locked` (Task 1), `LabCadence.slot_*` and `note_slots` (Task 4), and `RerollProgress.note_lab_slots` (Task 4).
- **Produces:** no new names. After this task, `git grep -nwE "slot2_status|slot2_price|slot2_point|unlock_slot2|min_gems|LAB2_GEMS|note_slot2|slot2_due|slot2_owned|note_lab_slot2|unlock_gate|_validate_unlock|recorded_unlocks|_unlock_lab_two|confirm_slot2" -- '*.py' '*.ts' '*.tsx'` prints nothing, except the three `hasattr` strings in `tests/test_lab_execution.py`.

- [ ] **Step 1: Migrate the tests first, so they fail against the old names**

Make the following test changes:

- **`tests/test_lab_visit.py`**, lines 340–341 become:

```python
    assert read_home(image, locked).next_locked.price == 100
    owned_home = read_home(image, owned)
    assert owned_home.next_locked.slot == 3 and owned_home.slots_owned == 2
```

- **`tests/test_lab_plan.py`**:
  - Rename `test_slot_two_reservation_is_account_bound` and rewrite its body with `slot_due(2, …, min_gems=100)`, `note_slots({2: status}, gems, now)` and `slot_owned(2)`. Keep the same numbers.
  - In `test_slot_two_check_honours_a_raised_gem_floor`, use `slot_due(2, 1100., wallet_gems=…, min_gems=…)`. Delete the `LAB2_GEMS == 100` assertion and drop `LAB2_GEMS` from the import.
- **`tests/test_labs_view.py`**:
  - `cadence.note_slot2("locked", 60, 900.)` becomes `cadence.note_slots({2: "locked"}, 60, 900.)`.
  - `LabCadence(root, "account-b").note_slot2("owned", 60, 900.)` becomes `LabCadence(root, "account-b").note_slots({2: "owned"}, 60, 900.)`.
- **`tests/test_build_route_integration.py` and `tests/test_reroll_progress.py`.** Every `progress.note_lab_slot2(STATUS, GEMS[, now=T])` becomes `progress.note_lab_slots({2: STATUS}, GEMS[, now=T])`.
- **`tests/test_strategy_blocks.py:333`.** `progress.lab_cadence.slot2_owned = lambda: True` becomes `progress.lab_cadence.slot_owned = lambda slot: True`.

The rewritten `tests/test_lab_plan.py` test:

```python
def test_slot_two_record_is_account_bound(tmp_path: Path) -> None:
    from lab_plan import LabCadence

    cadence = LabCadence(tmp_path, "ACCOUNT-A")
    assert cadence.slot_due(2, 1000.)
    cadence.note_slots({2: "locked"}, 65, 1000.)
    assert not LabCadence(tmp_path, "ACCOUNT-A").slot_due(2, 1100., wallet_gems=99, min_gems=100)
    assert LabCadence(tmp_path, "ACCOUNT-A").slot_due(2, 1100., wallet_gems=100, min_gems=100)
    cadence.note_slots({2: "locked"}, 100, 1150.)
    assert not cadence.slot_due(2, 1200., wallet_gems=100, min_gems=100)
    assert LabCadence(tmp_path, "ACCOUNT-B").slot_due(2, 1100.)
    cadence.note_slots({2: "owned"}, 19, 1200.)
    assert cadence.slot_owned(2)
    assert not cadence.slot_due(2, 100_000.)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_visit.py tests/test_lab_plan.py tests/test_strategy_blocks.py`
Expected: FAIL. `tests/test_strategy_blocks.py`'s stub is not used, because `shopping_policy` still calls `slot2_owned`. The other two files are expected to PASS already, because the new names exist.

- [ ] **Step 3: Remove the old names**

In `lab_screen.py`, delete the three `slot2_*` fields from `LabHomeReading`. In `read_home`:
- Keep `second_locked`, `lab2_labels`, `unlock_labels`, `third_labels` and `third_unlocks`.
- Delete the `slot2_status/slot2_price/slot2_point` locals, and the whole `if len(lab2_labels) == len(unlock_labels) == 1:` / `elif …` block.
- Replace that block with:

```python
    if (not len(lab2_labels) == len(unlock_labels) == 1
            and len(lab2_labels) == len(third_labels) == len(third_unlocks) == 1):
        slots_owned = 2
```

- Drop the three trailing `slot2_status, slot2_price, slot2_point` arguments from each `LabHomeReading(True, …)` return, and keep `next_locked=next_locked`.

In `lab_plan.py`:
- Delete `LAB2_GEMS` and its comment.
- Delete `slot2_owned`, `slot2_due` and `note_slot2`.

In `fleet/reroll_progress.py`:
- In `shopping_policy`, change `if not self.lab_cadence.slot2_owned():` to `if not self.lab_cadence.slot_owned(2):` and keep the comment.
- Delete `note_lab_slot2`.

- [ ] **Step 4: Run the audit grep and the tests**

Run: `git grep -nwE "slot2_status|slot2_price|slot2_point|unlock_slot2|min_gems|LAB2_GEMS|note_slot2|slot2_due|slot2_owned|note_lab_slot2|unlock_gate|_validate_unlock|recorded_unlocks|_unlock_lab_two|confirm_slot2" -- '*.py' '*.ts' '*.tsx'`
Expected: only the `hasattr(lab_routes, 'unlock_gate')` and `hasattr(lab_routes, 'recorded_unlocks')` lines in `tests/test_lab_execution.py`.

Run: `uv run pytest -p no:allure_pytest -q tests/test_lab_screen.py tests/test_lab_visit.py tests/test_lab_plan.py tests/test_labs_view.py tests/test_build_route_integration.py tests/test_reroll_progress.py tests/test_strategy_blocks.py tests/test_lab_slot_unlock.py tests/test_lab_execution.py tests/test_lab_runtime.py`
Expected: PASS. If a failure looks unrelated, reproduce it on `origin/main` in a separate worktree before calling it pre-existing.

- [ ] **Step 5: Commit**

```bash
git add lab_screen.py lab_plan.py fleet/reroll_progress.py tests/test_lab_visit.py tests/test_lab_plan.py \
  tests/test_labs_view.py tests/test_build_route_integration.py tests/test_reroll_progress.py tests/test_strategy_blocks.py
git commit -m "Remove the slot-2-only lab names now that every slot shares one path

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Spec coverage

| Spec section | Task |
|---|---|
| 1. Locked-slot reader, `next_locked`, `None` = all owned | 1 (the `slot2_*` fields are removed in 10) |
| 2. Rollout record: file, lock, operations, CAS, corrupt/missing | 2 |
| Replaces `unlock_gate` in `lab_visit` | 7 |
| Replaces `unlock_gate` in `_authorize_lab` | 6 (interim), 7 |
| Replaces `unlock_gate` in `gem_automated` and `evaluate_resources` | 8 |
| `unlock_gate` and `_validate_unlock` removed; `research_gate` and the manifest kept | 8 |
| 3. Executor: conditions, rehearsal, tap via `_prepare`, `confirm_slot`, `LabSlotUnlocked` real values | 7 (journal support in 5) |
| 4. `lab-slots.json`, `slot_due`/`slot_owned`, legacy read-once, `unlock_slots`, `_gem_met` for every slot | 4, 6 |
| Lifecycle: 2 rehearsals / 10 min / same price / same worker; price mismatch halts | 2, 7 |
| Canary: three outcomes; a second miss halts; evidence frames | 5, 7 |
| Canary: `release_canary` | 2, 7 |
| Fleet: an uncertain result holds only that worker | 7 (`_unlock_uncertain` halts only a canary) |
| Halted: nobody taps; Reset | 2, 7, 9 |
| Failure table: two writers, missing/corrupt, no price, restart mid-visit, auto-unlock off, unreadable post-tap | 2, 7, 8 |
| Visibility: Fleet State strings | 8 |
| Visibility: events | 3, 7 |
| Visibility: dashboard panel | 9 |
| Tests: reader, rollout, executor, wiring, migrated slot-2 tests | 1, 2, 7, 8, 10 |
