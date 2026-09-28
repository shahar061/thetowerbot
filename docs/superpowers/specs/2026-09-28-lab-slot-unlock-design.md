# Lab slot unlock: design

Date: 2026-09-28
Program: Lab strategy planner. This spec covers sub-project 2, milestone 1 of 2.

## Goal

The bot unlocks lab slots 2–5 with gems by itself. No slot is unlocked by hand, and no
recorded route session is needed. Slot 2 ships first. Slots 3–5 use the same code and
switch on one at a time, each after its own checked rollout.

Sub-project 2 has two milestones, built in this order:

1. **Slot unlock** (this spec).
2. **General lab starter**: OCR the picker, check the confirmation, Auto Research, and
   replace the per-lab research route gate. It gets its own spec.

## Why

- Every account holds its gems with slot 2 locked. `lab_routes.unlock_gate(2)` needs a
  recorded, human-reviewed session in `catalog/lab-routes.v1.json`, and that manifest has
  no routes.
- No tool records such a session. The receipt it needs can only come from the bot's own
  spend path, and that path is behind the gate.
- The owner's lab plan pins Labs Speed to slot 2, so the lab plan needs slot 2 unlocked.

## Decisions

| Decision | Choice |
|---|---|
| Order | One spec, two milestones; the unlock comes first |
| Safety model | A per-slot rollout: dry run → canary → fleet. Each step is promoted automatically, only after a checked success. Anything unexpected halts the slot and waits for the owner |
| Slot after unlock | Left empty until milestone 2. Fleet State shows "Slot N owned · waiting for lab starter" |
| Gate | A shared rollout record replaces `unlock_gate` for slot unlocks. The recorded-route manifest stays for research routes only |

## What exists today

- `lab_visit._unlock_lab_two` already:
  - takes two matching reads;
  - records the purchase in the journal first (`_prepare('lab_unlock')`);
  - taps;
  - proves the effect from "slot owned and `slots_owned` went up".
  
  If the result is uncertain, the worker is held in read-only inspection
  (`tower_bot.py` lab reconciliation).
- Slot 2 is hard-coded in all of these:
  - the price of exactly 100;
  - `LabVisitOptions.unlock_slot2`;
  - `LabSlotUnlocked(slot=2, price=100)`;
  - `lab-slot2-cadence.json`;
  - `_gem_met`;
  - `_validate_unlock`.
- `lab_screen.read_home` reads only the "Unlock 2nd lab" tile's price and point.
  `read_slots` already recognises "Unlock 3rd/4th/5th lab" headers, but only to count
  owned slots.
- `lab_catalog.lab_slot_gems` holds the prices: 100 / 400 / 1400 / 3000 for slots 2–5.
- Nobody knows what the game shows after a locked slot is tapped: an immediate unlock or
  a confirmation dialog. No fixture captures it. The dry run cannot find out because it
  never taps. The canary's first tap will.

## Components

### 1. Locked-slot reader (`lab_screen.py`)

`read_next_locked(image, boxes) -> LockedSlot | None`, where
`LockedSlot(slot: int, price: int | None, point: tuple[int, int] | None)`.

- It reads the first "Unlock Nth lab" tile, for N from 2 to 5. Slots unlock in order (the
  gem lane requires ascending slots), so only the first locked tile matters.
- It generalises today's slot-2 logic. The price is the single number inside the tile,
  below its unlock label. The point is the centre of that price box.
- `LabHomeReading` exposes `next_locked: LockedSlot | None` in place of `slot2_status`,
  `slot2_price` and `slot2_point`. `None` with a complete strip read means every slot is
  owned.

### 2. Rollout record (new `lab_unlock_rollout.py`)

One file shared by the fleet, `<fleet root>/lab-unlock-rollout.json`, read and written
under the fleet's existing file-lock pattern.

```json
{
  "schema_version": 1,
  "slots": {
    "2": {
      "stage": "canary",
      "canary_worker": "Tiramisu64_60",
      "dry_runs": [
        {"worker": "Tiramisu64_60", "at": 1790620000.0, "price": 100, "gems": 103},
        {"worker": "Tiramisu64_60", "at": 1790620900.0, "price": 100, "gems": 104}
      ],
      "unlock": null,
      "halted_reason": null,
      "evidence": []
    }
  }
}
```

- `stage` is one of `dry_run`, `canary`, `fleet` or `halted`. A slot with no entry is
  `dry_run`.
- `may_tap(slot, worker) -> bool` is true only when the stage is `fleet`, or when the
  stage is `canary` and `worker == canary_worker`.
- The other operations are:
  - `note_dry_run(slot, worker, price, gems, at)`;
  - `note_unlock(slot, worker, transaction_key, outcome)`;
  - `halt(slot, reason, evidence)`;
  - `release_canary(slot)`;
  - `reset(slot)`, the owner action on a halted slot.
- Every change is a read-modify-write under the lock. Every promotion is a compare-and-set
  on the stage it expects: "canary → fleet only if the stage is still canary with the
  same `canary_worker`". A lost race re-reads.
- A missing file means every slot is in `dry_run`. A corrupt file is renamed aside
  (`.corrupt-<ts>`), logged, and then also treated as all `dry_run`. It is never silently
  overwritten.

The rollout record replaces `unlock_gate` at each of these checks:

- `lab_visit`: the rehearsal and tap decision;
- `tower_bot._authorize_lab`: the `lab_unlock` operation;
- `fleet/resource_blocks.gem_automated`: the gem lane's "automated" flag;
- `fleet/build_route_eval.evaluate_resources`: Fleet State's gem step (replacing the
  "Planning only · not calibrated" check from PR #218).

`lab_routes.unlock_gate` and `_validate_unlock` are removed. `research_gate` and the
manifest stay unchanged.

### 3. Executor (`lab_visit.py`)

`_unlock_lab_two` becomes `_unlock_slot(locked: LockedSlot)`. It runs on the way out of a
Labs visit, as today, when all of these hold:

- the route has `auto_unlock_lab_slots` on;
- the account's gem lane's next step is `unlock_lab_slot` for `locked.slot`;
- `locked.price == lab_catalog.lab_slot_gems(locked.slot)`;
- the gem balance is at least `price + keep`;
- `locked.point` lies inside the tile;
- the strip is fully read, with `slots_owned == locked.slot - 1`;
- two consecutive reads match.

What it then does depends on the rollout record:

- **`may_tap` is false and the stage is `dry_run`:** a rehearsal. It calls `note_dry_run`,
  publishes `LabUnlockRehearsed`, and taps nothing. It does not prepare a transaction.
- **`may_tap` is true:** it runs today's path with the slot as a parameter:
  `_prepare('lab_unlock', slot=N, price=P)`, then one tap, then the `confirm_slot`
  state.
- **Anything else** (a halted slot, or a canary stage held by another worker): it does
  nothing and publishes `Skipped` with the reason.

`LabSlotUnlocked(slot, price)` carries the real values. The ledger records the gem debit
the same way it does today.

### 4. Per-account slot state

- `lab-slot2-cadence.json` becomes `lab-slots.json`: `{slot: {status, wallet_gems,
  observed_at}}`.
- `slot2_due` and `slot2_owned` become `slot_due(n, ...)` and `slot_owned(n)`.
- An existing `lab-slot2-cadence.json` is read once as slot 2's entry.
- `LabVisitOptions.unlock_slot2` becomes `unlock_slots`. The route rule
  `auto_unlock_lab_slots` still switches the whole thing off per route.
- `_gem_met` knows every slot from the slot state and the strip read.

## Rollout lifecycle

### Dry run → canary

A worker rehearses slot N only when:

- its gem lane's next step is `unlock_lab_slot` for N;
- its route has auto-unlock on;
- its wallet holds at least the price plus `keep`.

A rehearsal counts as **clean** when:

- the first locked tile is N;
- its price equals the catalog price;
- the gems cover the price plus `keep`;
- the point lies inside the tile;
- two consecutive reads in the same visit match.

The slot is promoted **dry run → canary** after **2 clean rehearsals on the same worker,
from separate visits at least 10 minutes apart, with the same price**. That worker
becomes `canary_worker`.

A rehearsal whose price disagrees with the catalog **halts** the slot, because it means our
model of the screen is wrong.

### Canary → fleet

Only `canary_worker` taps. The post-tap reads have three outcomes:

1. **Slot N owned, `slots_owned` up by one, gems down by exactly the price.** The
   transaction resolves as bought. The slot is promoted **canary → fleet** and the
   transaction key is recorded.
2. **Still Labs home, slot N still locked, gems unchanged.** The tap did not land. The
   transaction resolves as not charged, the same way shopping handles a missed tap. The
   canary may try again on a later visit. A second miss halts the slot.
3. **Anything else**, for example a gem confirmation dialog, an unreadable page, a partial
   change, or a debit that does not match. The slot is halted. The transaction stays
   open, so the canary stays in today's read-only inspection hold. The frames from the
   tap onward are saved to the canary worker's
   `evidence/lab-unlock-slot<N>-<ts>-<k>.png` and listed in the record's `evidence`.

If the canary worker disappears (retired, or reassigned to another account) before
tapping, `release_canary` puts the slot back to `dry_run`.

### Fleet

Every eligible worker unlocks slot N through the same checked path. An uncertain result
holds only that worker, as today. It does not halt the slot, because the canary already
proved the flow.

### Halted

Nobody taps a halted slot. Fleet State shows the reason and the evidence. **Reset** puts
the slot back to `dry_run`.

A held canary is released in one of two ways:

- **The unlock did happen:** the next visit that reads slot N as owned settles the
  transaction as bought. The owner does nothing.
- **The owner has checked the account and confirmed no gems were spent:** the owner runs
  `tools/reconcile_transaction.py` to settle it as not charged. That tool writes no ledger
  line, so it must not be used when the gems may have been spent.

## Failure handling

| Case | Handling |
|---|---|
| Rollout file written by two workers at once | A file lock plus compare-and-set promotions. A lost race re-reads |
| Rollout file missing or corrupt | Every slot is `dry_run`, so nothing taps. A corrupt file is renamed aside and logged |
| No catalog price for slot N | No rehearsal and no tap. Fleet State shows "price unknown" |
| Fleet restart mid-visit | The journal survives restarts. An open unlock transaction settles against the next Labs read, through today's recovery path, which now knows slot N |
| Route turns auto-unlock off | That route's workers stop rehearsing and tapping. The rollout stage is unchanged |
| Post-tap screen the bot cannot read | Halt the slot, hold the canary, and keep the evidence. A dialog is the expected case, and its frames become a new reader |

## Visibility

- **Fleet State gem step,** for a worker whose next gem step is slot N:
  - "Rehearsing slot N · k/2 dry runs";
  - "Canary: <worker> unlocks slot N next visit" on the canary, "Waiting for canary"
    elsewhere;
  - "Unlocking slot N" at fleet stage, or "Save X more gems" when short;
  - "Halted: <reason>" with the evidence.
- **After the unlock:** "Slot N owned · waiting for lab starter" until milestone 2.
- **Fleet dashboard, "Lab slot rollout" panel:** one row per slot 2–5 with the stage, the
  canary, the dry-run count and the evidence. **Reset** is shown on halted slots only.
- **Events:** `LabUnlockRehearsed(slot, price, gems)`, `LabUnlockPromoted(slot, stage)`,
  `LabUnlockHalted(slot, reason)`, and `LabSlotUnlocked(slot, price)` with real values.

## Out of scope

- Starting research on a newly unlocked slot (milestone 2).
- A reader for a post-tap confirmation dialog. It is written only if the canary halts on
  one, as a small follow-up that uses its evidence frames.
- Editing the rollout from the dashboard beyond **Reset**.
- Unlocks bought with anything other than gems.

## Testing

Only the new and neighbouring test files are run. Failures are compared against `main`.

- **Reader:**
  - `menu_labs_slot1_idle`, `menu_labs_slot1_affordable` and
    `menu_labs_game_speed_running` read `LockedSlot(2, 100, point)`;
  - `menu_labs_active` (five slots owned) reads `None`.
  - A live locked-slot-3 capture is added when an account shows one. Until then slots 3–5
    are covered by the catalog-price check and label parsing.
- **Rollout record:**
  - every transition;
  - the promotion thresholds (2 visits, 10 minutes, same price, same worker);
  - a catalog-price mismatch halting the slot;
  - two concurrent writers;
  - a corrupt or missing file;
  - `release_canary`;
  - `reset`.
- **Executor:**
  - a rehearsal never taps and never prepares a transaction;
  - the canary taps exactly once;
  - each of the three post-tap outcomes leads to its resolution, promotion or halt;
  - a slot at fleet stage unlocks on a second worker.
- **Wiring:** `_authorize_lab`, `gem_automated`, `_gem_met` and `evaluate_resources`
  follow the rollout stage. The existing slot-2 tests are migrated.

## Live verification

After merge and a fleet restart, watch Tiramisu64_60 and _61:

1. Two dry runs of slot 2.
2. Promotion to canary.
3. One real unlock.
4. Promotion to fleet.

The worker log, the ledger and the rollout record show each step. The first real tap is
where we learn whether the game shows a dialog. Either the unlock succeeds, or the slot
halts with evidence frames; both complete this milestone.
