"""Runtime configuration for the Tower bot.

Keep every tunable value here so the bot logic stays free of magic numbers.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

# --- ADB connection -------------------------------------------------------
ADB_HOST: str = "127.0.0.1"
ADB_PORT: int = 5037  # adb *server* port (not the device port)

# The emulator's ADB endpoint. Android Studio AVDs listen on <console_port + 1>,
# so the first running emulator (emulator-5554) is reachable at 127.0.0.1:5555.
DEVICE_HOST: str = "127.0.0.1"
DEVICE_PORT: int = 5555

# --- Loop timing ----------------------------------------------------------
SCAN_INTERVAL_SECONDS: float = 2.0
# Minimum delay between two clicks on the *same* template, so a slow UI
# animation does not cause a burst of taps on a button that is already pressed.
CLICK_COOLDOWN_SECONDS: float = 1.0

# --- Jitter ---------------------------------------------------------------
# Every tap is an `input tap` over ADB: no travel path, no dwell, and a
# pixel derived by fixed offset from a template match. Left alone, the bot
# taps the identical pixel every time and scans on a metronome. These three
# put variance back. Zero means off for all of them - see jitter.py.
#
# Radius, in pixels, around the computed tap point. 8 on a 1080-wide
# capture is ~0.7% of screen width; a real fingertip contact patch is
# 8-10mm, which is 60-80px at this pixel density, so this is far tighter
# than genuine human variance. The bound that matters is the other side:
# the price strip is only PRICE_REGION.h tall, and strategy.MAX_TAP_JITTER_PX
# derives its ceiling from that.
TAP_JITTER_PX: float = 8.0
# Fraction applied to the scan interval (±, symmetric) and to the two
# cooldowns (+ only - they are functional minimums, see jitter.stretch).
TIMING_JITTER: float = 0.15
# Mean pause between finding a match and sending its tap. Without it the
# tap goes out in the same breath as the scan that decided it.
TAP_DELAY_SECONDS: float = 0.12

# --- Vision ---------------------------------------------------------------
TEMPLATE_DIR: Path = Path(__file__).parent / "templates"
DEFAULT_THRESHOLD: float = 0.8

# cv2.TM_CCOEFF_NORMED normalises out mean and variance, so it is blind to
# brightness: a semi-transparent popup dimming the whole page still scores
# ~1.0 against a template cropped before the popup appeared. Guard against
# THAT by also requiring the matched region to be about as bright as the
# template. The value is a ratio of mean grey level; 0.0 disables the check.
#
# This guards overlays, not prices. It is verified only for a dimming
# overlay covering the whole screen (modal fade: 1.00 lit, 0.28 dimmed - see
# vision.brightness_ratio's docstring and test_vision.py). It does NOT detect
# a per-button "cannot afford" style: measured on menu_cards.png (x1
# affordable at 40 gems, x10 not) over the button's OWN matched template -
# CARD_BUTTONS, label and border only, not the price strip - the unaffordable
# button is DESATURATED (border saturation 45.5 vs 57.0 affordable) rather
# than dimmed - its mean grey level is actually HIGHER (59.7 vs 51.7
# affordable), the wrong direction for this ratio to catch. See
# test_the_unaffordable_card_button_is_desaturated_not_dimmed in
# tests/test_shopping_templates.py for a second measurement of the same
# finding over CARD_PRICE_REGION instead - different absolute numbers,
# because it is a different region, but the same direction. The 0.75
# default stays as-is: it still does its real job (rejecting a dimmed
# overlay on shopping rows), and digit-reading (Task 5b) is required for
# card and workshop affordability regardless.
DEFAULT_BRIGHTNESS_RATIO: float = 0.75


class Region(NamedTuple):
    """A rectangle expressed relative to a matched anchor's top-left.

    Never absolute. The death modal shifts ~46px vertically depending on
    whether the "New Highest Wave!" line is present, so a hardcoded y would
    read the wrong row half the time. dx/dy may be negative: the anchor is
    not always above-left of the number it locates.
    """

    dx: int
    dy: int
    w: int
    h: int


class Action(NamedTuple):
    """One template the bot looks for, in priority order."""

    name: str
    template: str  # file name inside TEMPLATE_DIR
    threshold: float = DEFAULT_THRESHOLD
    # Minimum matched-region brightness, as a fraction of the template's own.
    # Keeps the bot from tapping upgrades it cannot afford. 0.0 disables.
    brightness_ratio: float = DEFAULT_BRIGHTNESS_RATIO


# Evaluated top to bottom on every scan. Add rows as you capture more buttons.
# Templates crop the *label* only (not the value/price boxes), because those
# numbers change on every purchase and would break the match.
ACTIONS: tuple[Action, ...] = (
    Action(name="Attack Speed", template="upgrade_attack_speed.png", threshold=0.9),
    Action(name="Critical Chance", template="upgrade_critical_chance.png", threshold=0.9),
    Action(name="Damage", template="upgrade_damage.png", threshold=0.9),
    Action(name="Critical Factor", template="upgrade_critical_factor.png", threshold=0.9),
)


# --- Screen recognition -----------------------------------------------------
# Anchors are small crops unique to one screen. Measured separation on the
# golden fixtures: 1.000 on the correct screen, <=0.462 on every wrong one,
# so 0.8 has a wide margin in both directions.
ANCHOR_THRESHOLD: float = 0.8

# A transition is only declared after this many consecutive identical
# readings. Capture lands inside the death modal's fade animation (the same
# region measures 0.74 mid-fade and 0.28 fully dimmed), and without debounce
# those frames produce phantom transitions that corrupt run boundaries.
SCREEN_CONFIRMATIONS: int = 2

SCREEN_ANCHORS: dict[str, str] = {
    "MAIN_MENU": "screens/main_menu.png",
    "IN_RUN": "screens/in_run.png",
    "GAME_OVER": "screens/game_over.png",
}

# --- The run HUD's cash counter -------------------------------------------
# IN_RUN is detected by this, not by the upgrade panel's header bar. The
# header bar is per-tab - screens/in_run.png is the ATTACK crop, and the
# DEFENSE and UTILITY bars score 0.46 and 0.33 against it - so anchoring the
# screen state to it made two thirds of a run read as UNKNOWN.
#
# The cash counter is the opposite: identical on every tab, because it
# belongs to the top HUD rather than to the panel. Cash exists only inside a
# run, so its presence IS the run. Measured across every committed fixture:
# 1.000 on all in-run and game-over frames, at most 0.613 on any menu.
#
# GAME_OVER frames score 1.000 too - the death modal leaves the HUD visible
# behind it - so GAME_OVER keeps precedence in screens.classify.
RUN_CASH_ANCHOR: str = "screens/run_cash.png"

# Searched in this absolute box rather than full-frame: the counter is always
# top-left, and a full-frame match would find the "$" in an upgrade's price.
# Tall enough for both cutout geometries - see RUN_CASH_REFERENCE_Y.
RUN_CASH_SEARCH: Region = Region(dx=0, dy=0, w=400, h=400)

# The wallet box, measured from the cash counter's own match rather than from
# the IN_RUN panel anchor. That indirection is the point: the panel is pinned
# to the BOTTOM of the screen and the wallet to the TOP, and emulators
# reserve different amounts of the top for a display cutout, so the gap
# between them is not a constant. Measured: an Android Studio AVD
# (Pixel-class, cutout) puts the counter at y=171 and BlueStacks (no cutout)
# at y=35 - same resolution, same templates at 1.000, 136px apart. Anchoring
# the wallet to the counter cancels that difference out on any emulator
# instead of encoding one of them.
WALLET_FROM_CASH: Region = Region(dx=-7, dy=-9, w=230, h=72)

# The gem counter, two HUD rows below the cash one and anchored to the same
# match for the same reason. Present only once the account HOLDS gems - the
# row is absent from in_run_lit.png and in_run_wallet.png, which is why a
# read from here has to survive finding nothing.
#
# Starts AFTER the gem icon, unlike WALLET_FROM_CASH which starts before the
# "$". That asymmetry is not a slip: "$" is a glyph in the wallet atlas and
# the diamond is not, and one unrecognised glyph fails the whole read by
# design - so a region framed like the cash row's reads None, never 60.
GEMS_FROM_CASH: Region = Region(dx=60, dy=149, w=170, h=72)

# --- The floating gem ------------------------------------------------------
# The free gem that orbits the tower mid-battle. Tapping it pays two gems;
# it spawns roughly once per 400-500 waves and at most once per 15 minutes.
#
# The battle field, measured from the cash counter so it moves with the HUD
# on an emulator with a display cutout (see WALLET_FROM_CASH). Bounded rather
# than full-frame because the two things on screen that share the gem's exact
# colour are BOTH outside it: the HUD gem counter above (dy=+149) and the
# ad-gem button below (dy=+1175). A full-frame search would find one of them
# on every single scan.
FLOATING_GEM_SEARCH: Region = Region(dx=200, dy=260, w=680, h=720)

# Measured off the HUD gem icon in in_run_early.png, which is the same
# artwork at a smaller size: hue 151 with a 2nd-98th percentile spread of
# 149-152, saturation ~198, value ~144. The window is widened to 145-158 for
# the glow's edges. The red enemy diamonds - same shape, same size, nearly
# the same brightness - sit at hue 177, so colour is the only thing that
# separates them and this window is where that separation lives.
FLOATING_GEM_HSV_LOW: tuple[int, int, int] = (145, 110, 90)
FLOATING_GEM_HSV_HIGH: tuple[int, int, int] = (158, 255, 255)

# The sprite trails small magenta particles of the same hue. PROVISIONAL:
# derived from the HUD icon's 387 glow pixels at 52x48 and the sprite being
# visibly larger in a battle frame. A harvested capture should confirm it.
FLOATING_GEM_MIN_AREA: int = 250

# How many scans a tap gets to show up in the gem counter before the claim
# is written off as unconfirmed. Three, at a ~2s interval, is about six
# seconds - generous for a counter that updates on the next frame, and the
# generosity is deliberate: giving up early publishes ClaimUncertain for a
# gem that was in fact collected.
FLOATING_GEM_CONFIRM_SCANS: int = 3

# Taps per sighting. More than one because the gem ORBITS: the tap lands
# where the sprite was a moment ago, so a miss is ordinary. Bounded because
# a false positive would otherwise be tapped forever.
FLOATING_GEM_MAX_TAPS: int = 3

# How long to leave the field alone after a claim that never confirmed.
# Without it the budget above buys nothing: the machine goes back to idle,
# sees the same unmoving blob on the very next scan, and starts over -
# tapping a false positive forever, three taps at a time.
#
# Only after a FAILURE. A confirmed claim resets immediately, because the
# counter rising is proof the thing was real and the next spawn deserves
# the same treatment.
FLOATING_GEM_COOLDOWN_SECONDS: float = 30.0

# How many consecutive scans a menu page may hold every action while
# nothing is walking, before the loop stops treating it as a transaction's
# frame and lets navigation try to recover.
#
# Ten rather than two or three: a claim walk arms on the main menu and takes
# several scans to reach its ladder, and the gap between "the page reader
# sees the page" and "the walk is armed" is a legitimate hold that must
# never be raced. At a ~2s interval this is roughly twenty seconds of
# genuinely stuck before anything changes course.
HELD_PAGE_SCAN_LIMIT: int = 10

# --- Unknown-screen snapshots ---------------------------------------------
UNKNOWN_DIR: Path = Path(__file__).parent / "unknown"
UNKNOWN_MIN_INTERVAL: float = 30.0
UNKNOWN_KEEP: int = 50

# How far two 64-bit difference hashes may sit apart and still be called the
# same screen. The rate limit alone does not stop a bot parked on one
# unmodelled screen from spending all fifty slots on it, so frames are also
# compared against what is already kept.
#
# Measured over every committed fixture (820 pairs, median 26 bits apart):
# the same screen redrawn stays within 8 - a fade scores 3, the death modal
# shifted 46px by its "New Highest Wave!" line scores 7, a scrolled missions
# list scores 8. The nearest pair that is genuinely two different screens is
# the main menu against the milestones page, at 11. There is no empty gap
# between the two bands, so this is a chosen tradeoff and not a natural
# boundary: 8 keeps a 3-bit margin under the first wrong answer, and errs
# towards saving a duplicate rather than dropping a screen nobody has seen.
UNKNOWN_HASH_DISTANCE: int = 8

# --- Resolution guard ------------------------------------------------------
# Templates are not scale-invariant. A different emulator resolution
# invalidates every one of them.
EXPECTED_RESOLUTION: tuple[int, int] = (1080, 2400)

# --- Auto-navigation ------------------------------------------------------
# Buttons are located by template match, never by fixed coordinates: the
# death modal shifts ~46px vertically depending on whether the
# "New Highest Wave!" line is present.
NAVIGATION_COOLDOWN_SECONDS: float = 3.0
#
# A tuple of candidates per screen, tried in order, because one screen can
# draw one slot two ways. MAIN_MENU is the case that forced it: a run
# abandoned mid-battle - a kill, a crash, a pause never returned from -
# leaves the menu offering RESUME BATTLE in the same 514x164 box BATTLE
# normally occupies. Measured on tests/fixtures/main_menu_resume.png,
# buttons/battle.png scores 0.535 against those glyphs, so the single-entry
# version declined silently and parked the bot on a recognised MAIN_MENU
# with nothing on the feed but `screen is MAIN_MENU`.
#
# The two crops are separate targets rather than one loose template on
# purpose: RESUME continues the run the bot lost, BATTLE starts a fresh one,
# and the feed and ledger are entitled to know which happened. The 2x2 says
# they stay apart - each template scores 1.000 on its own fixture and ~0.53
# on the other, either side of Navigator's 0.8 threshold.
NAV_BUTTONS: dict[str, tuple[tuple[str, str], ...]] = {
    "GAME_OVER": (("RETRY", "buttons/retry.png"),),
    "MAIN_MENU": (
        ("BATTLE", "buttons/battle.png"),
        ("RESUME_BATTLE", "buttons/resume_battle.png"),
    ),
}

# Tapped instead of NAV_BUTTONS["GAME_OVER"] when a Workshop visit is due.
# RETRY starts the next run from the death screen, so a bot left to it loops
# IN_RUN -> GAME_OVER -> IN_RUN and never touches MAIN_MENU - the one screen
# ShoppingSession.begin() is offered, and so the one way into the Workshop.
# HOME sits beside RETRY on the same screen and lands there directly.
GAME_OVER_HOME: tuple[str, str] = ("HOME", "buttons/home.png")

# The way off a menu page, keyed by PAGE_ANCHORS name rather than by
# ScreenState. NAV_BUTTONS cannot carry these: ScreenState models the run
# lifecycle only, so every menu page reads UNKNOWN to it (see pages.py), and
# UNKNOWN is the one key that must NOT tap - a screen nobody modelled has no
# known exit to aim at. The page name is the evidence that this frame does.
#
# Both land on the bottom tab bar's Battle tab, which is the same control
# shopping.py's RETURN step taps to end a visit.
#
# MISSIONS and the MILESTONES screens are deliberately absent. Their passive
# readers own the frame and return from the scan before the navigation block
# is reached, and their transactions tap MISSIONS_RETURN themselves - an
# entry here would be a second hand on the same wheel.
MENU_NAV_BUTTONS: dict[str, tuple[str, str]] = {
    "WORKSHOP": ("BATTLE_TAB", "nav/tab_battle.png"),
    "CARDS": ("BATTLE_TAB", "nav/tab_battle.png"),
}

# --- Digit reading --------------------------------------------------------
# Numbers are light glyphs on a dark panel. Binarise, split by column gaps,
# match each glyph against a per-size-class atlas.
ATLAS_DIR: Path = TEMPLATE_DIR / "atlas"

# Grey level above which a pixel counts as glyph rather than background.
DIGIT_BINARY_THRESHOLD: int = 140

# Per-size-class overrides, and empty is the correct state today rather than
# an oversight. Every class still read through the atlas - the in-run, modal
# and menu-price numbers - is light glyphs on a dark panel, which is exactly
# what the 140 default is for, and it segments all of them correctly on every
# committed fixture.
#
# The one class that ever needed an override was `header`, the menu's coin
# and gem bar: white text on a light purple ground, where at 140 the bar
# survived binarisation and bridged adjacent glyphs ("1.77K" segmenting as
# 1 . 77 K). Those balances are read by OCR now, so the class is gone and its
# 200 with it. The hook stays for the next class that renders inverted.
DIGIT_BINARY_THRESHOLDS: dict[str, int] = {}

# A glyph must match an atlas entry at least this well to be accepted.
GLYPH_MATCH_THRESHOLD: float = 0.7
# Narrowest run of lit columns still treated as a glyph. The decimal point is
# the narrowest real glyph, so raising this silently turns 1.5K into 15K.
GLYPH_MIN_WIDTH: int = 2

# Every region is relative to a matched anchor - see config.Region.
# Measured on a 1080x2400 capture; re-measure if the resolution ever changes.

# The in-run HUD, from the IN_RUN anchor at (12, 1646). Wide enough for the
# wallet to grow into "$ 12.34K" without clipping.
WALLET_REGION: Region = Region(dx=13, dy=-1484, w=230, h=72)

# The price box under an upgrade, from that upgrade's own matched LABEL - not
# from a screen anchor, because each of the four buttons has its own box. The
# same offset lands correctly on all four; verified against every one.
# Inset from the box border so a brighter theme cannot smear the projection.
PRICE_REGION: Region = Region(dx=252, dy=98, w=208, h=38)


def buy_point(anchor: tuple[int, int]) -> tuple[int, int]:
    """Where to tap to BUY the upgrade whose label matched at `anchor`.

    NOT the label's own centre. The label is itself a button - it opens an
    info panel that covers the screen - so a tap there buys nothing and
    blinds the next scan behind the overlay. The buy button is the square to
    the label's right.

    The point is derived from PRICE_REGION rather than measured separately:
    that offset already locates the price strip inside the buy square, from
    this same anchor, for all four upgrades. One calibration to keep correct
    instead of two that can drift apart.
    """
    return (
        anchor[0] + PRICE_REGION.dx + PRICE_REGION.w // 2,
        anchor[1] + PRICE_REGION.dy + PRICE_REGION.h // 2,
    )


# Death modal, from the GAME_OVER anchor at (330, 663). These crop the whole
# CENTRED line, not just its number: "Wave 1" grows to "Wave 137" and the
# digits shift left as it does, so a crop tight to the number would slide off
# it. Reading them therefore needs the label glyphs in the atlas too.
# "Wave N" sits directly under the title in both layouts, so it holds a fixed
# offset from the anchor. It crops the whole centred line, caption included.
MODAL_WAVE_REGION: Region = Region(dx=6, dy=111, w=408, h=54)

# Tier and coins do NOT hold a fixed offset. When the run beats its record the
# modal grows a green "New Highest Wave!" line between Wave and Tier, and
# everything below it moves - while the modal's top edge moves the other way,
# because a taller modal re-centres. Measured: the anchor rises 49px and these
# two lines fall 49px, so a fixed offset reads the wrong row on every record run.
#
# So they are located by their own caption, the same way navigate.py finds
# buttons. Verified against both layouts.
MODAL_TIER_CAPTION: str = "modal/tier_caption.png"
MODAL_COINS_CAPTION: str = "modal/coins_caption.png"

# From the "Tier" caption's top-left. The number trails the word at a constant
# gap, so this stays right however wide the number gets.
MODAL_TIER_REGION: Region = Region(dx=110, dy=-4, w=150, h=60)

# From the "coins earned" caption's top-left. The value is CENTRED under the
# caption, so this spans the full column rather than hugging the digits.
MODAL_COINS_REGION: Region = Region(dx=-30, dy=52, w=250, h=68)

# --- Menu navigation ------------------------------------------------------
# Everything here is located by template match and tapped at the match centre,
# never by fixed coordinates - the same rule the death modal forced on us.
NAV_TARGETS: dict[str, str] = {
    "MISSIONS": "nav/missions.png",          # top-right of the main menu
    "WORKSHOP": "nav/tab_workshop.png",      # bottom tab bar
    "CARDS": "nav/tab_cards.png",            # bottom tab bar
    "BATTLE_TAB": "nav/tab_battle.png",      # bottom tab bar - back to the menu
    "MISSIONS_RETURN": "nav/missions_return.png",  # missions has no tab; this exits
    "MILESTONES": "nav/milestones.png",      # main menu, cut TIGHT on the label - see
                                              # its own comment in the templates dir
    # Milestones has no tab either, and shares the exact same "Tap To Return
    # To Game" bar missions does - measured at 1.0000 on both ladder captures,
    # same file as MISSIONS_RETURN above, not a coincidence: it is the same
    # control drawn on a different page.
    "MILESTONES_RETURN": "nav/missions_return.png",
}

# First-visit popups sit between a tab and its page. Cards showed a two-step
# chain: an intro dialog with "Claim", then a full-screen "40 GEMS" reward with
# CLAIM and SKIP. Try these in order until none match, then read the page.
NAV_DISMISS: tuple[str, ...] = (
    "nav/claim.png",
    "nav/claim_reward.png",
    "nav/skip.png",
)

# --- Persistence ----------------------------------------------------------
DB_PATH: Path = Path(__file__).parent / "tower_bot.db"

# One JSON file per named strategy, plus a `.active` pointer. Committed, not
# gitignored: a strategy is a decision worth reviewing in a diff, and a fresh
# clone should start from the same defaults everyone else has.
STRATEGY_DIR: Path = Path(__file__).parent / "strategies"

# Events older than this are deleted at startup. ScanCompleted is never
# stored - it fires every 2s, roughly 43,000 near-identical rows a day - so
# what remains is state changes only, and 30 days of those stays small.
EVENT_RETENTION_DAYS: int = 30

# Which menu page is on screen. Deliberately SEPARATE from SCREEN_ANCHORS:
# ScreenState models the run lifecycle only, and classify() does
# ScreenState(winner), which would raise on a name the enum does not have.
PAGE_ANCHORS: dict[str, str] = {
    "MAIN_MENU": "screens/main_menu.png",
    "WORKSHOP": "screens/workshop.png",
    "CARDS": "screens/cards.png",
    "MISSIONS": "screens/missions.png",
}

# The coin and gem counters in the menu header bar. Identical pixels on every
# menu page, but each page's ANCHOR sits somewhere different, so the offset
# is per page rather than one shared pair. Measured on the committed
# fixtures: MAIN_MENU anchors at (336, 360), WORKSHOP at (32, 244), CARDS at
# (32, 248), against a header at absolute (85, 152) and (430, 152).
#
# The boxes are wider than today's values need. The coin counter grows from
# "78" through "1.77K" to "1.23M" without moving its left edge, so the room
# has to be on the right, and a clipped glyph fails the whole read.
HEADER_REGIONS: dict[str, tuple[Region, Region]] = {
    "MAIN_MENU": (
        Region(dx=-251, dy=-208, w=170, h=68),
        Region(dx=94, dy=-208, w=190, h=68),
    ),
    "WORKSHOP": (
        Region(dx=53, dy=-92, w=170, h=68),
        Region(dx=398, dy=-92, w=190, h=68),
    ),
    "CARDS": (
        Region(dx=53, dy=-96, w=170, h=68),
        Region(dx=398, dy=-96, w=190, h=68),
    ),
}

# Where to tap to close an upgrade's info panel.
#
# Tapping a row's LABEL opens a panel describing it ("Damage / Damage each
# Projectile deals to enemies / Current Level 1 / Max Level 6000") over the
# tile grid, dimming the page behind it. The page still classifies as
# WORKSHOP, but find_tiles goes to zero and the bot is blind until it
# closes. It closes on a tap anywhere outside itself.
#
# Measured on the page TITLE row, not in the empty space below the grid.
# Both dismiss it, but the space below the grid is only empty until enough
# rows unlock to fill it, and a dismiss tap that lands on a price box would
# buy something nobody asked for. The title sits above the first tile on
# every tab, whatever is unlocked. Verified live: tiles 0 -> 7, coins
# unchanged.
PANEL_DISMISS_POINT: tuple[int, int] = (180, 265)

# --- Menu shopping ---------------------------------------------------------
# The workshop's category tabs. Cut UNSELECTED: a selected tab is brighter,
# and a template cropped lit matches only its own selected state.
WORKSHOP_TABS: dict[str, str] = {
    "ATTACK": "workshop/tab_attack.png",
    "DEFENSE": "workshop/tab_defense.png",
    "UTILITY": "workshop/tab_utility.png",
}

# Shipped buy order. Taken from the community consensus (see the dashboard's
# Guide page) and cut down to rows this account can actually SEE: everything
# the guides name first - Cash/Wave, Coins/Wave, Def Abs, Def%, Thorns,
# Coins/Kill - is behind one of the three unlock tiles, which is why they
# lead. Together they cost 165 coins. Defence (Health, Health Regen) comes
# before attack for the same reason the guides give it: tier 1 is a "turtle
# build" - make the tower unhittable before making it hit harder.
#
# The crit rows ship disabled. They are here so there is a row to switch on,
# which is the same reason `enabled` exists at all - the community is
# unanimous that crit costs more and scales slower than everything above it
# this early.
#
# (name, category, enabled) triples only - a row is found by matching its
# name against what tiles.read_rows sees on the page, not by a template or a
# measured layout (spec §7's AMENDMENT), so there is nothing else to repeat
# here.
SHOPPING_ROWS: tuple[tuple[str, str, bool], ...] = (
    ("Unlock Cash Bonuses", "UTILITY", True),
    ("Unlock Defense Upgrades", "DEFENSE", True),
    ("Unlock Range Upgrades", "ATTACK", True),
    ("Health", "DEFENSE", True),
    ("Health Regen", "DEFENSE", True),
    ("Damage", "ATTACK", True),
    ("Attack Speed", "ATTACK", True),
    ("Critical Chance", "ATTACK", False),
    ("Critical Factor", "ATTACK", False),
)

# Buy buttons on the Cards page. Cropped to the "x1"/"x10" quantity label and
# border only - not the price or gem icon, which live in CARD_PRICE_REGION.
# A buy button carries no row name to read, unlike a workshop row, so it
# stays template-matched rather than addressed by name.
CARD_BUTTONS: dict[str, str] = {
    "x1": "cards/buy_x1.png",
    "x10": "cards/buy_x10.png",
}

# From the matched buy button's own top-left (see CARD_BUTTONS). The
# currency icon is INSIDE this region deliberately: the number is
# right-aligned against the icon and grows leftward, so trimming the icon off
# the right would clip a longer price from the left. The reader is taught to
# ignore the icon instead - see the `menu` size class.
CARD_PRICE_REGION: Region = Region(dx=110, dy=29, w=210, h=55)

# --- Live feed ------------------------------------------------------------
# How much history the SSE ring holds. A reconnecting browser replays from
# here using Last-Event-ID, so this is also how long a laptop can sleep
# before the feed has a hole in it.
SSE_RING_SIZE: int = 500
# How often the stream endpoint looks for new events. The scan interval is 2s,
# so a quarter of a second is already imperceptible.
SSE_POLL_SECONDS: float = 0.25
# An idle stream sends a comment this often so proxies and browsers do not
# decide the connection died.
SSE_HEARTBEAT_SECONDS: float = 15.0
# How often the MJPEG stream checks whether a new frame has been published.
# Same reasoning as SSE_POLL_SECONDS: a quarter second against a 2s scan
# interval is imperceptible, and frame_stream only actually sends when the
# frame number has moved, so a faster poll here would not deliver frames any
# sooner anyway.
FRAME_POLL_SECONDS: float = 0.25

# --- Web dashboard --------------------------------------------------------
# SECURITY: loopback only, and there is no auth. The dashboard serves
# screenshots of a live session and the full event history of this machine.
# Binding 0.0.0.0 puts both on the local network in the clear - do not change
# this without putting real authentication in front of it first.
# Since the control plane landed this server is no longer read-only: anything
# that can reach it can start and stop the bot, change what it buys, end the
# process, and create or delete strategy files under strategies/. That last
# one is a write path onto the disk, not just a knob on a running loop.
# Loopback is doing real work here, not just avoiding an open port.
WEB_HOST: str = "127.0.0.1"
WEB_PORT: int = 8765

# --- Upgrade tiles --------------------------------------------------------
# An absolute rectangle on the frame. Not Region, which is anchor-relative
# (dx, dy): a Rect says where something IS, a Region says where to look
# relative to something already found. Both ocr.py and tiles.py need this,
# which is why it lives here rather than in either of them.
class Rect(NamedTuple):
    x: int
    y: int
    w: int
    h: int


# A tile is a bright bordered rectangle: a half-width upgrade tile, or a
# full-width unlock tile. Both the workshop and the in-run panel use them.
#
# Measured with tools/tile_preview.py --all against all four committed
# fixtures (menu_workshop_attack/defense/utility.png, in_run_lit.png). Real
# tiles are a tight, consistent pair of sizes: half-width upgrade tiles are
# 503x196, the full-width unlock tile is 1020x196 - so every real tile seen
# is exactly 196 tall, regardless of page or width.
#
# Everything RETR_EXTERNAL returns alongside them is either much smaller
# (glyphs, icons: w<=108, h<=94) or falls in a gap around that 196:
#   - menu bottom nav bar: ~267-807 wide, 99-106 tall
#   - in_run_lit.png's HUD panels above the upgrade area (the fixture
#     called out as likely trouble, since its background is a live battle
#     rather than a flat menu): 519 wide, 158-160 tall - close enough in
#     WIDTH to a real tile that width alone can't reject it, but still short
#     of the 196 a real tile measures
#   - in_run_lit.png's full-width top HUD bar: 1080 wide (the screen width),
#     100 tall
# So height alone separates them without needing a crop region: the widest
# gap below a real tile's 196 is above the HUD panels' 160, and TILE_MIN_H
# sits at the middle of that gap (178), the same convention used elsewhere
# in this file (see DIGIT_BINARY_THRESHOLDS) for picking the middle of a
# measured plateau rather than its edge. TILE_MAX_H (230) leaves comfortable
# headroom above 196 without reaching any measured false candidate.
#
# TILE_MIN_W (450) and TILE_MAX_W (1050) sit just outside the measured real
# widths (503 and 1020): nothing false was measured between 503 and 1020, and
# the nearest false width above 1020 is the in_run_lit.png top HUD bar at
# 1080, so 1050 sits in that gap. Width is the weaker filter here - height is
# what actually rejects the HUD panels - but it still rejects the smaller
# glyph/icon contours and stays wide, as the bounds should: they exist to
# reject the value and price panels nested inside a tile (and now, measured,
# a couple of same-size-range HUD elements), not to pin a layout a game
# update may nudge.
#
# TILE_BINARY_THRESHOLD (100) was swept from 40 to 180 across all four
# fixtures: the detected tile count did not move at any point in that range,
# so 100 sits well inside a stable plateau rather than on an edge - a tile's
# border is bright and its surroundings are dark enough that where exactly
# the cut falls barely matters.
TILE_BINARY_THRESHOLD: int = 100
TILE_MIN_W: int = 450
TILE_MAX_W: int = 1050
TILE_MIN_H: int = 178
TILE_MAX_H: int = 230

# Where the price sits within a tile, as a fraction of tile height. A tile
# holds a stat value panel above a price panel, and both parse as numbers -
# so position is what separates "3" (Damage's level) from "30" (its price).
#
# This bounds a box TOP: tiles.py compares box.rect.y, so tops are the only
# edge that enters the predicate and the only edge worth measuring. (An
# earlier revision of this comment justified its number against stat-value
# BOTTOMS, which the code never looks at.)
#
# Measured with tools/tile_preview.py plus the recorded OCR boxes (see
# tests/fixtures/ocr/*.json) against all four committed fixtures; every real
# tile is 196px tall. The two layouts put the price at very different
# heights - a half-width upgrade tile has a separate value box well above
# its price (row tiles: price top 0.638-0.709), while a full-width unlock
# tile centres its price directly under the label with no value box at all
# (unlock tiles: price top 0.536-0.551) - so the number that bounds this
# fraction from above is the unlock tiles' low end, not the row tiles'.
#
# The feasible interval is therefore (highest NUMERIC stat-value top,
# lowest price top]:
#   - highest numeric stat-value top: 0.286, the "5" on
#     menu_workshop_defense.png. ("0.00/sec", "1.00%" and "x1.20" sit at
#     0.255-0.27 but never enter this comparison at all, since parse_number
#     already refuses them; the highest that does parse besides the "5" is
#     "3" at 0.281 and "1.00" at 0.26-0.27.)
#   - lowest price top: 0.536, Unlock Cash Bonuses on
#     menu_workshop_utility.png.
# 0.41 is the middle of that (0.286, 0.536] gap, the same
# pick-the-middle-of-the-plateau convention TILE_MIN_H and
# DIGIT_BINARY_THRESHOLDS use, and leaves ~24px of margin on each side of a
# 196px tile. The two previous guesses were both measured against the wrong
# edge: 0.55 fell ABOVE the unlock price top and would have rejected the
# unlock tiles' own price outright, and 0.51 left only ~5px before doing the
# same.
TILE_PRICE_TOP_FRACTION: float = 0.41

# --- OCR -------------------------------------------------------------------
# Boxes below this are dropped inside ocr.py and never reach a consumer.
# Measured on the committed fixtures: real content read at 0.968-1.000 and
# the one observed phantom - an 'A' on empty screen - at 0.555. This sits in
# the middle of that gap, the same pick-the-middle-of-the-plateau convention
# DIGIT_BINARY_THRESHOLD uses. Calibrated on four images; revisit against
# live data.
OCR_CONFIDENCE_FLOOR: float = 0.85


# --- In-battle game speed -------------------------------------------------
# The widget sits at the bottom right of the play area: [-] x1.0 [+]. All
# three parts are anchor-relative, like every other in-run region - the
# IN_RUN anchor's top-left is (12, 1646) on tests/fixtures/in_run_lit.png,
# which is where these offsets were measured.
#
# The readout is matched inside SPEED_READOUT_REGION rather than full-frame,
# and that is not an optimisation: "x1.00" (coins multiplier) and "x1.20"
# (critical factor) are both drawn elsewhere on the same screen, so a
# full-frame match for "x1.0" would happily find the wrong one.
SPEED_MINUS_REGION: Region = Region(dx=664, dy=-265, w=65, h=66)
SPEED_PLUS_REGION: Region = Region(dx=864, dy=-265, w=65, h=66)
# Deliberately wider than the "x1.0" glyph block it currently holds, so a
# longer label ("x10.0") still fits without re-measuring.
SPEED_READOUT_REGION: Region = Region(dx=733, dy=-255, w=126, h=50)

# Every speed the arrows step through, in ascending order, each with a
# readout template at templates/speed/<label>.png. Harvested off a live run
# by tools/harvest_speed_glyphs.py, which is also how this list grows: a
# value here without a template is a crash the first time the bot reads the
# widget, so never add one by hand.
#
# x1.5 is the ceiling on the account this was harvested from, not a ceiling
# in the game - higher speeds unlock with progression. Re-run the harvest
# after unlocking one and the tuple grows.
SPEED_VALUES: tuple[float, ...] = (0.0, 1.0, 1.5)

# The subset a strategy may aim for. x0.0 is deliberately excluded, and the
# asymmetry with SPEED_VALUES above is the whole point.
#
# The bottom step of the widget does not slow the game, it STOPS it. That has
# to be readable - a bot that read None at x0.0 could never climb out, since
# decide() refuses to act on a widget it cannot read - but it must not be
# holdable: a strategy pinned to x0.0 is a soft hang. No cash accrues, no
# upgrade becomes affordable, the run never ends, max_runs is never reached,
# and the dashboard reports "running" the entire time.
TARGET_SPEEDS: tuple[float, ...] = tuple(v for v in SPEED_VALUES if v > 0.0)

# A readout must match its template at least this well to be believed. Higher
# than DEFAULT_THRESHOLD: the labels differ by a single glyph, so a loose
# threshold reads x1.0 as x4.0 rather than failing honestly.
SPEED_MATCH_THRESHOLD: float = 0.9


def speed_template(value: float) -> str:
    """Template path for one speed value. One place builds this name."""
    return f"speed/x{value:.1f}.png"
