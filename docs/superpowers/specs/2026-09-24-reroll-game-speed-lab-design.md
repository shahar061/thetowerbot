# Game Speed research in reroll lab slot 1

## Goal

For every reroll emulator, keep lab slot 1 working on Game Speed until the
research reaches its observed maximum level. Research may be started only when
the slot is idle, Game Speed is available, and the account can pay its visible
coin price. An existing job is never cancelled. The bot never spends gems on a
lab slot, rush, or acceleration as part of this feature.

## Existing system and scope

`labs.py` models observed lab entries, owned slots, and active jobs, but has no
screen reader or action executor. `objectives.py` already identifies
`labs.game-speed` as an early research objective. The repository has one
recorded active Labs page (`tests/fixtures/menu_labs_active.png`), but no
recorded idle slot, research picker, or purchase confirmation. The reroll bot
already serializes Workshop, claims, Cards, and Stats menu visits. This feature
adds a Labs visit to that coordinator, after claimable rewards, without
changing the single-emulator automation or other lab slots.

## Observation and decision

The Labs reader identifies the page from its own header and reads lab slot 1
separately from the research list. It reports one of: occupied with a readable
job and timer, idle, locked, or unknown. The list reader resolves only a
clearly labelled Game Speed row, its current and maximum level, availability,
and coin price. It also reads the account's coin balance from a verified
header. Unknown, conflicting, or low-confidence readings authorize no spend.
Two consistent readings are required before the account state is updated or a
purchase is attempted.

A pure planner takes the fresh reading and chooses one action: wait for the
existing slot-1 job; visit the research list for an idle slot; start Game
Speed when the row and price are verified and affordable; wait for coins or an
unlock; or finish permanently when the maximum level is observed. When Game
Speed is unaffordable, slot 1 remains reserved for it. An active job in any
other slot does not affect this decision.

## Visit and action flow

The reroll worker checks Labs during a main-menu maintenance opportunity. It
does not interrupt a battle. A persisted next-check time based on an observed
job timer avoids reopening the page on every run; idle and unaffordable
accounts retry at a bounded interval or after a meaningful balance change.
The existing one-visit-at-a-time coordinator prevents a Labs walk from racing
with claims or shopping. Claimable rewards take priority because they may
provide the coins needed for research.

For an authorized start, the executor opens the observed idle slot, finds the
Game Speed row, rechecks the coin price and balance on the current screen, and
taps that row's verified purchase control. It then rereads slot 1. The action
is successful only when slot 1 visibly names Game Speed and shows an active
timer. A tap alone never records success or increments a research level.
Account state and the fleet ledger receive the verified job and coin spend;
completion is observed on a later visit, not inferred from elapsed time.

At every transition the walk has a timeout and a safe return path to the main
menu. Unexpected overlays, missing rows, ambiguous OCR, or changed prices
end the visit without another spend. The normal reroll loop can continue.

## Evidence and verification

Before enabling purchase taps, record current-game fixtures for: an unlocked
idle slot 1, the research list with Game Speed affordable and unaffordable, a
maxed or locked Game Speed row where available, and slot 1 after starting it.
Use the existing active Labs fixture for occupied-slot behavior. These frames
define anchors and button bounds; the executor must not guess coordinates
from the active fixture alone.

Focused tests cover occupied slot 1, idle and affordable, idle and
unaffordable, locked, maxed, unreadable or contradictory evidence, a price
change before tapping, visit timeout, and post-tap confirmation. Replay tests
use recorded frames to verify page recognition and row targeting. A paused,
single-emulator live check validates the full walk before enabling it for the
fleet. No gem-spending control is part of the action path.
