# Labs & Gems path — design

Date: 2026-09-26
Mockup: https://claude.ai/artifact/4qQW6N5JnTWYdQ8ASynxSm

## Goal

Give every strategy a real Labs lane and Gems lane made of blocks, ship a built-in
template for the common community path, and show each emulator's lab slots, the next
lab per slot with its price, and the next gem step on a new **Labs & Gems** page.

It also adds **Strategy rules**: settings for the whole strategy, such as how coins are
shared between Workshop and Labs, whether labs start automatically, and how lab pools
pick. Blocks inherit these rules.

The lab and gem executors stay the same: Game Speed in Lab 1 (coins) and the 100-gem
Lab 2 unlock. Three rules are **live** and change worker behavior today:
- coin sharing, which affects Workshop spending and Game Speed;
- start labs automatically;
- unlock lab slots.

Every other block and rule evaluates and shows as "Planned · not automated". Automating
Labs 3–5 and other research is a follow-up project; it needs new screen readers and
recorded fixtures.

## Research summary

Sources: Fandom wiki (via its MediaWiki API), tower-hub.com, and The Tower Discord
(read-only): the pinned "Lab Progression Guide" in #beginner-guides, the "Labs Tier List"
in #player-driven-guides, and repeated answers in #player-questions / #tower-talks.

Facts used as data:

| Item | Value | Source |
|---|---|---|
| Labs unlock | Tier 1 wave 30 | Fandom Lab_Upgrades |
| Lab slot gem prices | Lab 2 = 100, Lab 3 = 400, Lab 4 = 1,400, Lab 5 = 3,000 | Fandom Lab_Upgrades |
| Card slot gem prices | slot 2 = 50, 3 = 100, 4 = 200, 5 = 300, 6 = 400, 7 = 500, 8 = 600, 9 = 750, 10 = 1,000 | Fandom Cards |
| Card | 20 gems | Fandom Cards |
| Labs Speed unlock | Tier 1 wave 150 | Fandom Lab/Lab Speed |

Game Speed (Fandom Lab/Game_Speed; Discord confirms L7 ≈ 25 days):

| Lvl | Coins | Base time | Max speed |
|---|---|---|---|
| 1 | 300 | 9m | ×2.0 |
| 2 | 2,500 | 2h 30m | ×2.5 |
| 3 | 12,000 | 9h 48m | ×3.0 |
| 4 | 50,000 | 1d 10h 2m | ×3.5 |
| 5 | 150,000 | 3d 19h 39m | ×4.0 |
| 6 | 500,000 | 14d 1h 46m | ×4.5 |
| 7 | 1,000,000 | 25d 11h 6m | ×5.0 |

Community path. Where the wiki and Discord disagree, the template follows Discord:

- **Gems:** buy all five lab slots before anything else. Then card slots up to about 10,
  only when a usable card is waiting. Then cards for card-buy missions, then modules.
  Nobody recommends rushing labs with gems. (The wiki's Gem Guide alternates lab slots
  and card slots instead.)
- **Labs by slot:**
  - Slot 1: Game Speed to max ("save up for each level"), then Attack Speed to 50.
  - Slot 2: short labs until Game Speed is maxed, then Labs Speed, kept running.
  - Slots 3–4: economy (Coins/Wave up to about 20, then Cash Bonus, then Coins/Kill).
    Skip Starting Cash.
  - Slot 5: flex (Buy Multiplier, Workshop discounts, short labs).
- **Open disagreements, recorded on the blocks:**
  - Labs Speed target: 30 vs 50.
  - After Game Speed in slot 1: Attack Speed vs Coins/Kill.

## Scope

**In scope**

1. A lab and gem catalog with the prices above.
2. Block programs for the `labs` and `gems` lanes, with validation and a pure evaluator.
3. The "Common Labs & Gems path" built-in template.
4. Studio editing of the two new lanes, plus a Strategy rules panel (section 9).
5. A new Labs & Gems page and its API.
6. Moving Recent Workshop buys to the Workshop page.
7. A slimmed Fleet roads card.

**Out of scope (follow-up)**

- New executors: unlocking Labs 3–5, card slots, researching any lab other than Game
  Speed, module pulls.
- Reading slots 3–5 and slot 2's research from the screen.
- Price tables for labs other than Game Speed.
- Lab rushing. It stays forbidden.

## Design

### 1. Catalog — `catalog/labs.v1.json`

- **`labs`:** one entry per lab the template or editor references.
  - Game Speed carries its full `levels` table (`coins`, `seconds`, `max_speed`) and its
    `max_level` of 7.
  - The others carry `id` (existing `labs.*` concept id), `name`, `unlock`
    (e.g. `{"best_tier_1_wave": 150}`) and `levels: null`, meaning the price is unknown.
- **`lab_slots`:** gem prices for slots 2–5.
- **`card_slots`:** gem prices for slots 2–10.
- **`card_gems`:** 20.
- **Metadata:** each entry records its source URL and a `checked` date.

A loader `lab_catalog.py` validates the file at import, like `workshop_prices`.

### 2. Route model — `fleet/build_route.py`

- `GemRoute` and `LabRoute` each gain `mode: "steps" | "blocks"` (default `"steps"`) and
  `blocks`. The existing `steps` shape keeps working unchanged.
- Blocks-mode invariants:
  - The first gem block is `unlock_lab_slot` with slot 2. This keeps today's 100-gem
    reserve rule.
  - The slot-1 track starts with `research` Game Speed.
- The route still rejects anything that would rush labs.

### 3. Blocks — new module `fleet/resource_blocks.py`

This is a separate module from `strategy_blocks.py`. That file is Workshop/Battle
purchase logic and is already about 790 lines. It shares the same limits (`MAX_BLOCKS`,
`MAX_DEPTH`, unique ids, labels).

Labs lane:

| Block | Fields | Meaning |
|---|---|---|
| `slot_track` | `slots` (subset of 1–5), `children` | The ordered plan for those slots; the first unmet child wins |
| `research` | `lab_id`, `to_level` | Research this lab until it reaches `to_level` |
| `lab_pool` | `lab_ids`, `selection` (`ordered` \| `cheapest`), optional `max_seconds`, optional per-lab `caps` | Pick one lab from the pool |
| `condition` | `field` (`best_tier_1_wave` \| `game_speed_maxed` \| `lab_level`), `cmp`, `value`, optional `lab_id`, `then`, `else` | Branch on observed facts |
| `wait` | — | Leave the slot idle |

Gems lane (evaluated top to bottom; the first unmet step is next):

| Block | Fields |
|---|---|
| `unlock_lab_slot` | `slot` 2–5, in order |
| `card_slots` | `up_to` (2–10), `when_usable_card` bool |
| `buy_cards` | `purpose` (`card_missions` \| `until_cards` with a card list) |
| `save_for` | `target: "modules"` |
| `wait` | — |

**Automated set.** It is exactly `research labs.game-speed` in slot 1 and
`unlock_lab_slot 2`. It lives in one place, `resource_blocks.AUTOMATED`, so the UI never
invents its own. Every other block evaluates to `planned`.

### 4. Evaluator — `evaluate_lab_plan(route, facts) -> LabPlan`

This is a pure function, used by the page API and by Fleet roads.

- **Per slot 1–5:**
  - `now`: one of `researching`, `idle`, `locked`, `owned_unread`, `unknown`, plus a
    level, `completes_at`, `stale`.
  - `next`: `lab_id`, level, and the price and duration from the catalog (or `null`).
  - `covered`: wallet ≥ price, or `null` when the price is unknown.
  - `automated`.
  - `why`: a trace of the blocks it passed.
- **Gems:** `wallet`, `next` step, `price`, `have`, `automated`, `why`.
- **Legacy steps-mode routes** are translated to the equivalent blocks before
  evaluating, so the page works for every strategy.
- `evaluate_resources` keeps its current output shape.

Slot state comes only from what we actually read. We never guess:

| Slot | Source | When not read |
|---|---|---|
| 1 | The cadence record, extended with `job_completes_at` | `unknown` |
| 2 | The slot2 record (`locked` / `owned`) | `unknown` |
| 3–5 | Inferred `locked` when slot 2 is locked, because slots are bought in order | `unknown` |

- An owned slot 2 has an unread research, so it shows `owned_unread`.
- A passed `completes_at` with no newer read shows "Should have finished ~N ago", never
  a negative timer.
- A read older than 24 h is marked `stale`.

### 5. Template — `fleet/strategy_library.py`

New built-in: **"Common Labs & Gems path"**.

- It copies the Opening template's Workshop and Battle programs and uses blocks mode for
  both new lanes, following the community path above.
- Like the other templates it is immutable; "Create copy to edit" forks it.
- Existing templates are unchanged.

**Rules in the template:**
- `lab_share: save_pct 25`.
  - Discord says to "save up for each level" of Game Speed. Full `labs_first` would
    pause Workshop until 1M coins for Game Speed 7, so 25% is the middle ground.
- Auto-start on, auto-unlock on.
- `pool: cheapest, max_price_pct_of_wallet 10`.
- `idle_fill: shortest_under_30m`, which matches Discord's "short labs" advice for
  slot 2.

### 6. Studio — `ResourceBlocks.tsx`

- Renders and edits the two lanes as blocks (add, remove, reorder, edit fields),
  reusing the Workshop block editor's list and inspector patterns.
- Each block shows the same "Planned · not automated" / "Automated" badge the Labs page
  uses.
- A steps-mode route shows a "Convert to blocks" action.

### 7. API — `GET /api/fleet/labs`

New module `fleet/labs_view.py`. It returns one row per worker:
- identity, strategy name, `read_at`;
- wallet coins and gems;
- the `LabPlan`;
- recent activity: the last 8 ledger lines of kind `LAB` and `CARD_BUY`, with reason when
  present, plus the Studio's gem-path steps with each marked done, current or next.

It is read-only and never writes facts or spends.

### 8. UI

- **Sidebar:** add "Labs & Gems" (FlaskConical) right after Workshop.
- **`app/fleet/reroll/labs/page.tsx` + `LabsMatrix.tsx`:**
  - Summary chips.
  - An emulator × slot matrix, where each cell has a Now tier and a Next tier.
  - A gem column.
  - Expandable rows with the gem-path stepper and recent activity.
  - Game Speed price and lab/card slot price reference panels, with sources.
  - A "Hide not-automated" toggle.
  - `?worker=` focus.
  - A card-per-emulator layout below `md`.
- **Color rules:**
  - Amber appears only on a slot that is idle *and* whose next lab is automated and
    covered.
  - "Planned · not automated" is neutral.
  - The gem-step highlight uses its own hue.
- **Fleet roads (`FleetRoutePreview.tsx`):**
  - Remove the Gems · Labs box and the Recent Workshop buys list.
  - Add a three-line strip:
    - Labs, linking to `/fleet/reroll/labs/?worker=`.
    - Gems.
    - Last buy, linking to `/fleet/reroll/workshop/?worker=`.
  - When there's no data, show "Labs: unknown" rather than hiding the line.
- **Workshop page:** accept `?worker=`, and show the focused emulator's recent buys (same
  data as today's section) under the matrix.

### 9. Strategy rules

**Model.** `RouteBaseline` gains `rules: RouteRules`, validated in `fleet/build_route.py`.
Defaults reproduce today's behavior exactly, so existing routes and templates are
unchanged.

```
rules:
  coins:
    lab_share: {mode: "when_affordable" | "save_pct" | "labs_first", pct: 5..90}   # default when_affordable
    workshop_spend_limit_pct: 10..100       # moved from workshop.coin_spend_limit_pct
  labs:
    auto_start: bool                        # default true
    pool: {selection: "cheapest" | "ordered" | "shortest",
           max_price_pct_of_wallet: 1..100 | null, max_seconds: int | null}
    idle_fill: "leave_idle" | "shortest_under_30m"   # default leave_idle
  gems:
    auto_unlock_lab_slots: bool             # default true
    spend_limit_pct: 10..100                # moved from gems.spend_limit_pct
    keep: int >= 0                          # default 0
```

**Migration of the moved fields.** The old locations stay readable. On load,
`workshop.coin_spend_limit_pct` and `gems.spend_limit_pct` fill the rules when `rules`
is absent. Saving writes both places for one release so older workers read the same
value. Account overrides that set `coin_spend_limit_pct` keep working and map to the
rule.

**Precedence.** It is the same everywhere: in the worker, in previews, and in the trace
shown in Studio.
1. Fixed safety rules. These cannot be edited and are shown locked: never rush, never
   cancel a running lab, pay only the price read on screen, and keep 100 gems for Lab 2
   until it's owned.
2. Reserves: `gems.keep`, the Lab 2 reserve, and the lab coin jar.
3. The coin-sharing rule.
4. Spend limits.
5. Lane blocks.
6. The existing wave-60 stop.

For pools, a `lab_pool` block inherits `rules.labs.pool` when it leaves a field unset.
A block may set a stricter limit, never a looser one; validation rejects a looser value.

**Coin sharing (live).** Workshop and Labs share one coin wallet.

- `when_affordable` is today's behavior. A lab starts only when the wallet covers it,
  and Workshop never holds back.
- `save_pct` uses a lab coin jar, a per-account amount of coins that Workshop may not
  spend.
  - At each Workshop visit, when an automated lab is waiting for coins:
    `jar = min(next_lab_price, jar + pct% × (wallet − jar))`.
  - Workshop's ceiling is `(wallet − jar) × workshop_spend_limit_pct`.
  - When the lab is covered (wallet ≥ price), the lab visit is due. After the lab's
    confirmed coin debit, the jar resets to 0.
  - With no automated lab waiting (maxed, or none covered by the automated set), the jar
    is 0.
  - The jar is stored in `lab-coin-jar.json` next to the cadence files. It is written
    atomically and belongs to one account.
  - If the file is missing or unreadable, the jar is 0 (the safe direction: Workshop
    behaves as today) and the problem is logged.
- `labs_first`: while an automated lab reads `wait_coins`, Workshop visits are skipped
  and the lab check is due again when the wallet reaches the price. It never skips
  claims, the tutorial grant, or the first Cards visit.

The three places that compute a Workshop coin ceiling go through one helper,
`fleet/coin_share.py: workshop_ceiling(route, wallet, jar) -> int`:
`build_route_eval.py:285`, `reroll_progress.py:418` and `strategy_blocks.py:352`. This
means previews and workers cannot disagree.

**Start labs automatically (live).** When `labs.auto_start` is false:
- `lab_due` never arms the Game Speed start;
- the page shows "Auto-start off" on slot 1's Next tier;
- the jar stays 0.

**Unlock lab slots (live).** When `gems.auto_unlock_lab_slots` is false, `slot2_due`
never arms the Lab 2 unlock. `gems.keep` raises the gem floor used by the Lab 2 check
and by card gem spending (`shopping.py` gem floor).

**Planned.** Pool selection and limits, `idle_fill`, and the gem spend limit beyond
today's card use take effect only through planning and display until their executors
exist. They are tagged "Planned" in the panel.

**UI.** Build route gets a "Strategy rules" panel with four sections:
- Coins: Workshop vs Labs
- Labs
- Gems
- Fixed safety rules

Each rule is tagged Live or Planned. A side preview for the selected emulator shows how
the wallet splits (jar vs Workshop), the jar's progress to the next lab, and the rule
order. Rules are part of the strategy's saved version and its ledger history, like
lanes.

## Error handling

- The catalog fails loudly at import if invalid.
- A route with invalid resource blocks is rejected on save with the validator's message,
  as Workshop blocks are today.
- The API returns per-worker `unknown` states instead of failing when records are missing
  or unreadable.

## Testing

Python, run only the touched files:
- `test_lab_catalog.py` (new)
- `test_resource_blocks.py` (new): validation, invariants, evaluator per slot state, the
  legacy translation, the automated set
- `test_build_route_resources.py`
- `test_strategy_templates.py` / `test_strategy_library.py`
- `test_lab_plan.py` (`job_completes_at`)
- `test_coin_share.py` (new): jar growth, cap and reset; the ceiling for each mode;
  missing or corrupt jar file gives 0
- `test_build_route.py`: rules validation, migration of the moved fields, the
  stricter-only pool limits
- `test_reroll_progress.py` / the relevant `test_shopping*.py`: `lab_due` with
  auto-start off; Workshop skipped under `labs_first`; ceiling under `save_pct`
- `test_labs_view.py` (new)

UI (vitest):
- `LabsMatrix.test.tsx` (new): states and amber rule
- `FleetRoutePreview.test.tsx`: strip replaces sections
- `WorkshopMatrix.test.tsx`: `?worker=` and recent buys
- ResourceBlocks tests

## Acceptance

- The template exists, validates, and shows the community path in Studio.
- A copy can be edited and saved.
- On the Labs & Gems page:
  - every worker shows five slots, with no guessed state;
  - Game Speed's next level shows the catalog price and time, and whether the wallet
    covers it;
  - the gem column shows the next step with have/need.
- Fleet roads shows the three-line strip and links to both pages. Recent buys appear on
  the Workshop page instead.
- A route without `rules` behaves exactly as today. The defaults are
  `when_affordable`, auto-start on, and auto-unlock on.
- With `save_pct` 20, a Workshop visit spends at most `(wallet − jar)`. The jar grows
  toward the next Game Speed price and resets after that lab's confirmed debit.
- With `labs_first`, Workshop pauses while Game Speed waits for coins, and resumes once
  it starts.
- Turning auto-start or auto-unlock off stops those taps.
- The lab and gem executors themselves are unchanged. The rules only gate when they are
  armed and how much Workshop may spend.
