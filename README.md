# The Tower — background ADB bot

Automates the Android game *The Tower* running in an Android emulator
(Android Studio AVD or BlueStacks — see below). All input goes through ADB
(`input tap`), so the emulator window never needs focus and your mouse is
never taken over.

One scan is: capture a frame, work out which screen is showing, evaluate every
configured action against that frame, and tap the ones that pass. Actions only
fire in a run — the bot will not tap its way through a death modal or an ad.

Everything the scan loop does is published as an event. Sinks consume those
events on their own threads behind bounded queues, so a terminal panel, a
SQLite writer and a browser dashboard can all watch a running bot without
slowing the loop down. When a sink cannot keep up its events are dropped and
counted, never awaited.

## Install

Dependencies are managed with [uv](https://docs.astral.sh/uv/). If you don't
have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, from the repo root:

```bash
uv sync
```

That creates `.venv/`, installs the exact versions locked in `uv.lock`, and
downloads the Python pinned in `.python-version` (3.12) if it isn't already on
your machine. There is nothing to activate — every command below runs through
`uv run`, which re-syncs the environment first.

## Connect to the emulator

`adb` must be on your PATH (Android Studio ships it at
`~/Library/Android/sdk/platform-tools`):

```bash
export PATH="$PATH:$HOME/Library/Android/sdk/platform-tools"
adb start-server
adb devices          # the emulator should be listed
```

`config.py` holds both endpoints: `ADB_HOST`/`ADB_PORT` for the adb *server*
(default `127.0.0.1:5037`) and `DEVICE_HOST`/`DEVICE_PORT` for the emulator
itself (default `127.0.0.1:5555`). `--host` / `--port` override the device
endpoint for a single run.

Both supported emulators land on that same default port, so `config.py` needs
no edit either way:

- **Android Studio AVD** — listens on its console port + 1, so the first
  emulator (`emulator-5554`) is reachable at `127.0.0.1:5555`.
- **BlueStacks** — can listen on `127.0.0.1:5555` directly. Some installations
  also register an `emulator-5554` alias. If both appear as separate attached
  transports, the bot refuses the ambiguous endpoint.

**The emulator must be 1080x2400 at 440 DPI.** The resolution
(`EXPECTED_RESOLUTION`) is the hard requirement: every template and every
screen region in `config.py` was measured at that size, and
`cv2.matchTemplate` is not scale invariant — a different resolution invalidates
all of them at once. Density matters because the game lays its UI out against
it.

### BlueStacks setup

Two settings, and the bot cannot work without either:

1. **Settings → Advanced → Android Debug Bridge: on.** Off, BlueStacks still
   opens port 5555 and still answers `getprop`, `dumpsys`, `logcat` and
   `screencap` — so the bot connects and captures frames perfectly well — but
   every other shell command, `input` included, dies with `error: closed`.
   The symptom is a bot that sees everything and taps nothing.
2. **Settings → Display: custom resolution, portrait, 1080 x 2400, DPI 440.**

BlueStacks defaults to 1080x1920 @ 320, which matches nothing in `templates/`.
`adb shell wm size 1080x2400 && adb shell wm density 440` applies the same
geometry without an instance restart, but it is an *override*: BlueStacks
drops it on restart and the bot silently falls back to unmatched templates.
Use it to test, set it in Display to keep it. `wm size reset` / `wm density
reset` undo it.

Verify the whole chain — device, geometry, shell, and that taps share the
capture's coordinate space — before a first run:

```bash
uv run python -c "
import device, screens, vision, config
d = device.connect_device()
img = device.capture_screen(d)
print('frame', img.shape[1], 'x', img.shape[0], 'expected', config.EXPECTED_RESOLUTION)
d.shell('input tap 5 5')          # raises if the ADB shell is restricted
print(screens.classify(img, vision.TemplateCache(config.TEMPLATE_DIR)))
"
```

A confirmed screen at ~1.000 confidence means the templates are valid on this
emulator; anything under `ANCHOR_THRESHOLD` means they are not.

#### Named BlueStacks fleet workers

BlueStacks Air has no proven scriptable create, clone, reset, or stop interface
on this host. Use a bounded, manually provisioned pool. Create the instance in
the BlueStacks UI, verify its ADB port, and write a JSON inventory such as:

```json
{"instances": [{"name": "Tiramisu64", "endpoint": "127.0.0.1:5555", "lease_id": "lease-a", "state": "running"}]}
```

Run one supervised dashboard worker with matching `--host`, `--port`, and
`--lease-id`, plus `--bluestacks-instance Tiramisu64 --bluestacks-pool
/path/to/pool.json`. Also supply `--worker-id`, `--attempt-id`,
`--runtime-root`, an explicit `--web-port`, and `--web`. The pool file is
read-only to the bot. Supply `--game-package` with the verified Tower package
for relaunch. A stopped manual instance requires an operator start;
recovery then retries the exact leased endpoint and quarantines that worker
on exhaustion. A session-conflict modal also quarantines without choosing
either response. Automatic replenishment is unavailable until a host control
API is independently qualified. Clone staging is reserved for later
qualification and has no dashboard command.

M05 clone-source qualification is an explicit staging transaction in
`fleet/clone_qualification.py`. It records a source Account popup observation,
stages two clones through the exclusive M03 staging lease, invokes R00 for each
clone, and probes both workers concurrently through a named host restart and
fresh post-restart account observation. The persisted record binds host,
BlueStacks version, source lineage and version, game version, instance
configuration, three distinct Tower Account IDs, endpoints, leases, and
evidence times. The read gate closes on missing, simulated, stale, or changed
scope. The installed manual pool cannot stage or restart clones, so it records
`unqualified` with `unsupported_host_capability`; no dashboard clone command
is enabled. A future host driver must independently attest its live capability
and measured scope before M05 can record `passed`.

## Run

```bash
./run.sh                                 # sync, rebuild if stale, then --web
./run.sh --idle                          # any tower_bot.py flag passes through
uv run tower_bot.py                      # scan every 2s until Ctrl+C
uv run tower_bot.py --tui                # live terminal panel
uv run tower_bot.py --web                # browser dashboard on :8765
uv run tower_bot.py --web --idle         # dashboard with no bot - press Start
uv run tower_bot.py --debug-scores       # one-shot diagnostic, taps nothing
```

`./run.sh` is the one to use after pulling. Editing Python moves the backend
hash without touching a single dashboard source, so the committed bundle goes
stale and the dashboard refuses to drive the bot - "Runtime mismatch, controls
locked". The script checks both hashes before launching and rebuilds only when
they disagree, so a current bundle still needs no node. It also stops a
previous run holding the port, but only after confirming the process really is
this bot.

| Flag | Default | What it does |
|---|---|---|
| `--host` / `--port` | `127.0.0.1:5555` | emulator ADB endpoint |
| `--interval N` | active strategy | seconds between scans — overrides the active strategy and is saved into it |
| `--once` | off | scan just long enough for the screen tracker to settle, then exit |
| `--debug-scores` | off | capture one frame, print every action's and anchor's match score plus brightness ratio, exit without tapping |
| `--tui` | off | live `rich` panel instead of log lines |
| `--auto-navigate` | active strategy | tap RETRY / BATTLE to loop runs unattended — overrides and saves |
| `--max-runs N` | active strategy | stop after N runs — overrides and saves |
| `--affordability` | active strategy | `digits` or `brightness` — overrides and saves |
| `--web` | off | serve the dashboard while the bot runs |
| `--idle` | off | with `--web`, serve the dashboard without starting the bot — press Start in the browser |
| `--strategy NAME` | the active one | which saved strategy profile to load |
| `--web-host` / `--web-port` | `127.0.0.1:8765` | where the dashboard binds — read the warning below before changing the host |
| `--db PATH` | `tower_bot.db` | SQLite file for the event log |
| `--no-store` | store | run with no database at all |

`--tui` and `--web` can be on at once.

`--interval`, `--auto-navigate`, `--max-runs` and `--affordability` all
overlap with the active strategy profile (`strategies/*.json`, editable from
the dashboard). Passing one of them on the CLI overrides the loaded profile
**and persists the change into it**, with a log line saying so — the
alternative, overriding without saving, would leave the dashboard showing a
value the file does not hold. Leave a flag off and the loaded strategy
supplies it unchanged.

### Strategy profiles

`strategies/` holds one committed, hand-editable JSON file per profile —
`strategies/default.json`, `strategies/crit.json`, and so on — plus a plain
text `strategies/.active` naming which one the bot loads by default.
`--strategy NAME` loads a specific profile for a single run without changing
`.active`. The dashboard's `/api/strategies/*` routes can list, read, write,
activate and delete profiles the same way, through the same `StrategyStore`
and the same validation a CLI flag would get.

## Screens, gating, and runs

Every scan starts by asking which screen is showing. Three anchor templates are
matched against the frame and the best score wins, provided it clears
`ANCHOR_THRESHOLD` (`0.8`):

| State | Anchor |
|---|---|
| `MAIN_MENU` | `templates/screens/main_menu.png` |
| `IN_RUN` | `templates/screens/in_run.png` |
| `GAME_OVER` | `templates/screens/game_over.png` |
| `UNKNOWN` | nothing scored high enough |

A transition is only believed after `SCREEN_CONFIRMATIONS` (`2`) consecutive
identical readings, so one ambiguous frame mid-animation does not move the FSM.

**Gating is the point of all this.** Upgrade actions fire on `IN_RUN` and
nowhere else. That rule is hardcoded in `tower_bot.py` — it is not a per-action
setting — and it is what stops the bot tapping blindly into a modal that has
dimmed the screen behind it.

Skips are recorded rather than silent, with five reasons, checked cheapest
first: `paused`, then `screen_gated`, then the match score, then
`unaffordable` / `dimmed`, then `cooldown`.

Menu pages — `MAIN_MENU`, `WORKSHOP`, `CARDS`, `MISSIONS` — are a separate
classification, done by `pages.py` against `config.PAGE_ANCHORS`, and kept
deliberately out of the table above. `screens.classify()` does
`ScreenState(winner)`, which raises on any name the enum does not have, so a
menu page can never become a `ScreenState` member without turning every
Workshop or Cards frame into a crash. Outside a shopping visit, a menu page
reading `UNKNOWN` to this tracker is correct behaviour, not a gap — see
"Shopping between runs" below.

### Unknown screens

Ads, daily rewards, anything unmodelled: the bot holds still. It taps nothing,
and writes the frame to `unknown/` as a nanosecond-timestamped PNG so you can
see what it hit. Writes are throttled to one per `UNKNOWN_MIN_INTERVAL`
(`30.0`s) and the directory is pruned to the newest `UNKNOWN_KEEP` (`50`). The
directory is gitignored; the dashboard shows the newest twelve as thumbnails.
These are how you decide which screen to model next.

A screen is only kept once. The throttle caps how *often* the bot writes, not
how many times it writes the same thing — a bot sitting on one unmodelled
screen for half an hour would still spend all fifty slots on it and evict
everything else. So each frame is fingerprinted first: greyscaled, averaged
down to a 9×8 grid, and reduced to 64 bits recording whether each cell is
brighter than the one to its right. A frame within `UNKNOWN_HASH_DISTANCE`
(`8`) bits of a snapshot already kept is dropped. At that size the
fingerprint tracks layout and ignores what moves inside it, so a ticking
counter, a fade, a scroll or a claimed button is the same screen, while a
different screen is 20–39 bits away. The fingerprints are read back off disk
at startup, so restarting the bot does not re-collect what it collected
yesterday, and evicting a snapshot frees its screen to be captured again.

Snapshotting is suppressed for the whole time a shopping visit is running.
The Workshop and Cards pages read `UNKNOWN` to this tracker by design (see
above), so without the guard every visit would fill `unknown/` with pictures
of the very pages it is deliberately visiting, evicting the genuine
unmodelled screens the directory exists to capture.

### Runs

A run opens on the first confirmed `IN_RUN` and closes on the first confirmed
state that is not `IN_RUN` — `GAME_OVER` is a normal end, `MAIN_MENU` closes it
as abandoned. `UNKNOWN` neither opens nor closes one, so a run survives an ad
appearing mid-fight. On a clean end the bot reads wave, coins and tier off the
death modal.

Run ids are the primary key of the `runs` table, so at startup they are seeded
from `MAX(id)`: kill the bot and restart it and the next run is numbered after
the last stored one instead of overwriting it. Event sequence numbers are
seeded the same way, and both are read *before* the retention prune so an
aged-out number is never reissued. Under `--no-store` both restart at 1,
because there is nothing to collide with.

### Auto-navigation

Off by default (`auto_navigate` in the active strategy). The bot still watches, gates, tracks runs and snapshots unknown
screens — it just never taps between screens, so it sits on the death modal
until you act.

Turned on, it taps exactly two buttons: `RETRY` on `GAME_OVER` and `BATTLE` on
`MAIN_MENU`, each near the matched template's centre (see "Jitter" below), no
closer together than `NAVIGATION_COOLDOWN_SECONDS` (`3.0`s). `--max-runs N`
stops after N runs and suppresses the RETRY that would have started run N+1.

This is a strategy field, but its switch sits on the **Control** page beside
Start rather than only on the Strategy page, and that placement is the point:
it is the difference between "the scan loop is running" and "the bot is
playing". With it off, Start produces a bot that watches the death modal
forever, and the only way to find that out used to be to go and look at the
emulator. Off, the page now says so before you press anything.

### Jitter

Every tap is an `input tap` over ADB: a synthetic event with no travel path
and no dwell, aimed at a pixel derived by a fixed offset from a template
match. Left alone that means the bot taps the *identical* pixel on every
purchase of a given upgrade, for its whole life, and scans on a metronome.
`jitter.py` is the single place variance is added, and all three knobs live
in the active strategy's **Jitter** section, editable live.

| Field | Default | What it does |
| --- | --- | --- |
| `tap_jitter_px` | `8.0` | Radius, in pixels, around the computed tap point. Gaussian (σ = radius/3, clamped), because a human's taps bunch near a button's middle rather than spreading evenly to its edges. |
| `timing_jitter` | `0.15` | Fraction applied to the scan interval (± symmetric) and to both cooldowns (+ only). |
| `tap_delay` | `0.12` | Mean pause between finding a match and sending its tap — without it the tap leaves in the same breath as the scan that decided it. |

`0.0` switches any of them off individually, which is how the old fully
deterministic behaviour stays reachable without a code change.

Two asymmetries are deliberate, and both are about not breaking something
that already works:

- **The cooldowns only ever stretch.** `click_cooldown` gives a slow UI time
  to respond and `navigation_cooldown` waits out a transition — they are
  functional minimums, not targets. Jittering them *down* would re-admit the
  double-tap they exist to prevent, so jitter can only lengthen them. The
  scan interval, by contrast, is a target and varies both ways: a loop that
  only ever waited *longer* than its nominal interval is still a pattern.
- **`tap_jitter_px` is capped at `19`**, which is `PRICE_REGION.h / 2` rather
  than a chosen number. The buy point sits at the centre of the price strip,
  the smallest thing the bot ever taps; a radius past half its height could
  land outside the button, where the tap buys nothing while the bot still
  reports a successful purchase. Re-measure `PRICE_REGION` for a new
  resolution and the ceiling moves with it.

The jittered point — not the un-jittered one — is what the device view's
crosshair draws and what `Tapped` events carry, so the overlay never lies
about where a tap actually landed.

### Game speed

Inside a battle the game draws a speed widget at the bottom right of the play
area — `[−] x1.0 [+]`. `speed.py` reads it and taps its arrows, and there are
two ways to drive it, deliberately kept apart:

| | What it is | Where |
| --- | --- | --- |
| `target_speed` | Standing policy. The bot reads the readout every scan and taps one arrow toward the target until they match. `null` (the default) means "leave the speed alone". | Strategy → Run policy |
| `speed_up` / `speed_down` | One command, performed once. "One step from wherever it is now." | Control page, the `−` / `+` buttons |

The split matters because the two fail differently. A patch is idempotent —
re-sending it leaves the same settings — whereas re-sending a command taps
again, so they do not share a route: commands go to `POST
/api/control/command`, which queues them on `Controls` for the scan loop's
next pass. **The web layer never taps the device.** It has no handle on one,
which is the boundary `control.py` exists to keep.

Both paths are refused off the battle screen and while paused, and a command
the loop cannot honour right now is **discarded rather than banked** — held,
it would fire the moment the bot next entered a run, which can be minutes
after the person who pressed the button stopped watching.

**Two lists, and the difference matters.** `SPEED_VALUES` is every reading the
widget can show — harvested off a live run as `(0.0, 1.0, 1.5)`.
`TARGET_SPEEDS` is the subset a strategy may aim for, and it excludes `x0.0`:

- The bottom step does not slow the game, it **stops** it. That has to be
  *readable*, because `decide()` refuses to act on a widget it cannot read —
  a bot that read `None` at `x0.0` could never tap its way back out, and would
  sit on a frozen game for the rest of the run.
- It must not be *holdable*. A strategy pinned to `x0.0` is a soft hang: no
  cash accrues, no upgrade becomes affordable, the run never ends, `max_runs`
  is never reached, and the dashboard reports "running" throughout.

`target_speed` is validated against `TARGET_SPEEDS` by membership, not by
range, because every legal value needs a readout template to be recognised by.
A target between two known speeds is one the bot would tap toward forever
without ever matching. For the same reason `SpeedController` keeps a patience
budget: a target above the account's unlocked ceiling makes the widget simply
stop moving, and without a budget the loop would tap `+` at that ceiling for
the rest of the run. Four taps that leave the reading where it was and it
stops asking; a reading that *moves* resets the budget, so a long climb never
exhausts it.

**`x1.5` is this account's ceiling, not the game's** — higher speeds unlock
with progression. To pick up a newly unlocked one, start a run, turn the speed
all the way down, and:

```bash
uv run tools/harvest_speed_glyphs.py
```

It taps `+` until the readout stops changing — discovering the real ceiling
rather than guessing it — saves a crop per step, and puts the speed back where
it found it. It deliberately does not *name* the values: reading the readout
is the very thing the templates are needed for. Rename the crops to
`templates/speed/x<value>.png`, list them in `config.SPEED_VALUES`, and:

```bash
uv run pytest tests/test_speed_loop.py -k template
```

which fails naming any value whose template is missing. Never add a value by
hand: one listed without its template is a `FileNotFoundError` out of the scan
loop the first time the bot reads the widget.

The arrows are tapped at their region's centre rather than at a template
match — the same call `config.buy_point()` already makes for the price strip.
The widget does not move relative to the `IN_RUN` anchor, so a match would
cost a full `matchTemplate` per scan to rediscover a fixed offset. The
death-modal rule that forces template matching elsewhere is about a screen
that genuinely shifts. The *readout*, by contrast, is matched inside
`SPEED_READOUT_REGION` rather than full-frame, and that is not an
optimisation: `x1.00` (the coins multiplier) and `x1.20` (critical factor) are
both drawn elsewhere on the same screen, so a full-frame match for `x1.0`
would happily find the wrong one.

### Shopping between runs

Off by default (`shopping.enabled` in the active strategy, same shape as
`auto_navigate`). When the bot lands on `MAIN_MENU` between runs, it can walk
the Workshop and Cards pages and buy from a priority list you edit on the
**Strategy** page, instead of just sitting there waiting for the next
`BATTLE` tap.

A visit advances **one step per scan** — one capture, one decision, at most
one tap — the same discipline the rest of the scan loop already runs under.
`ShoppingSession.advance()` in `shopping.py` is written so every call either
makes progress, publishes a skip or a purchase, or ends the visit; it never
blocks waiting on the next frame. That is what keeps a minute-long shopping
errand from arriving as one opaque log line after the fact: Pause still
freezes it mid-errand instead of having to unwind it, the device frame
stream never stalls, and the event feed shows every row it looked at, not
just the ones it bought.

**This is the first thing the bot does that spends a resource you cannot get
back.** `navigate.py`'s own docstring opens by boasting that "nothing here
spends permanent resources: in-run upgrades are bought with per-run cash
that resets, and coins are only ever earned." Shopping breaks that
invariant on purpose — it spends coins and gems the game does not hand back
— so every safety property the rest of the bot gets for free had to be
re-earned here explicitly: no brightness fallback (see below), a step that
raises ends the visit instead of the bot, a tap budget, and a switch that
defaults to off.

Two switches, not one mode:

| Field | Default | What it does |
|---|---|---|
| `enabled` | `false` | Lets a visit start at all |
| `armed` | `false` | Lets a visit actually tap |

`enabled` without `armed` is a full rehearsal: the bot navigates, reads
prices and balances, decides what it would buy, and publishes a `Purchased`
event for every purchase it *would* make (`dry_run=True`) — it just never
reaches `device.tap`. `_tap()` in `shopping.py` is the only place in the
whole module that calls it, and it returns before getting there whenever
`not shopping.armed`. `armed` is the only field standing between a
miscalibrated template and coins or gems that cannot be refunded, which is
why it is its own boolean rather than a value of something else, and why it
defaults to `false` independently of `enabled`.

**The buy rule** is: on the current tab, buy the highest-priority affordable
row, re-read the balance, and repeat until nothing left on that tab is
affordable. Row order **is** the policy — `Shopping.workshop` is a flat,
reorderable list, the same shape as `Strategy.actions` for in-run upgrades —
and which tabs get visited, and in what order, is *derived* from that same
list (`Shopping.categories_in_priority_order()`) rather than configured
separately. Reordering rows is the only control anyone needs: a tab whose
rows are all disabled is never opened, because there is nothing on it to
buy.

Cards work the same way with one difference: a card purchase does not use up
the row, so the bot keeps buying the configured batch (`x1` or `x10`) until
it hits a cap rather than until the button disappears. Cards ship disabled
(`cards.enabled: false` by default) — see "What it never taps" below for
why. Four numbers bound how often a visit happens and how much it can
spend:

- **Visit cadence** (`visit_every_n_runs`, default `1`) — how many runs pass
  between the end of one visit and the start of the next being considered
  at all. `1` means every run that ends on `MAIN_MENU` is a candidate.
- **Gem floor** (`cards.gem_floor`, default `40`) — the bot will not spend a
  gem balance below this floor. Inclusive at zero on purpose: spending down
  to nothing is a real, permitted choice, unlike a negative floor, which is
  not a choice at all.
- **Cards per visit** (`cards.max_per_visit`, default `2`).
- **Tap budget** (`max_taps_per_visit`, default `40`) — every attempted tap
  counts against it, armed or not, so a rehearsal hits the same ceiling a
  real run would.

**What it never taps:** card slots, labs, modules, relics, the shop, and
Ultimate Weapon selection — all deliberate, not missing features.

- Gem purchases don't touch **lab slots**, even though the community's own
  gem spend order (see the **Guide** page) puts lab slots *above* cards. The
  bot cannot see the Labs screen at all — no template, no classifier,
  nothing — so a bot spending gems on cards while blind to the better
  purchase would be worse than one spending none.
- **Ultimate Weapon** picks are irreversible, and each new pick costs more
  than the last. Permanent plus escalating is exactly the combination a
  policy read off a priority list should not be trusted with.
- Card **slots**, **modules**, **relics** and the **shop** are simply out of
  scope for this feature; nothing about any of them is modelled.

**Bail-outs.** Any of the following ends the visit immediately and taps the
Battle tab on the way out, best-effort, whether or not the tap budget has
room left — the one tap explicitly licensed past the cap, because the
alternative is a bot stranded on the Workshop or Cards page with nothing,
not even `navigate.py`, able to rescue it:

- The tap budget runs out.
- The same page fails to classify, or a positioning step's target template
  is not found, two scans in a row — once is assumed to be an animation
  frame still settling, twice means a mis-cut or renamed template.
- Any step raises. `advance()` catches everything, logs it, and ends the
  visit with the exception message as the reason, rather than ever letting
  a bad frame crash the scan loop.

Every ending, clean or aborted, publishes `ShoppingEnded(bought=…, spent=…,
aborted=…, reason=…)`, and every purchase attempt along the way publishes
its own `Purchased` or `PurchaseSkipped` event — so a visit is as legible in
the event feed as any other run, not one entry that shows up after the fact.

> **Two things here are still unverified on a live device.**
>
> The coin balance is read with OCR, and has never been read that way on a
> live device. `shopping.header_numbers()` reads each balance off its own
> padded crop of the page's header region (`ocr.read_region`) and takes the
> one number it finds there — two numbers in one crop is ambiguity and
> refuses. Cropping rather than filtering a whole-frame read is what makes a
> short balance readable at all: the detection stage returns no box for an
> isolated `0` in a full frame, at any confidence floor. It reads both
> balances correctly off all five committed menu fixtures, and
> `build_shopping()` gates on `ocr.available()`, so an engine that will not
> import disables buying loudly at startup instead of silently. What no
> fixture can prove is that the region keeps to its own number once the
> balance grows a digit wider. This fails safe the same way the glyph atlas
> did: an unread balance aborts the visit, and can approve no purchase.
>
> The `menu` glyph atlas — the size class card prices are read at — is
> incomplete the same way. `templates/atlas/menu/` holds `0 2 3 4 5 7 coin
> gem`, missing `1 6 8 9`: a glyph can only be harvested once a price
> actually containing it has appeared on screen, and nothing so far has
> forced one. Nothing gates this at `build_shopping()` time - card prices
> escalate with every purchase, so a
> session could buy once or twice against today's readable prices and then
> start refusing every card purchase the moment one crosses 1, 6, 8 or 9.
> The `menu` atlas gap no longer affects workshop prices: those are read by
> OCR (`tiles.read_rows`), which read `92` off a live Attack Speed row that
> the atlas refused, and bought it. The atlas is still what reads CARD
> prices, which are not part of the OCR row-addressing work, so the gap
> stays real there.
>
> One thing about closing that gap has changed. `tools/harvest_menu_glyphs.py`
> now finds workshop prices the way the bot does, by OCR, and crops the price
> box itself — where the old version cropped a measured region that
> deliberately included the coin icon beside the number. The digits it
> harvests are unchanged (verified against the old harvester on the same
> fixtures: both reach `0 3 4 5 7`), but the coin glyph is no longer among
> them, and the cards half supplies only the gem. The committed `coin` entry
> is fine; it simply could not be re-harvested from workshop fixtures if it
> were ever lost.

The workshop buy point used to be the third thing on that list. It is
settled now: a watched live visit tried both tap points the codebase had and
neither bought anything — the row label and the tile centre each left coins
and price unmoved — while a tap on the price strip bought (coins 1740 →
1680, price 56 → 92). So a row purchase taps the price box OCR just read,
which is `tiles.Row.tap`. `config.buy_point()` was never involved and still
is not: it exists because an IN-RUN upgrade's label opens an info panel
instead of buying, and the in-run surface has not been cut over.

## Capturing templates

```bash
uv run grab_screen.py screen.png
```

Crop each button out of `screen.png` and save the crop into `templates/`.

Two rules, and breaking either one produces a template that matches nothing or
matches everything:

1. **Same emulator resolution as the bot runs at** (1080x2400 @ 440 DPI).
   `cv2.matchTemplate` is not scale invariant.
2. **Cut from a lit, in-run frame.** Not from a screenshot where a modal has
   dimmed the screen, not from a greyed-out unaffordable button, not from a
   promotional image. The brightness check below compares each candidate region
   against the template's own grey level, so a template cut from a dimmed frame
   sets the baseline at the dimmed value and defeats the check entirely.

`tools/crop_preview.py` previews a candidate region against a captured frame
and prints a paste-ready `config.Region(...)` line, which is how the screen
regions in `config.py` were measured:

```bash
uv run tools/crop_preview.py tests/fixtures/in_run_wallet.png \
    --anchor IN_RUN --rect 40 -60 220 46 --out /tmp/crop.png
```

## Reading the numbers

The default affordability strategy, `digits`, reads the actual numbers off the
screen: it reads your wallet once per scan and each upgrade's price from beside
its matched label, and buys when `wallet >= price`. That is exact, and it is
also where the dashboard's wave, coin and tier stats come from.

It works by template-matching individual glyphs against an atlas. Because
`cv2.matchTemplate` is not scale invariant and the game renders numbers at three
sizes, there is one atlas per size class — `wallet`, `price` and `modal` — under
`templates/atlas/`, each a flat directory of single-glyph PNGs named for the
character they represent (`7.png`, `dollar.png`, `cap_w.png`).

Every read is all-or-nothing and every failure returns `None` rather than
raising: a partially recognised number is never returned, because reading `1?34`
as `134` would let the bot act on a price that looks plausible and is wrong by
an order of magnitude.

### Building the atlas

The committed atlas was built from a live emulator, so you only need this if a
resolution change invalidates it. One size class per run, and each wants a
different part of the game on screen:

```bash
# 1. start a run, then harvest the two in-run classes
uv run build_atlas.py --size-class wallet --frames 20
uv run build_atlas.py --size-class price  --frames 20

# 2. label wallet by hand — it is the bootstrap reference, with nothing
#    to score against:  templates/atlas/wallet/glyph_003.png -> .../7.png

# 3. auto-label the rest against it
uv run tools/label_glyphs.py --size-class price            # dry run + contact sheet
uv run tools/label_glyphs.py --size-class price --apply    # rename

# 4. let the run end, then harvest the death modal
uv run build_atlas.py --size-class modal --frames 10
uv run tools/label_glyphs.py --size-class modal --apply

# 5. verify
uv run pytest tests/test_atlas_real.py -v
```

`build_atlas.py` dumps unlabelled `glyph_NNN.png` files; `tools/label_glyphs.py`
scores them against an already-complete class and renames the confident
matches, writing a contact sheet of everything it was unsure about. Rename
those by hand — the caption letters and the coin icon score low precisely
because no reference class contains them.

### Known gap: no suffix glyphs in the wallet or price atlas

The `wallet` and `price` classes have **no suffix glyphs at all**: the
parser handles suffixes correctly, but the glyph matcher never gets that
far, so a wallet rendered as `$1.5K` still fails to read and that scan falls
back to brightness. Harvest those two classes' suffix glyphs during a
session that reaches large enough numbers, and label them by hand off the
contact sheet.

There was a `header` class too, for the menu's coin and gem balances, and it
was incomplete in the same way - it never held `2 3 5 6 9 M B`. It has been
removed rather than completed. Nothing read a header glyph any more:
`shopping.header_numbers()` reads both balances with OCR, and
`build_shopping()`'s startup gate is `ocr.available()` rather than the
atlas. A built-but-unread atlas is worse than no atlas, because it invites
exactly the wrong conclusion about why a balance came back empty.

The `menu` atlas - the size class `shopping.py` reads every card price at
(`_buy_cards` passes `"menu"` to `NumberReader.read`) - has the same kind of
gap: it is missing `1 6 8 9`,
because none of the committed fixtures happen to show a price containing
them. There is no dedicated startup gate for this one - `build_shopping()`
checks only that the OCR engine loads - so the failure shows up
per-purchase instead: a price containing one of those four digits reads as
None, and shopping.py refuses it the same way it refuses any other
unreadable price (`PurchaseSkipped(reason="unreadable")`, the row marked
exhausted for the visit) rather than reading a neighbouring glyph and
reporting a wrong number. Today's fixture prices - 30, 40, 50, 75 - happen
to avoid all four digits, which is why this has not surfaced in a fixture
run; prices escalate with every purchase on a live account, so a real
session would eventually hit one.

Closing it does NOT go through `build_atlas.py`: its `SOURCES` table (above)
keys every source on a `screens.ScreenState`, and the workshop and cards
pages a menu price lives on read as `UNKNOWN` to `screens.classify` by
design (see pages.py) - there is no `ScreenState` for `build_atlas.py` to
key a `"menu"` source on, and that is deliberate, not an oversight.
`tools/harvest_menu_glyphs.py` is the tool for this class instead: it reads
the COMMITTED menu fixtures rather than a live device, and is what built
the current partial `templates/atlas/menu/` in the first place. Capture a
fixture showing a price that contains `1`, `6`, `8` or `9`, add it alongside
the existing ones, then re-run `uv run tools/harvest_menu_glyphs.py` followed
by `uv run tools/label_glyphs.py --size-class menu --reference modal`.

Before spending that effort, note that this gap is on its way out: an OCR
reader now runs alongside the template path in dry-run and logs where the
two disagree (`tower_bot.ocr_ab`), precisely so a price the `menu` atlas
refuses and OCR reads can be counted before the templates are retired.

### Brightness, the older heuristic

Setting the strategy's `affordability` to `brightness` selects it outright.
Otherwise it is the fallback:
`digits` uses it for any single read it could not complete, and
`build_affordability` drops to it for the whole session if *any* size class is
missing from the atlas.

The two are distinguishable in the event log on purpose. A skip reason of
`unaffordable` means both numbers were read and the wallet was genuinely short;
`dimmed` means we could not tell and fell back.

`cv2.TM_CCOEFF_NORMED` normalises away mean and variance, so it is **blind to
brightness by design**: a semi-transparent popup dimming the whole page still
scores `1.000` against a template cropped before the popup appeared. So each
match is also checked against the template's own grey level and skipped when
the region is too dark. Tune it per button with the `Action`'s
`brightness_ratio` field (default `DEFAULT_BRIGHTNESS_RATIO`, `0.75`); `0.0`
disables the check for that action.

> **This guards against a dimming overlay, not a "can't afford it" style.**
> Measured on the live device: a lit in-run button reads `1.00` and a
> modal-dimmed one reads `0.28`, comfortably either side of `0.75` - that
> case works and is verified with a wide margin.
>
> It does **not** work for a greyed-out unaffordable button, and this has
> now actually been measured rather than assumed: on the Cards page at 40
> gems, measured over each button's own matched template (`CARD_BUTTONS` -
> the label and border only, not the price strip), the affordable `x1`
> button reads mean grey `51.7` / saturation `57.0`, while the unaffordable
> `x10` button reads mean grey `59.7` / saturation `45.5` - the unaffordable
> button is *brighter*, not dimmer. The game signals "cannot afford" by
> **desaturating** the price and icon toward grey, not by darkening
> anything, so `brightness_ratio` - which only ever measures mean grey -
> cannot distinguish the two at all: a gate calibrated on the affordable
> style scores the unaffordable button at roughly `59.7 / 51.7 ≈ 1.15` and
> would wave it straight through, in exactly the wrong direction. (A second
> measurement over `CARD_PRICE_REGION` instead - the price strip plus its
> gem icon, a different region with different absolute numbers - gets
> `52.5`/`65.1` mean grey and the same conclusion, at roughly `1.24`; see
> `test_the_unaffordable_card_button_is_desaturated_not_dimmed` in
> `tests/test_shopping_templates.py`.) See the comment on
> `config.DEFAULT_BRIGHTNESS_RATIO` for the button-template measurement. Do
> not try to calibrate `brightness_ratio` against an unaffordable state by
> spending a wallet down and reading `--debug-scores` - that advice assumed
> a dimming that is not how this particular state is actually rendered.
> Brightness keeps doing its original job - rejecting a dimmed overlay - and
> nothing
> more; reading the numbers (`digits`) is the only affordability check that
> works for both
> workshop upgrades and cards.

## Watching it work

Neither view is required — with no flags the bot writes ordinary log lines.

**`--tui`** replaces the log with a `rich` panel refreshed four times a second:
current screen, uptime, scan count, a tap tally per action, a skip tally per
reason, the last error, and the twelve most recent events. Logging is silenced
while it runs, because rich owns the terminal.

**`--web`** serves a dashboard at `http://127.0.0.1:8765`, seven pages behind
a sidebar. `--web --idle` serves the dashboard without starting a bot —
press **Start** in the browser.

- **Live** — the current run, the live device screen with its match overlay,
  a wave sparkline, a filterable live event feed, a run-history table, and
  thumbnails of unrecognised screens. Click a history row to replay that
  run's stored events; **back to live** returns to the stream. While a run is
  open it also carries **Purchases — this run**: every in-battle upgrade the
  autopilot has bought so far, timed from the start of the battle, with its
  price and the value it reached. The card is absent between runs, because
  there is no "this run" to report on then.
- **Runs** — every stored run, and a drill-down (`/runs/?id=42`, click a row
  or open the link directly) showing that run's wave, coins, tier, tap count
  and scan count alongside what it bought in the battle and its own event
  feed, both replayed from SQLite. The purchases card is the same one the
  live dashboard shows, served from the same `events` rows — an open run and
  a finished one are one query, not two. A price OCR could not read stays a
  dash and is counted apart from the total ("2 prices unread") rather than
  summed as zero, and a total spend is withheld entirely when not one price
  was legible. Purchases are subject to the 30-day event retention while the
  run row itself is kept forever, so an older run reports *no purchase
  record* rather than claiming it bought nothing.
- **Stats** — charts aggregated over every stored run: wave and run length
  over time, taps by action, and events by screen.
- **Errors** — `BotError` tracebacks alongside the unknown-screen snapshots,
  in one place.
- **Strategy** — the whole decision policy: which upgrades to buy and in
  what order, per-row match and brightness thresholds, loop timing, and run
  policy, plus a **Jitter** section (see "Jitter" above) and a **Shopping**
  section for the between-runs buy list — see
  "Shopping between runs" above. Named profiles live in `strategies/*.json`,
  switchable live and editable by hand. Two fields — navigation cooldown and
  screen confirmations — configure objects built once per bot, so they are
  labelled *applies on next Start*.
- **Guide** — the researched community strategy the bot's default buy order
  is drawn from, sourced page by page (economy/defence/attack order, the gem
  spend order, card mechanics) so you can judge whether you agree with it.
  Where the community disagrees with itself, the disagreement is kept
  rather than papered over.
- **Control** — see below.

The live device screen is `GET /api/frame`, an MJPEG stream
(`multipart/x-mixed-replace`) rendered by a plain `<img>` — no polling loop,
no cache-busting, no JavaScript decode path — with that scan's matched
regions and tap points drawn over it as an overlay. The event feed arrives
over SSE and reconnects on its own — a laptop that slept resumes from
`Last-Event-ID` rather than starting blank, as long as it was gone for less
than the 500-event ring.

### OCR autopilot

**Strategy → Effective Paths advisor** accepts a local JSON or normalized CSV
recommendation export. Download an example in the panel, replace its placeholder
data with your account's results, and import it. The panel separates health,
damage and economy paths and shows the source version, account, timestamps,
unknown values and missing inputs. Imports are stored per profile in local
`advisor.json`, which is excluded from git.

This adapter does not connect to Google Sheets, calculate Effective Paths formulas,
or accept arbitrary native workbook CSVs. Effective Paths currently excludes a
standard Workshop path; its lab and other recommendations remain advisory.
Workshop unlock prerequisites stay in your editable purchase plan. Use the
JSON or CSV example in the advisor panel as the import template.

Only recognized Workshop recommendations in coins with improving **displayed stat**
targets can be added to a Strategy draft. Both source and account data must be
less than 24 hours old with no reported missing inputs. A Workshop level is never
converted into a stat target. Required unlocks must already be observed or enabled
earlier in the draft. Adding a recommendation preserves existing priorities,
targets, disabled rows, budgets and arming. Save or Revert still controls the draft;
an import or draft addition never purchases an upgrade.

In **Strategy → Purchases**, choose Battle or Workshop, then Attack, Defense
or Utility. The catalog supplies upgrade names and aliases; OCR supplies your
observed values, prices and availability. Unknown means unseen, not locked.
You can plan unknown or locked upgrades in advance, but spending requires a
fresh, readable upgrade and currency balance. Targets refer to displayed stat
values, rather than purchase counts. Lower cooldown targets work in the opposite
direction for Shockwave Frequency and Wall Rebuild.

Choose Manual, Turtle or Health, adjust the ordered rules and targets, enable
battle autopilot, and Save. Turtle protects buffered Defense Absolute after
Defense %, builds early economy and advances Thorns breakpoints. Health moves
from early economy to health, lifesteal, attack speed, knockback and orbs.
These are editable guide presets; missing critical combat readings make them
wait. The Live page explains the current decision and verified purchases.

**Scan upgrades**, **Show category in game**, and **Buy once** are immediate
commands while an unpaused bot is in battle. Navigation verifies the category
on a later frame and searches with a finite scroll budget. Purchases wait for
a subsequent value, price or maxed-state change before continuing. Pause and
strategy edits retain outstanding purchase evidence to avoid duplicate taps.
Legacy template purchases remain available when autopilot is disabled.

Workshop spending requires shopping enabled and armed, a nonzero **coin budget
per visit**, enough coins above the **coin reserve**, and an enabled row.
Unlock tiles additionally require **Allow Workshop unlocks**. Cash Bonus and
Coin Bonus unlocks are separate rows. Confirmed unlocks appear as Unlocked;
unseen later unlocks can still be added by their exact OCR name. Shopping visits
take priority over starting the next battle. Existing card controls are unchanged.

The Live tier comparison uses completed farming runs and elapsed time; at least
three runs on a tier are required for a recommendation. Milestone runs are
tagged separately. Tier selection remains manual in game. Observations currently
live in memory, expire for purchase decisions and reset at battle boundaries;
profiles and completed run history persist.

Battle category navigation currently supports the verified three-tab layout at
1080×2400. Changed layouts or unreadable currency stop the action. Recorded-frame
and fake-device tests cover the executor; live-device calibration is still needed
for later unlocked panels. Labs, perks and automatic tier switching are outside
this implementation.

The **control** page is session concerns only: **Start**, **Stop bot**
(ends the bot, keeps the dashboard serving), **Shut down** (ends the bot and
the dashboard together, with a confirmation since there is no button to
bring it back), and **Pause** (still scanning, not tapping). It also shows a
read-only summary of the active strategy with a link to the **Strategy**
page, which is where the policy itself — what to buy, thresholds, timing —
is edited. Every tab converges on the current pause state live, over the
same SSE feed.

> **The dashboard has no authentication.** It serves a continuous MJPEG
> video stream of the device (`/api/frame`), not just JSON history and
> screenshots, plus this machine's entire event history, and its control
> page lets anyone who can reach the port pause the bot, start or stop it,
> shut down the whole dashboard process, rewrite what it buys, and create
> or delete strategy files — which is why it binds loopback. Since shopping,
> that includes flipping `armed` to `true` and reordering the between-runs
> buy list from the Strategy page, and this one is not like the others: a
> deleted strategy file can be rewritten from git or by hand, but gems and
> coins a visit already spent cannot be recovered by any means available to
> the player, ever. `--web-host` will let you bind something else, and the
> bot logs a warning when you do, but it will not stop you. Do not put it on
> a network without real auth in front of it.

Ctrl+C and `POST /api/shutdown` stop both the bot and the dashboard.
Reaching `--max-runs`, or stopping the bot from the dashboard, stops only the
bot — the dashboard keeps serving, with a **Start** button ready to run
another one.

## Where the data goes

Events are written to SQLite (`tower_bot.db` by default, `--db` to move it,
`--no-store` to turn it off entirely). Two tables: `runs`, one row per run with
its wave, coins, tier, scan count and tap count; and `events`, one row per
state change.

`ScanCompleted` is deliberately **not** stored — it fires every two seconds,
roughly 43,000 near-identical rows a day. The run row carries `scan_count`
instead. What is left is state changes only, and `EVENT_RETENTION_DAYS` (`30`)
of those stays small. Pruning runs once at startup.

Columns worth querying are typed; everything else on an event goes into a JSON
`detail` blob, so adding a field to an event needs no migration. The writer sink
owns the only write connection and the web layer opens the file read-only, so
the dashboard physically cannot corrupt the log.

A run left open by a killed process is closed out and marked `abandoned` on the
next startup.

### The ledger

The `events` table is pruned at `EVENT_RETENTION_DAYS` (30). The `ledger`
table is not — it is the account's permanent history of everything that
happens *outside* a run: workshop and card purchases, the rows it skipped
and why, shopping visits, policy changes, and each run's coin payout. In-run
upgrades never appear: they are bought with per-run cash that resets, which
is not account history. The dashboard serves it at **/ledger/**, newest
first, with the running coin and gem balance after every line.

Rehearsals (`shopping.enabled` without `armed`) are recorded but hidden
behind a toggle. They carry the price they would have paid and a delta of
zero, so they can never move a balance.

**Unexplained lines.** The bot can see the Workshop and the Cards page and
nothing else — not labs, lab slots, modules, relics, ultimate weapons, the
guild, the shop, or ad rewards. The community's own gem order puts lab slots
first, so the largest gem sink on this account is invisible to it. Rather
than let the running balance drift, the ledger compares every balance it
reads against what its own lines predict and writes the difference as an
`UNEXPLAINED` line: "−140 gems, balance moved outside the bot".

Two limits worth knowing. A gain and a loss of equal size between two
readings cancel out and produce no line — what is reported is the *net*
movement between observations. And the bot only reads a balance during a
shopping visit, so an `UNEXPLAINED` line is dated to the visit that
*revealed* the gap, not to when the spend actually happened.

## Configure

Add buttons in `config.py` → `ACTIONS`, one `Action(name, template, threshold)`
per button. All of them are evaluated top to bottom against the same captured
frame on every scan — matching is not exclusive, so several can fire in one
pass, in list order:

```python
ACTIONS: tuple[Action, ...] = (
    Action(name="Damage", template="upgrade_damage.png", threshold=0.9),
)
```

Use `--debug-scores` to see the best match score for every template and pick a
threshold just below the score of a real match (the default is `0.8`).
`CLICK_COOLDOWN_SECONDS` (`1.0`) keeps a slow UI animation from producing a
burst of taps on a button that was already pressed.

`TAP_JITTER_PX` (`8.0`), `TIMING_JITTER` (`0.15`) and `TAP_DELAY_SECONDS`
(`0.12`) are the shipped jitter defaults — what a fresh clone starts from.
Tune them per profile on the Strategy page rather than here; see "Jitter"
above for what each one does and why the cooldowns only stretch.

## Dependencies

Use uv rather than editing `pyproject.toml` by hand — it updates the lockfile
and the environment in one step:

```bash
uv add <package>
uv remove <package>
uv lock --upgrade      # refresh the locked versions
```

Commit `pyproject.toml` and `uv.lock` together so everyone resolves to the same
versions.

The dashboard source is the Next.js app in `web/ui/`. `./run.sh` builds its
ignored `web/static/` output when it is missing or stale, then starts the bot.
Node and npm are therefore required on a machine that runs the dashboard, but
the launcher installs the locked UI dependencies when they are absent.

## Tests

```bash
uv run pytest -q
```

No emulator, browser, or generated dashboard bundle is required — the suite
runs against committed fixtures. `./run.sh` verifies that its local dashboard
build matches the current frontend and backend sources; `npm test` in `web/ui`
runs the dashboard's own tests.
