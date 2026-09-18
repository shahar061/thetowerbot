"""Evidence-bounded readers for recorded English upgrade screens.

The recorded 1080x2400 Workshop grids, battle Attack grid, Daily Missions
page and Cards page provide geometry evidence. Existing battle Defense/Utility readers remain
supported, but lack recorded coverage. This is not every game screen or unlock
stage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import ocr
import tiles
from geometry import anchored_y, supported_frame
from device import Image


_RECORDED_READERS = (
    ('workshop.attack', 'menu_workshop_attack'),
    ('workshop.defense', 'menu_workshop_defense'),
    ('workshop.utility', 'menu_workshop_utility'),
    ('battle.attack', 'in_run_lit'),
    ('battle.defense', 'in_run_defense'),
    ('battle.utility', 'in_run_utility'),
    ('missions.daily', 'menu_missions'),
    ('cards.inventory', 'menu_cards'),
)

# Screens read at two genuinely different points in an account's life: the
# rows on the second capture are not the rows on the first, which is what
# makes the screen id a property of the page rather than of one save file.
#
# Only the Workshop tabs qualify, and the bar is deliberately this high.
# in_run_early parses to the same four rows, prices and targets as in_run_lit;
# stats_summary_early and stats_tiers_early parse identically to their
# originals; menu_missions_weekly differs from menu_missions by a daily
# rotation, which is not an unlock. Those captures are still evidence - see
# _RECORDED_READERS, where two of them give battle.defense and battle.utility
# their first recorded layout - but they are not stage evidence, and listing
# them here would make this table mean nothing.
_RECORDED_UNLOCK_STAGES = (
    ('workshop.attack', ('menu_workshop_attack', 'menu_workshop_attack_early')),
    ('workshop.defense', ('menu_workshop_defense', 'menu_workshop_defense_early')),
    ('workshop.utility', ('menu_workshop_utility', 'menu_workshop_utility_early')),
)

# Rows the game shows and the catalog cannot name. perception already refuses
# them a tap and reports them as `discovered:<label>`; listing them turns that
# silent refusal into visible, ownable work. Extending the catalog is a
# purchasing change and belongs to the catalog task, not to screen discovery.
#
# Empty, and checked to be: the catalog now names every row in every recorded
# capture. It held attackrange, unlockmultishotupgrades and unlockthornupgrades
# until those three names were added to the catalog as aliases - which is the
# whole point of the table, so it is kept rather than deleted.
_UNCATALOGUED_LABELS: tuple[tuple[str, str], ...] = ()

# The Ultimate Upgrades page IS recorded, but only at its locked stage: the
# capture is an account whose UW system is still shut behind tournaments, so
# it carries an unlock offer and a prerequisite and not one owned weapon.
# Everything only an owned weapon could show is therefore still unread.
# ultimate_weapons.py reads this table instead of keeping its own copy, so a
# reader and the matrix describing it cannot drift apart.
ULTIMATE_WEAPON_GAPS = {
    'ultimate_weapon_owned_page_layout': 'B08',
    'ultimate_weapon_stone_upgrade_rows': 'U02',
    'ultimate_weapon_toggles_and_cooldowns': 'U03',
    'uw_plus_system_state': 'U04',
}

# Replay coverage, declared here so the support matrix and the fixture set
# cannot drift apart. A capability is `covered` when a positive, an
# unavailable and an ambiguous example of it all exist and are replayed by
# tests/test_fixtures.py; `_REPLAY_GAPS` names the ones that do not, with the
# task that owns closing them. Listing a capability as enabled while nothing
# has ever shown what it does on a state it must not act on is the gap this
# table exists to make visible.
_REPLAY_MANIFEST = 'tests/fixtures/replay/manifest.json'
_ACCOUNT_SNAPSHOTS = 'tests/fixtures/account'
_REPLAY_COVERED = (
    'workshop.attack', 'workshop.defense', 'workshop.utility',
    'battle.attack', 'battle.defense', 'battle.utility',
    'missions.daily', 'cards.inventory',
    'account.settings', 'account.stats.summary',
    'game_over.result',
)
# game_over.py reads the death modal directly rather than through discover():
# the modal is a centred overlay with no upgrade-grid heading to validate,
# and every field on it is found by its own caption rather than a row a
# purchase loop could act on. Kept in its own matrix key rather than
# 'readers' because it is not the grid adapter those entries name, not
# because it is any less enabled or any less proven.
_GAME_OVER = ('game_over.result',)
# Even a progressed v29.0.2 history table draws literal zeros for tiers with
# no recorded wave. Nothing on that screen distinguishes no history, a lock,
# and a trusted numeric zero, so the unavailable-state example remains owned.
_REPLAY_GAPS = {'account.stats.tiers.unavailable': 'B08'}

# What remains out of scope, and who owns it. An entry with no owner is a
# standing property of the design rather than work someone will pick up.
_UNSUPPORTED_OWNERS = {
    # A stocked v29.0.2 Cards page now shows identity rows and presets, but
    # the current reader refuses its changed heading geometry. The captures
    # are study material, not evidence that those rows are actionable.
    'card_identity_rows': 'B08',
    'card_mastery_ownership': 'B08',
    'card_presets': 'B08',
    'in_run_card_locks': 'B08',
    # A different kind of gap: the pixels are not the problem. Registry v1
    # files all 31 cards under one kind, so passive bonuses and active
    # abilities are indistinguishable by identity. Splitting them is a catalog
    # change, like any other uncatalogued label above.
    'card_passive_or_active_classification': 'B02',
    'card_slot_purchase_actions': 'C02',
    'later_unlock_stage_layouts_outside_workshop': 'B08',
    'battle_history_export': 'B08',
    'native_stat_export': 'B08',
    # An active v29.0.2 Labs page is recorded, but no Labs discovery context
    # or second layout is validated. Recording a Rush price does not enable a
    # gem spend; starting and accelerating research have separate owners.
    'labs_screen_layout': 'B08',
    'labs_research_actions': 'L02',
    'labs_acceleration_spend': 'L03',
    'missions_claim_actions': 'T01',
    'missions_reward_currency': 'T01',
    'missions_milestone_claim_state': 'T01',
    'missions_beyond_the_recorded_strip': 'T01',
    # A stocked v29.0.2 Modules page is recorded, but no Modules discovery
    # context or banner/pity state is validated. Inventory and spend actions
    # stay disabled until their own readers are proved.
    'modules_screen_layout': 'B08',
    'modules_banner_pity_state': 'B08',
    'module_upgrade_merge_actions': 'C04',
    'other_locales': 'V06',
    'other_resolutions': 'V06',
    'unknown_overlays': 'by_design',
    **ULTIMATE_WEAPON_GAPS,
}

# Three independent anchors measured on the recorded Daily Missions capture:
# the page title at (32, 249, 433, 40), the WEEKLY CHALLENGE banner at
# (276, 378, 527, 40) and the "N/M Missions" band at (27, 653, 287, 43). One
# anchor could be a coincidence; the page is claimed only when all three land
# where they were measured, and never when an upgrade heading is also present.
_MISSIONS_TITLE_Y = (230, 270)
_MISSIONS_BANNER_Y = (358, 418)
_MISSIONS_COUNT_Y = (633, 693)
# The measured gaps from the page title down to its banner and to its count
# band - the part of the recorded geometry that survives a device drawing the
# page higher. Measured at +129/+405 on the inset-bearing captures and
# +131/+404 on the edge-to-edge one; the bound is those with 20px of margin.
# See MAX_TOP_INSET for why frame y was never the coordinate to key on.
_MISSIONS_TITLE_TO_BANNER = (109, 149)
_MISSIONS_TITLE_TO_COUNT = (385, 425)
# Matched against the raw text, not tiles.normalise: normalising strips the
# slash, and "2/8 Missions" would become indistinguishable from "28 missions".
_MISSIONS_COUNT = re.compile(r'(\d+)\s*/\s*(\d+)\s*missions', re.I)

# Three independent anchors measured on the recorded Cards capture: the page
# title at (30, 246, 174, 45), the ACTIVE heading at (436, 504, 208, 52) and
# the equipped band beneath it at (502, 555, 77, 45). The capture also shows
# BUY NEW CARD, INVENTORY and an Unlock New Slot tile; those corroborate the
# page for the cards reader but are not part of its identity, because a
# scrolled or later-stage page may not draw all of them.
_CARDS_TITLE_Y = (226, 266)
_CARDS_ACTIVE_Y = (484, 524)
_CARDS_SLOTS_Y = (535, 575)
_CARDS_SLOTS = re.compile(r'(\d+)\s*/\s*(\d+)')

# The MILESTONES ladder. Measured on menu_milestones_claimable, which - like
# every milestones capture - comes from the edge-to-edge device, so the title
# sits at y=113 where the MISSIONS anchors were recorded at y=249 on a device
# that reserves a status bar.
#
# The band therefore widens DOWNWARD by MAX_TOP_INSET, where the missions band
# widens upward. Same one fact about the device, opposite direction, because
# the recording is the other layout: a device WITH a status bar draws this page
# LOWER than the capture, not higher.
_MILESTONES_TITLE_Y = (93, 133)
# Title at y=113, `Tier 1` at y=2118: a gap of 2005, measured from the page's
# own title rather than from the frame edge, for the same reason every other
# anchor here is.
_MILESTONES_TITLE_TO_TIER = (1985, 2025)
# Matched against RAW text, not a normalised label: tiles.normalise('Tier 1')
# is 'tier1', which bakes the tier number into the thing being compared. Same
# reason _MISSIONS_COUNT and _CARDS_SLOTS match raw text.
_MILESTONES_TIER = re.compile(r'tier\s*(\d+)', re.I)

# The reward ceremony `Claim All` opens. It is a centred overlay with no title
# to measure from, so unlike every other page's anchors these stay absolute -
# and it has been captured on ONE layout only, the edge-to-edge device. See
# the spec's Limits.
#
# Both bands widen DOWNWARD by MAX_TOP_INSET at the point of use, same
# direction and same reasoning as _milestones_title: the capture is inset-0,
# and a device that reserves a status bar draws this overlay LOWER, not
# higher - there is no title on this screen to measure the gap from instead.
_MODAL_SKIP_Y = (252, 292)
_MODAL_CLAIM_Y = (1840, 1880)

# These bounds cover the central bordered panels in the two recorded Workshop
# overlays (740x702 and 890x1132) and two game-over captures (986x1116/1212).
# Recorded upgrade tiles are only 196px high. This is a fail-closed guard for
# that measured geometry, not recognition of every overlay the game can show.
_GUARDED_OVERLAY_MIN_W = 700
_GUARDED_OVERLAY_MIN_H = 600
_GUARDED_OVERLAY_EVIDENCE = (
    'menu_workshop_info_panel',
    'menu_workshop_explainer_modal',
    'game_over_fade',
    'game_over_stats',
)


def _has_guarded_overlay(screen: Image) -> bool:
    """Return whether a measured large panel covers the screen centre."""
    height, width = screen.shape[:2]
    centre_x, centre_y = width // 2, height // 2
    return any(
        rect.w >= _GUARDED_OVERLAY_MIN_W
        and rect.h >= _GUARDED_OVERLAY_MIN_H
        and rect.x <= centre_x < rect.x + rect.w
        and rect.y <= centre_y < rect.y + rect.h
        for rect in tiles.candidates(screen)
    )


# The two measured y bands an upgrade heading occupies: the Workshop page's
# and the in-run panel's. Shared, so that recognising a heading and refusing
# one are the same judgement made in one place.
_WORKSHOP_HEADING_Y = (380, 420)
_BATTLE_HEADING_Y = (1640, 1680)
# How far ABOVE its recorded band a top-anchored menu page can be drawn.
#
# The recorded captures come from a device that reserves a status bar, so
# every menu page in tests/fixtures starts below one. A device that draws the
# game edge to edge instead has no such strip, and the game paints the very
# same page that much higher: measured against a live 1080x2400 phone, the
# Workshop title, its heading and every row tap all sat 135-139px above where
# the fixtures put them - one translation, not a different layout.
#
# So frame y was never the coordinate the evidence is really in. Only the
# top-anchored menu pages move; the in-run panel is anchored to the bottom of
# the screen and lands on its recorded band on both devices, which is why
# battle reading never showed this and _BATTLE_HEADING_Y stays absolute.
#
# Public so the bound this module enforces can be NAMED by the readers that
# live downstream of it - missions_screen documents its own probe against it -
# even though nothing outside this module reads the value today. It is one
# fact about the device, and it is stated once.
MAX_TOP_INSET = 200
# The measured gap from the Workshop page title down to its category heading -
# the part of the recorded geometry that survives the translation above.
_WORKSHOP_TITLE_TO_HEADING = (131, 171)
_UPGRADE_HEADINGS = ('attackupgrades', 'defenseupgrades', 'utilityupgrades')


def _upgrade_label(box: ocr.TextBox) -> str | None:
    """The upgrade category this box names, or None if it names none.

    An exact label, never a suffix. A mission card reading "Buy 20 battle
    upgrades" ends in the same word and is prose, not a heading.
    """
    label = tiles.normalise(box.text).replace('defence', 'defense')
    return label.removesuffix('upgrades') if label in _UPGRADE_HEADINGS else None


def _is_upgrade_heading(box: ocr.TextBox) -> bool:
    """Whether this box is an upgrade heading WHERE an upgrade heading sits.

    Position is part of the identity. Without it this is a word match, and
    any text anywhere on any screen can borrow a heading's meaning.
    """
    return (_upgrade_label(box) is not None and box.confidence >= .9
            and 0 <= box.rect.x <= 70
            and (_WORKSHOP_HEADING_Y[0] - MAX_TOP_INSET
                 <= box.rect.y <= _WORKSHOP_HEADING_Y[1]
                 or _BATTLE_HEADING_Y[0] <= box.rect.y <= _BATTLE_HEADING_Y[1]))


def _single(boxes: tuple[ocr.TextBox, ...], label: str,
            bounds: tuple[int, int]) -> ocr.TextBox | None:
    """The one trusted box with this label inside a measured y band, or None.

    Two candidates are ambiguous and yield None exactly like none at all: a
    duplicated anchor is not evidence that the page is what it looks like.
    """
    top, bottom = bounds
    matches = [b for b in boxes if tiles.normalise(b.text) == label
               and b.confidence >= .9 and top <= b.rect.y <= bottom]
    return matches[0] if len(matches) == 1 else None


def _missions_title(boxes: tuple[ocr.TextBox, ...]) -> ocr.TextBox | None:
    """The Daily Missions title, wherever this device's inset put it.

    Searched in a band widened upward by MAX_TOP_INSET rather than at its
    recorded y: the title is this page's origin, and every other missions
    anchor is measured from it. Still exactly one or nothing - two titles are
    not evidence of a page, they are evidence of a misread.
    """
    return _single(boxes, 'dailymissions',
                   (_MISSIONS_TITLE_Y[0] - MAX_TOP_INSET, _MISSIONS_TITLE_Y[1]))


def missions_count(boxes: tuple[ocr.TextBox, ...]) -> tuple[int, int] | None:
    """The "N/M Missions" band as (shown, offered), or None if not certain.

    Located by its measured distance below the page title rather than by
    frame y: a device that reserves no status bar draws the same page higher,
    and the gap between title and band is what the captures evidence. No
    title means no origin to measure from, which is not certainty.
    """
    title = _missions_title(boxes)
    if title is None:
        return None
    low, high = _MISSIONS_TITLE_TO_COUNT
    matches = [m for m in (_MISSIONS_COUNT.fullmatch(b.text.strip())
                           for b in boxes if b.confidence >= .9
                           and low <= b.rect.y - title.rect.y <= high) if m]
    if len(matches) != 1:
        return None
    shown, offered = int(matches[0][1]), int(matches[0][2])
    return (shown, offered) if shown <= offered else None


def cards_slot_band(boxes: tuple[ocr.TextBox, ...]) -> ocr.TextBox | None:
    """The one trusted "N / M" box under the ACTIVE heading, or None.

    Matched against the raw text for the same reason the missions band is:
    tiles.normalise strips the slash, and "0/1" would become "01".
    """
    matches = [b for b in boxes if b.confidence >= .9
               and _CARDS_SLOTS_Y[0] <= b.rect.y <= _CARDS_SLOTS_Y[1]
               and _CARDS_SLOTS.fullmatch(b.text.strip())]
    return matches[0] if len(matches) == 1 else None


def cards_slots(boxes: tuple[ocr.TextBox, ...]) -> tuple[int, int] | None:
    """The ACTIVE band as (equipped, capacity), or None if not certain."""
    box = cards_slot_band(boxes)
    if box is None:
        return None
    match = _CARDS_SLOTS.fullmatch(box.text.strip())
    equipped, capacity = int(match[1]), int(match[2])
    # More cards equipped than slots to hold them is a misread, not a state.
    return (equipped, capacity) if equipped <= capacity else None


def _discover_cards(boxes: tuple[ocr.TextBox, ...]) -> ScreenDiscovery:
    """Claim the Cards page only on all three measured anchors."""
    # The Cards page has no upgrade heading. One appearing here means the
    # frame is an upgrade screen reached in the wrong context, and reading it
    # as a card collection would put slot counts on a purchase grid.
    if any(_is_upgrade_heading(b) for b in boxes):
        return ScreenDiscovery(None, False, 'unsupported_layout')
    title = _single(boxes, 'cards', _CARDS_TITLE_Y)
    active = _single(boxes, 'active', _CARDS_ACTIVE_Y)
    if title is None or active is None or cards_slots(boxes) is None:
        return ScreenDiscovery(None, False, 'ambiguous_or_unreadable_heading')
    if not 0 <= title.rect.x <= 70:
        return ScreenDiscovery(None, False, 'unsupported_layout')
    return ScreenDiscovery('cards.inventory', True, 'recorded_layout')


def _discover_missions(boxes: tuple[ocr.TextBox, ...]) -> ScreenDiscovery:
    """Claim the Daily Missions page only on all three measured anchors."""
    # An upgrade page must never be read as missions. Keyed on a heading at
    # its measured place, because the missions page is FULL of prose that
    # mentions upgrades: "Buy 20 battle upgrades" is an ordinary daily, and
    # matching it here made the reader fail on any day it was drawn.
    if any(_is_upgrade_heading(b) for b in boxes):
        return ScreenDiscovery(None, False, 'unsupported_layout')
    title = _missions_title(boxes)
    if title is None:
        return ScreenDiscovery(None, False, 'ambiguous_or_unreadable_heading')
    # Measured against the title rather than the frame edge - see
    # _MISSIONS_TITLE_TO_BANNER. Three anchors still, and still all three.
    banner = _single(boxes, 'weeklychallenge',
                     (title.rect.y + _MISSIONS_TITLE_TO_BANNER[0],
                      title.rect.y + _MISSIONS_TITLE_TO_BANNER[1]))
    if banner is None or missions_count(boxes) is None:
        return ScreenDiscovery(None, False, 'ambiguous_or_unreadable_heading')
    if not 0 <= title.rect.x <= 70:
        return ScreenDiscovery(None, False, 'unsupported_layout')
    return ScreenDiscovery('missions.daily', True, 'recorded_layout')


def _milestones_title(boxes: tuple[ocr.TextBox, ...]) -> ocr.TextBox | None:
    """The MILESTONES title, wherever this device's inset put it.

    Exactly one or nothing, like every other page title here: two are not
    evidence of a page, they are evidence of a misread.
    """
    return _single(boxes, 'milestones',
                   (_MILESTONES_TITLE_Y[0], _MILESTONES_TITLE_Y[1] + MAX_TOP_INSET))


def milestones_tier(boxes: tuple[ocr.TextBox, ...], *, frame_height: int = 2400) -> int | None:
    """Which tier's ladder this is, or None if not certain.

    Located by its measured distance below the page title rather than by
    frame y, for the reason MAX_TOP_INSET exists. No title means no origin to
    measure from, which is not certainty.
    """
    title = _milestones_title(boxes)
    if title is None:
        return None
    offset = anchored_y(0, frame_height, 'bottom')
    low, high = (bound + offset for bound in _MILESTONES_TITLE_TO_TIER)
    matches = [m for m in (_MILESTONES_TIER.fullmatch(b.text.strip())
                           for b in boxes if b.confidence >= .9
                           and low <= b.rect.y - title.rect.y <= high) if m]
    return int(matches[0][1]) if len(matches) == 1 else None


def _modal_skip(boxes: tuple[ocr.TextBox, ...], *,
                frame_height: int = 2400) -> ocr.TextBox | None:
    """The reward modal's SKIP button, wherever this device's inset put it.

    Public-enough for milestones_screen to probe with directly, the way
    missions_screen already probes _missions_title: SKIP is the one anchor
    that appears on exactly one committed capture, the reward modal, so it is
    a discriminating presence signal on its own - unlike `claim`, which also
    appears on the missions page and would false-positive there.
    """
    shift = anchored_y(0, frame_height, 'center')
    return _single(boxes, 'skip',
                   (_MODAL_SKIP_Y[0] + shift,
                    _MODAL_SKIP_Y[1] + shift + MAX_TOP_INSET))


def milestone_modal_action(boxes: tuple[ocr.TextBox, ...], *,
                           frame_height: int = 2400) -> tuple[str, ocr.TextBox] | None:
    """The single NEXT or CLAIM control on a measured reward ceremony."""
    shift = anchored_y(0, frame_height, 'center')
    candidates = [b for b in boxes if b.confidence >= .9
                  and tiles.normalise(b.text) in {'next', 'claim'}
                  and _MODAL_CLAIM_Y[0] + shift <= b.rect.y
                  <= _MODAL_CLAIM_Y[1] + shift + MAX_TOP_INSET]
    if len(candidates) != 1:
        return None
    box = candidates[0]
    return tiles.normalise(box.text), box


def _modal_claim(boxes: tuple[ocr.TextBox, ...], *,
                 frame_height: int = 2400) -> ocr.TextBox | None:
    """The reward modal's final CLAIM, if that is its only action."""
    found = milestone_modal_action(boxes, frame_height=frame_height)
    return found[1] if found is not None and found[0] == 'claim' else None


def _discover_milestones(boxes: tuple[ocr.TextBox, ...], frame_height: int) -> ScreenDiscovery:
    """The ladder on its title and tier, the modal on its two buttons.

    The modal is checked first, and that ordering is an ASSUMPTION rather
    than an observation. The modal is drawn over the ladder, so a frame
    carrying both sets of anchors ought to be the modal - but no capture
    carries both: the recorded modal frame holds four boxes (SKIP, a coin
    glyph, the reward line and CLAIM) and none of the ladder's. The two
    anchor sets are disjoint on every frame measured so far, so this order
    does not currently decide anything.
    """
    skip = _modal_skip(boxes, frame_height=frame_height)
    action = milestone_modal_action(boxes, frame_height=frame_height)
    if skip is not None and action is not None:
        return ScreenDiscovery('milestones.reward_modal', True, 'recorded_layout')
    title = _milestones_title(boxes)
    if title is None or milestones_tier(boxes, frame_height=frame_height) is None:
        return ScreenDiscovery(None, False, 'ambiguous_or_unreadable_heading')
    if not 0 <= title.rect.x <= 70:
        return ScreenDiscovery(None, False, 'unsupported_layout')
    return ScreenDiscovery('milestones.ladder', True, 'recorded_layout')


def capabilities() -> dict[str, Any]:
    """Return a detached support matrix; catalog existence is not coverage."""
    return {
        'schema_version': 1,
        'complete': False,
        'resolution': [1080, 2400],
        'locale': 'en',
        'readers': dict(_RECORDED_READERS),
        'recorded_verified': [reader for reader, _ in _RECORDED_READERS],
        'existing_runtime_supported': [],
        'recorded_unlock_stages': {screen: list(names)
                                   for screen, names in _RECORDED_UNLOCK_STAGES},
        'uncatalogued_labels': dict(_UNCATALOGUED_LABELS),
        'account_screens': ['account.settings', 'account.stats.summary',
                            'account.stats.tiers'],
        'game_over': list(_GAME_OVER),
        'replay_coverage': {
            'manifest': _REPLAY_MANIFEST,
            'account_snapshots': _ACCOUNT_SNAPSHOTS,
            'covered': list(_REPLAY_COVERED),
            'gaps': dict(_REPLAY_GAPS),
        },
        'recognized_overlays': {
            'workshop.info_overlay': 'menu_workshop_info_panel',
            'workshop.ultimate_explainer': 'menu_workshop_explainer_modal',
        },
        'guarded_overlay_geometry': {
            'minimum_size': [_GUARDED_OVERLAY_MIN_W, _GUARDED_OVERLAY_MIN_H],
            'must_cover_screen_center': True,
            'recorded_evidence': list(_GUARDED_OVERLAY_EVIDENCE),
        },
        'unsupported_owners': dict(_UNSUPPORTED_OWNERS),
        # Recorded, and honest about which stage was recorded. Absent from
        # 'readers' deliberately: discover() still refuses this page, because
        # it is not a screen the purchase loop may ever be handed rows from.
        'ultimate_weapons': {
            'recorded_stage': 'menu_workshop_ultimate',
            'recorded_stage_meaning': 'locked_system',
            'reader': 'ultimate_weapons.read_page',
            'base_identities': 9,
            'unread': sorted(ULTIMATE_WEAPON_GAPS),
        },
        'unsupported': [
            # 'nested_account_menus' was removed once the Settings -> Stats
            # menu was read and proven. 'later_unlock_stage_layouts' narrowed
            # to the screens that still have no second-stage capture rather
            # than disappearing: the Workshop tabs have one, nothing else does.
            'later_unlock_stage_layouts_outside_workshop',
            'battle_history_export', 'native_stat_export',
            'labs_screen_layout', 'labs_research_actions', 'labs_acceleration_spend',
            'other_locales', 'other_resolutions', 'unknown_overlays',
            # A claimable capture and the full 5..35 strip are now recorded
            # (menu_missions_claimable and menu_missions_weekly), so the
            # limits below are no longer about missing evidence. They are
            # about the reader: a card whose progress bar is replaced by a
            # CLAIM button carries no "N / M" text, so it parses as
            # `unreadable` with no mission id at all. The reader sees that a
            # card is there and honestly reports it cannot identify it. It
            # observes; it never claims a reward or names a currency.
            'missions_claim_actions', 'missions_reward_currency',
            'missions_milestone_claim_state', 'missions_beyond_the_recorded_strip',
            # Listed with the missing captures rather than the reader limits:
            # the Modules screen has never been recorded, so `discover` refuses
            # its context outright instead of guessing at anchors.
            'modules_screen_layout', 'modules_banner_pity_state',
            'module_upgrade_merge_actions',
            # The recorded Cards page is an account with an empty collection:
            # its slot band reads exactly, and every tile below it is a
            # padlock. So the page is supported and the collection is not,
            # and those two facts have to stay visibly separate.
            'card_identity_rows', 'card_mastery_ownership', 'card_presets',
            'in_run_card_locks', 'card_passive_or_active_classification',
            'card_slot_purchase_actions',
            # Not "no capture exists" for the page - one does - but "no
            # capture exists of an account that owns a UW", which is what
            # every entry below would have to be read from.
            *sorted(ULTIMATE_WEAPON_GAPS),
        ],
        # Declared readers whose behaviour is consistent with the recorded
        # evidence but NOT demonstrated by it. Kept apart from 'unsupported'
        # deliberately: these paths do run, and the risk is a reader being
        # trusted for a property no capture has actually shown.
        'unproven': {
            'missions.identity_across_reordering':
                'The recorded page holds two missions in one ordering. The '
                'focused checks translate those boxes between the two card '
                'rectangles, which shows the reader does not key off row '
                'index but is not a second recorded ordering. A multi-mission '
                'capture of the same set reordered would settle it.',
            'cards.identity_on_a_stocked_collection':
                'The one recorded Cards page belongs to an account with an '
                'empty collection. Its title and ACTIVE band are read where '
                'they were measured, and nothing shows that those anchors '
                'stay put once rows, presets and a scrollable inventory are '
                'drawn under them. A capture of a stocked collection would '
                'settle it, and would also be the first evidence a card row '
                'reader could be built on.',
            'game_over.result.low_confidence_title_refusal':
                'parse_frame() refuses the whole modal - screen_id None, no '
                'fields - when the GAMESTATS title is not found at trusted '
                'confidence, which is the path a still-fading-in modal would '
                'take. None of the five recorded game-over captures shows '
                'this: despite its name, game_over_fade.png reads every field '
                'at full confidence, no different from a stable capture (see '
                "its 'why' in the replay manifest's frames table). The "
                'ambiguous example for game_over.result is real evidence of a '
                'different failure - a garbled field value - not this one. A '
                'capture of the modal mid-fade would settle it.',
            'missions.identity_across_ocr_jitter':
                'The one real mission text on the capture reads as '
                '"Kill zo0basic enemies" at confidence .9208, above the .90 '
                'gate. A misread cannot inherit a clean mission id, but a '
                'stable id for that mission is unproven on a single sample.',
        },
    }


@dataclass(frozen=True)
class ScreenDiscovery:
    screen_id: str | None
    readable: bool
    reason: str


def discover(
    screen: Image, boxes: tuple[ocr.TextBox, ...], context: str, *, locale: str = 'en'
) -> ScreenDiscovery:
    """Validate context and measured heading geometry before exposing rows.

    Locale is a configured assertion, additionally checked against the known
    English headings. It is not automatic detection of an entire game locale.
    Unknown screens and recorded overlays never provide action coordinates.
    """
    if screen.shape[:2] != (2400, 1080) and not (
            context in ('milestones', 'battle', 'workshop', 'missions')
            and supported_frame(screen.shape[1], screen.shape[0])):
        return ScreenDiscovery(None, False, 'unsupported_geometry')
    if locale != 'en':
        return ScreenDiscovery(None, False, 'unsupported_locale')
    if context not in ('workshop', 'battle', 'missions', 'cards', 'milestones'):
        return ScreenDiscovery(None, False, 'unsupported_context')
    labels = {tiles.normalise(b.text) for b in boxes}
    if {'currentlevel', 'maxlevel'} <= labels:
        return ScreenDiscovery('workshop.info_overlay', False, 'overlay')
    if {'ultimateweapons', 'ok'} <= labels:
        return ScreenDiscovery('workshop.ultimate_explainer', False, 'overlay')
    if _has_guarded_overlay(screen):
        return ScreenDiscovery(None, False, 'overlay_geometry')
    if context == 'missions':
        return _discover_missions(boxes)
    if context == 'cards':
        return _discover_cards(boxes)
    if context == 'milestones':
        return _discover_milestones(boxes, screen.shape[0])
    headings = [b for b in boxes if _upgrade_label(b) is not None]
    if len(headings) != 1 or headings[0].confidence < .9:
        return ScreenDiscovery(None, False, 'ambiguous_or_unreadable_heading')
    heading = headings[0]
    category = _upgrade_label(heading)
    if context == 'workshop':
        # Measured against the page's own title rather than against the top
        # of the frame: the gap between the two is the recorded evidence, and
        # the distance down from the frame edge is the device's status bar.
        # See MAX_TOP_INSET.
        titles = [b for b in boxes if tiles.normalise(b.text) == 'workshop']
        valid = (len(titles) == 1 and titles[0].confidence >= .9
                 and 230 - MAX_TOP_INSET <= titles[0].rect.y <= 270
                 and _WORKSHOP_TITLE_TO_HEADING[0]
                 <= heading.rect.y - titles[0].rect.y
                 <= _WORKSHOP_TITLE_TO_HEADING[1])
    else:
        valid = ('workshop' not in labels
                 and anchored_y(_BATTLE_HEADING_Y[0], screen.shape[0], 'bottom')
                 <= heading.rect.y
                 <= anchored_y(_BATTLE_HEADING_Y[1], screen.shape[0], 'bottom'))
    if not valid or not (0 <= heading.rect.x <= 70):
        return ScreenDiscovery(None, False, 'unsupported_layout')
    screen_id = f'{context}.{category}'
    reason = 'recorded_layout' if screen_id in dict(_RECORDED_READERS) else 'existing_runtime_supported'
    return ScreenDiscovery(screen_id, True, reason)
