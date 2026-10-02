"""Passive, read-only reading of the recorded English Daily Missions page.

Identity comes from the mission's own text and never from its row position:
the same mission keeps its id when the list reorders and when a new mission
appears above it. That is the whole point of this reader, so `parse_frame`
binds every number to the card the text was read from.

Claim targets come only from a visible button on the current card. A separate
walk owns the tap and verifies the following frame.

The Daily Missions layout is measured at both 1080x2400 and native
1080x1920 BlueStacks height, with captures and OCR under tests/fixtures.
"""
from __future__ import annotations

import hashlib
import math
import re
import statistics
import time
import threading
from collections import Counter
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable

import ocr
import screen_discovery
import tiles
from geometry import supported_frame
from config import Rect
from device import Image

_MIN_CONFIDENCE = .90

# Unmeasured frame sizes are never interpreted as a clear page.

# The page title, measured at (32, 249, 433, 40).
_TITLE_LABEL = 'dailymissions'
# The title's y on every inset-bearing capture. An offset is the difference
# between where this device drew the title and this number; it is 0 on the
# recorded captures and negative on an edge-to-edge device.
_RECORDED_TITLE_Y = 249

# scan() used to detect the page by cropping a title-sized band and OCR-ing it
# at each of two MEASURED status-bar insets (0, 136) before paying for a
# full-frame read - and paid for the full-frame read anyway once a crop hit,
# so a page found at the second inset cost three OCR passes, and a page at
# neither cost two for nothing. Measured against
# tests/fixtures/in_run_lit.png (median of 5 warm runs): the two-crop probe
# cost 487.5ms; a single full-frame read costs 252.2ms - about the same as
# ONE crop, because a RapidOCR call's overhead does not scale with a region
# this small. So scan() now reads the whole frame once, always, and detects
# the page from those same boxes via screen_discovery._missions_title, which
# searches the widened 0..MAX_TOP_INSET band screen_discovery already
# declares - continuously, rather than at two discrete insets. That also
# closes a coverage gap the two-inset probe had: an inset outside {0, 136}
# used to make scan() report `scanned=True, screen_id=None` - the one answer
# permitted to mean "examined, and no missions page is up" - on a frame
# screen_discovery itself would call missions.daily. Searching the range
# continuously removes that hole rather than adding a third fixed point to it.
#
# One narrowing this trades in: the old crop probe read with
# min_confidence=0., so any confidence at all counted as "maybe found," and
# only the downstream parse enforced the >=.90 trust gate. _missions_title
# enforces that same gate directly, so a title read at, say, 0.87 - between
# the full read's 0.85 floor and the .90 trust cutoff - now falls out of
# "found" instead of falling through to "found but unreadable." Every
# recorded missions title reads at .9908-.9931, none within .09 of that gate,
# so this is UNEXERCISED by any capture - no evidence of a problem, not
# evidence of its absence. Accepted because the alternative was this reader
# quietly keeping a more permissive trust rule than screen_discovery's own,
# for exactly the kind of low-confidence misread this module refuses to act
# on everywhere else.

# "completed 0/35" measured at (796, 326, 261, 36) - the daily counter, and
# the only evidence this reader has for whether a weekly milestone is reached.
_COMPLETED = Rect(700, 306, 380, 76)
# Matched against the raw text: tiles.normalise strips the slash, which would
# turn "completed 0/35" into the unreadable "completed035".
_COMPLETED_TEXT = re.compile(r'completed\s*(\d+)\s*/\s*(\d+)', re.I)

# The weekly milestone thresholds, measured as a single row of bare integers
# at y 577-621. The first viewport shows 5..25; a horizontal swipe reveals
# 30 and 35. Each frame contains only the boxes visible in that viewport.
_MILESTONES = Rect(60, 555, 950, 90)

# The two mission cards tiles.candidates finds at (17, 738, 1046, 242) and
# (17, 1003, 1046, 242). Deliberately outside config.TILE_MIN_H/TILE_MAX_H:
# find_tiles must keep refusing these, so a missions frame can never turn
# into a grid of purchasable upgrade rows.
_CARD_MIN_W, _CARD_MAX_W = 1000, 1080
_CARD_MIN_H, _CARD_MAX_H = 220, 270

# Measured fractions of a card, consistent across both recorded cards: the
# reward column sits right of .85, the progress bar is centred below .5, and
# the mission text is what remains in the upper left.
_REWARD_LEFT_FRACTION = .85
_PROGRESS_TOP_FRACTION = .5
_PROGRESS = re.compile(r'(\d+)\s*/\s*(\d+)')
_INTEGER = re.compile(r'\d+')
_WEEKLY_REWARD = re.compile(r'\s*(\d+)\s*(coins|gems|medals|stones)\s*', re.I)
_WEEKLY_PROGRESS = re.compile(r'(\d+)\s*/\s*(\d+)')
WEEKLY_REWARD_SCREEN = 'missions.weekly_reward_modal'

# The button that replaces the progress bar on a finished card. Measured at
# (454, 751, 153, 46) on menu_missions_claimable_no_status_bar - centred in the
# card's lower half, left of the reward column, exactly where a bar would be.
_CLAIM_LABEL = 'claim'


@dataclass(frozen=True)
class MissionEntry:
    """One mission card, addressed by what is written on it.

    `mission_id` is the text with its goal count removed - see identity_of
    for which digit that is and which digits stay - so a daily that returns
    asking for a bigger number is still the same mission. It is None whenever
    the text could not be trusted: an unreadable card has no identity rather
    than a guessed one. Comparing OBJECTIVES across frames means comparing
    `(mission_id, target)` - MissionsReading.status_for takes both for
    exactly that reason; `mission_id` alone means "the same recurring
    mission", which is a weaker claim.

    `status` keeps available, complete, claimable, ambiguous and unreadable
    apart. `claimable` is a card whose bar has been replaced by a CLAIM
    button: its `progress` and `target` are None because the page no longer
    states them, which is not the same fact as a bar that could not be read.
    """

    mission_id: str | None
    raw_text: str
    progress: int | None
    target: int | None
    status: str
    reward_values: tuple[int, ...]
    rewards_status: str
    confidence: float
    rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class MilestoneEntry:
    """One weekly-challenge chest, addressed by its threshold.

    `status` is the historical counter-derived eligibility. `claim_state` is
    read from the chest pixels: a bright box can be tapped, a green check has
    already been claimed, and an unreadable box must never become a target.
    `rect` bounds the chest itself, not the number printed underneath it.
    """

    threshold: int
    status: str
    claim_state: str
    confidence: float
    rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class ChestTarget:
    threshold: int
    rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class WeeklyRewardModal:
    index: int
    total: int
    action: str
    control: tuple[int, int, int, int]
    reward_text: str | None
    currency: str | None
    amount: int | None


def weekly_reward_modal(screen: Image,
                        boxes: tuple[ocr.TextBox, ...]) -> WeeklyRewardModal | None:
    """Read one frame of the chest's NEXT ... CLAIM ceremony."""
    height, width = screen.shape[:2]
    skips = [box for box in boxes if _trusted(box)
             and box.text.strip().casefold() == 'skip'
             and box.rect.x > width * .7 and box.rect.y < height * .2]
    steps = [match for box in boxes if _trusted(box)
             and box.rect.y > height * .9
             for match in [_WEEKLY_PROGRESS.fullmatch(box.text.strip())] if match]
    actions = [box for box in boxes if _trusted(box)
               and box.text.strip().casefold() in {'next', 'claim'}
               and height * .72 < box.rect.y < height * .84]
    rewards = [box for box in boxes if _trusted(box)
               and height * .53 < box.rect.y < height * .63
               and _WEEKLY_REWARD.fullmatch(box.text) is not None]
    if len(skips) != 1 or len(steps) != 1 or len(actions) != 1 or len(rewards) > 1:
        return None
    index, total = map(int, steps[0].groups())
    action = actions[0].text.strip().casefold()
    if not (1 <= index <= total <= 8
            and action == ('claim' if index == total else 'next')):
        return None
    reward_text = rewards[0].text if rewards else None
    match = _WEEKLY_REWARD.fullmatch(reward_text) if reward_text else None
    rect = actions[0].rect
    return WeeklyRewardModal(
        index, total, action, (rect.x, rect.y, rect.w, rect.h), reward_text,
        match[2].lower() if match else None,
        int(match[1]) if match else None)


@dataclass(frozen=True)
class MissionsReading:
    """One frame of the Daily Missions page.

    `shown`/`offered` are the two halves of the "N/M Missions" band and
    `unseen` is their difference. That the band means drawn-of-offered is an
    INFERENCE, not something the page states: the separate "completed 0/35"
    counter rules out the competing reading (that N counts finished
    missions), but only a capture with a different draw count settles it.
    """

    screen_id: str
    observed_at: float
    frame_width: int
    frame_height: int
    frame_digest: str
    completed: int | None
    completed_target: int | None
    shown: int | None
    offered: int | None
    unseen: int | None
    # How many mission cards the FRAME draws, independent of how many the
    # count band claims and of how many carried readable text. The band and
    # the parsed count can agree on a short read - a band misread low, a card
    # that yielded no OCR - and then only the drawn cards contradict them.
    cards_drawn: int
    missions: tuple[MissionEntry, ...]
    milestones: tuple[MilestoneEntry, ...]
    complete: bool

    def status_for(self, mission_id: str, target: int | None = None) -> str:
        """What this frame says about one mission, keeping absence from doubt.

        Pass `target` as well to ask about an OBJECTIVE rather than about a
        recurring mission: 'Reach tier 5' and 'Reach tier 10' share the id
        `reach_tier` and are told apart only by their goal, so a caller that
        cares which one it is must say so. Omitting it asks the weaker
        question, "is this mission on the page at all".

        'unseen' is a claim about the WORLD - this mission is not on the
        page - and may only be made once the page was read completely: every
        card the page says it is showing was parsed, every card the FRAME
        draws was parsed, and every parsed card was identified. If any card's
        identity is unknown, or fewer cards were read than either the page
        says are drawn or the frame actually holds, then this mission cannot
        be ruled out, and the answer is 'unreadable' - a claim about our
        EVIDENCE. A card that is visibly on screen must never be reported as
        absent just because we failed to name it.
        """
        matches = [m for m in self.missions if m.mission_id == mission_id
                   and (target is None or m.target == target)]
        if len(matches) == 1:
            return matches[0].status
        if matches:
            return 'ambiguous'
        if (self.shown is None or len(self.missions) != self.shown
                or len(self.missions) != self.cards_drawn
                or any(m.mission_id is None for m in self.missions)):
            return 'unreadable'
        return 'unseen'


@dataclass(frozen=True)
class ClaimTarget:
    """One CLAIM button, with the card it belongs to named.

    Its own type rather than a field on MissionEntry, and deliberately so: the
    reader's entries stay free of anything a tap could be read from, and a
    caller that means to claim has to ask for targets explicitly. `rect` here
    IS a tap target - the only one this module produces - and it is only ever
    valid for the frame it was read from.
    """

    mission_id: str | None
    raw_text: str
    coins: int | None
    gems: int | None
    rect: tuple[int, int, int, int]


def _trusted(box: ocr.TextBox) -> bool:
    return math.isfinite(box.confidence) and _MIN_CONFIDENCE <= box.confidence <= 1.


def _inside(rect: Rect, box: Rect) -> bool:
    x, y = box.x + box.w / 2, box.y + box.h / 2
    return rect.x <= x < rect.x + rect.w and rect.y <= y < rect.y + rect.h


def _shift(rect: Rect, dy: int) -> Rect:
    """The same band, moved to where this device drew the page."""
    return Rect(rect.x, rect.y + dy, rect.w, rect.h)


def _title_offset(boxes: tuple[ocr.TextBox, ...]) -> int | None:
    """How far above its recorded place this device drew the page, or None.

    Measured from the page's own title, which is the only anchor here that is
    identified by its text rather than by its position. Exactly one trusted
    title or nothing: two are a misread, and none leaves no origin, which is
    not the same fact as an offset of zero.
    """
    titles = [b for b in boxes
              if _trusted(b) and tiles.normalise(b.text) == _TITLE_LABEL]
    if len(titles) != 1:
        return None
    return titles[0].rect.y - _RECORDED_TITLE_Y


def identity_of(text: str, target: int | None, *, complete: bool = False) -> str | None:
    """The mission's identity: its text with the goal count removed.

    The goal is the one digit run the progress bar agrees with, and only
    that one. 'Kill 200 basic enemies' against a 0/200 bar is
    `kill_basic_enemies`, so the same daily returning to ask for 500 keeps
    its identity while `target` carries what changed.

    Every OTHER digit run stays in the id, because it is part of what the
    mission is about rather than how much of it is wanted. 'Reach wave 100
    in tier 3' and 'Reach wave 100 in tier 5' are two different objectives:
    dropping every digit collapses them onto one id, which fails closed
    within a frame (both come back `ambiguous`) but silently conflates them
    ACROSS frames, where nothing is left to show the objective changed.

    When the target matches no digit run - a garbled read - or matches more
    than one, the goal cannot be singled out and every digit is kept. That
    direction is deliberate: an id that is too specific splits one mission
    into two, and each half is honestly reported, while an id that is too
    loose merges two missions into one and says nothing about it.

    No target at all is a DIFFERENT case and gets a different answer. With no
    bar there is no evidence whatever about which digit is the quantity, so a
    text carrying digits has no identity here rather than a guessed one -
    `None`, not a digit-preserving slug, because such a slug would be an
    identity asserted from an assumption nothing on the frame supports. A
    text with no digits at all has nothing to split and is answered normally.

    A text with no letters left is likewise `None`: a bare-number id could
    collide with any other numeric garble and names no mission.

    `complete` says the goal is absent because the mission is FINISHED, not
    because the bar could not be read - a claimable card replaces its bar with
    a CLAIM button. That is different evidence, so it gets a different answer:
    every digit is kept, giving `kill_200_basic_enemies` where the same mission
    in progress gives `kill_basic_enemies`. The two ids are deliberately not
    comparable, which is the safe direction - one mission reported as two,
    each honestly, rather than two collapsed into one that says nothing.

    Known and accepted: when the goal digit IS the subject - 'Reach tier 5'
    against a 0/5 bar, where the bar literally counts tiers - stripping it
    gives `reach_tier` for every tier. That is the same recurring objective
    at a different goal, and `target` is what tells them apart. Callers
    comparing OBJECTIVES pass both to MissionsReading.status_for.
    """
    if target is None and not complete and re.search(r'\d', text):
        return None
    runs = list(re.finditer(r'\d+', text))
    goals = [run for run in runs if target is not None and int(run[0]) == target]
    dropped = goals[0].span() if len(goals) == 1 else None
    pieces: list[str] = []
    cursor = 0
    for run in runs:
        pieces.append(text[cursor:run.start()])
        pieces.append(' ' if run.span() == dropped else run[0])
        cursor = run.end()
    pieces.append(text[cursor:])
    tokens = re.findall(r'[a-z]+|\d+', ''.join(pieces).casefold())
    # A bare-number id names nothing and would collide with the next numeric
    # garble, so a text with no surviving word has no identity.
    return '_'.join(tokens) if any(t.isalpha() for t in tokens) else None


def _counter(boxes: tuple[ocr.TextBox, ...],
            offset: int) -> tuple[int | None, int | None]:
    """The 'completed N/M' pair, or (None, None) when it is not certain."""
    band = _shift(_COMPLETED, offset)
    matches = [m for m in (_COMPLETED_TEXT.fullmatch(b.text.strip())
                           for b in boxes if _trusted(b) and _inside(band, b.rect)) if m]
    if len(matches) != 1:
        return None, None
    done, total = int(matches[0][1]), int(matches[0][2])
    return (done, total) if done <= total else (None, None)


def _chest_state(screen: Image, centre_x: int, title_y: int) -> str:
    """Classify the recorded lock, glow and green-check art conservatively."""
    region = screen[title_y + 161:title_y + 296, centre_x - 77:centre_x + 78]
    if region.shape != (135, 155, 3):
        return 'unreadable'
    blue, green, red = region[:, :, 0], region[:, :, 1], region[:, :, 2]
    white = int(((blue > 215) & (green > 215) & (red > 215)).sum())
    checked = int(((green > 110) & (green > red * 1.2)
                   & (green > blue * 1.15)).sum())
    glowing = int(((red > 145) & (blue > 100) & (red > green * 1.35)
                   & (blue > green * 1.15)).sum())
    if checked >= 700:
        return 'claimed'
    if white >= 2500 and glowing >= 1500:
        return 'claimable'
    if white >= 500 and glowing < 800:
        return 'locked'
    return 'unreadable'


def _milestones(screen: Image, boxes: tuple[ocr.TextBox, ...],
                completed: int | None, offset: int) -> tuple[MilestoneEntry, ...]:
    band = _shift(_MILESTONES, offset)
    found = [b for b in boxes if _inside(band, b.rect)
             and _INTEGER.fullmatch(b.text.strip())
             and int(b.text) in range(5, 36, 5)]
    thresholds = Counter(int(b.text) for b in found if _trusted(b))
    anchors = sorted((int(box.text), box.rect.x + box.rect.w / 2, box.confidence)
                     for box in found if _trusted(box) and thresholds[int(box.text)] == 1)
    measured = [(int(box.text), box.rect.x + box.rect.w / 2, box.confidence)
                for box in found]
    if len(anchors) >= 2 and all(count == 1 for count in thresholds.values()):
        slopes = [(right[1] - left[1]) / ((right[0] - left[0]) / 5)
                  for left, right in zip(anchors, anchors[1:])]
        spacing = statistics.median(slopes)
        origin = statistics.median(x - spacing * (threshold / 5 - 1)
                                   for threshold, x, _ in anchors)
        if (180 <= spacing <= 210
                and all(abs(x - (origin + spacing * (threshold / 5 - 1))) <= 15
                        for threshold, x, _ in anchors)):
            measured = [(threshold, origin + spacing * (threshold / 5 - 1),
                         min(confidence for _, _, confidence in anchors))
                        for threshold in range(5, 36, 5)
                        if 77 <= origin + spacing * (threshold / 5 - 1)
                        <= screen.shape[1] - 78]
    entries: list[MilestoneEntry] = []
    title_y = _RECORDED_TITLE_Y + offset
    for threshold, x, confidence in sorted(measured, key=lambda item: item[1]):
        # A repeated threshold is two chests claiming one identity, and an
        # unread counter is no evidence at all. Neither may become 'locked'.
        status = ('unreadable' if confidence < _MIN_CONFIDENCE or thresholds[threshold] > 1
                  or completed is None else
                  'unlocked' if completed >= threshold else 'locked')
        centre_x = round(x)
        state = (_chest_state(screen, centre_x, title_y)
                 if status != 'unreadable' else 'unreadable')
        entries.append(MilestoneEntry(
            threshold, status, state, confidence,
            (centre_x - 72, title_y + 195, 144, 122)))
    return tuple(entries)


def _cards(screen: Image) -> tuple[Rect, ...]:
    """The mission cards, top to bottom, by their measured bordered bounds."""
    return tuple(sorted(
        (rect for rect in tiles.candidates(screen)
         if _CARD_MIN_W <= rect.w <= _CARD_MAX_W and _CARD_MIN_H <= rect.h <= _CARD_MAX_H),
        key=lambda rect: rect.y))


def _claim_boxes(card: Rect, boxes: Iterable[ocr.TextBox]) -> list[ocr.TextBox]:
    """The CLAIM button candidates inside one card.

    Measures the same two fractions as the bar and the reward column:
    below _PROGRESS_TOP_FRACTION down the card and left of
    _REWARD_LEFT_FRACTION across it - exactly where a bar would sit, since
    the button is what replaces it. Shared by _mission and claim_targets so
    the two can never select a different box for the same card.
    """
    reward_edge = card.x + card.w * _REWARD_LEFT_FRACTION
    progress_edge = card.y + card.h * _PROGRESS_TOP_FRACTION
    return [b for b in boxes if _inside(card, b.rect) and _trusted(b)
            and b.rect.x < reward_edge and b.rect.y >= progress_edge
            and tiles.normalise(b.text) == _CLAIM_LABEL]


def _mission(card: Rect, boxes: tuple[ocr.TextBox, ...]) -> MissionEntry | None:
    inside = sorted((b for b in boxes if _inside(card, b.rect)),
                    key=lambda b: (b.rect.y, b.rect.x))
    if not inside:
        return None
    reward_edge = card.x + card.w * _REWARD_LEFT_FRACTION
    progress_edge = card.y + card.h * _PROGRESS_TOP_FRACTION

    reward_boxes = [b for b in inside if b.rect.x >= reward_edge]
    rewards = tuple(int(b.text) for b in reward_boxes
                    if _trusted(b) and _INTEGER.fullmatch(b.text.strip()))
    # The two reward numbers are told apart by their icons, which this reader
    # does not read. They are reported as values in the order they appear and
    # no currency is named for them.
    rewards_status = ('observed' if reward_boxes and len(rewards) == len(reward_boxes)
                      else 'unreadable')

    bars = [m for m in (_PROGRESS.fullmatch(b.text.strip().replace(' ', ''))
                        for b in inside
                        if b.rect.x < reward_edge and b.rect.y >= progress_edge
                        and _trusted(b)) if m]
    progress, target = (int(bars[0][1]), int(bars[0][2])) if len(bars) == 1 else (None, None)
    if progress is not None and target is not None and (target <= 0 or progress > target):
        progress, target = None, None

    claim_boxes = _claim_boxes(card, inside)
    text_boxes = [b for b in inside if b.rect.x < reward_edge and b.rect.y < progress_edge]
    raw_text = ' '.join(b.text for b in text_boxes)

    # A bar is what the button replaces, so a card appearing to hold both is a
    # misread of one of them. Nothing here can say which, so neither is
    # trusted over the other and the card has no reading at all.
    if claim_boxes and bars:
        return MissionEntry(None, raw_text, None, None, 'unreadable', rewards,
                            rewards_status, 0., (card.x, card.y, card.w, card.h))
    claimable = len(claim_boxes) == 1

    trusted_text = bool(text_boxes) and all(_trusted(b) for b in text_boxes)
    mission_id = identity_of(raw_text, target, complete=claimable) if trusted_text else None

    if mission_id is None:
        status = 'unreadable'
    elif claimable:
        # No progress and no target, and that is not a failed read: a finished
        # card states its completeness by replacing the bar with the button.
        status = 'claimable'
    elif progress is None or target is None:
        status = 'unreadable'
    else:
        status = 'available' if progress < target else 'complete'
    return MissionEntry(
        mission_id, raw_text, progress, target, status, rewards, rewards_status,
        min((b.confidence for b in text_boxes), default=0.) if trusted_text else 0.,
        (card.x, card.y, card.w, card.h))


def claim_targets(reading: MissionsReading,
                  boxes: tuple[ocr.TextBox, ...]) -> tuple[ClaimTarget, ...]:
    """The CLAIM buttons for this reading's claimable cards, top to bottom.

    Driven by the status the reader already assigned rather than by a second
    search of the frame, so a card this module refused to identify can never
    acquire a tap target by another route.

    One button per card and only where the card holds exactly one: a doubled
    or missing read yields no target for that card rather than a guessed one.
    """
    found: list[ClaimTarget] = []
    for entry in sorted(reading.missions, key=lambda m: m.rect[1]):
        if entry.status != 'claimable':
            continue
        card = Rect(*entry.rect)
        hits = _claim_boxes(card, boxes)
        if len(hits) == 1:
            # The reward column holds two numbers told apart only by their
            # icons, which this reader does not read - so they are named by
            # ORDER, coins then gems, and only when the column was read
            # cleanly and holds exactly the two the card is drawn with.
            coins, gems = (entry.reward_values
                           if entry.rewards_status == 'observed'
                           and len(entry.reward_values) == 2 else (None, None))
            found.append(ClaimTarget(
                entry.mission_id, entry.raw_text, coins, gems,
                (hits[0].rect.x, hits[0].rect.y, hits[0].rect.w, hits[0].rect.h)))
    return tuple(found)


def parse_frame(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
                now: float | None = None, locale: str = 'en') -> MissionsReading | None:
    """Read the recorded Daily Missions page, or nothing at all.

    The page must first identify itself through screen_discovery, so an
    unrecognised frame yields None rather than a partial reading assembled
    from whatever text happened to land in the measured bands.

    `boxes`, when supplied, MUST be a read of THIS `screen`. Nothing here
    verifies that pairing - only pixel-level checks touch `screen` itself -
    so a foreign box set is read with full confidence as this frame.
    """
    observed_at = time.time() if now is None else now
    if not math.isfinite(observed_at):
        return None
    discovery = screen_discovery.discover(screen, boxes, 'missions', locale=locale)
    if not discovery.readable or discovery.screen_id != 'missions.daily':
        return None
    # This page's origin. discover already required exactly one title at a
    # bounded distance above the banner; this reads its y back so every band
    # below is measured from the page rather than from the frame edge.
    offset = _title_offset(boxes)
    if offset is None:
        return None
    completed, completed_target = _counter(boxes, offset)
    counted = screen_discovery.missions_count(boxes)
    shown, offered = counted if counted else (None, None)
    cards = _cards(screen)
    missions = tuple(m for m in (_mission(card, boxes) for card in cards) if m)
    # Two cards under one identity are ambiguous, and ambiguity is its own
    # answer: it is not 'unreadable' (both were read) and not 'available'.
    repeated = Counter(m.mission_id for m in missions if m.mission_id is not None)
    missions = tuple(replace(m, status='ambiguous')
                     if m.mission_id is not None and repeated[m.mission_id] > 1 else m
                     for m in missions)
    return MissionsReading(
        'missions.daily', observed_at, screen.shape[1], screen.shape[0],
        hashlib.sha256(screen.tobytes()).hexdigest(),
        completed, completed_target, shown, offered,
        offered - shown if shown is not None and offered is not None else None,
        len(cards), missions, _milestones(screen, boxes, completed, offset),
        # One viewport cannot show the entire weekly strip or all missions.
        complete=False)


class MissionsReadings:
    """The latest Daily Missions reading, for the scan loop and the API.

    Thread-safe, holds one reading and no history, and persists nothing. It
    keeps the same distinction the reader itself keeps: a frame this reader
    never examined reports `scanned` False, which is not the same fact as a
    frame examined and found to hold no missions page. Only the second may
    stand for "no missions page is up".
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reading: MissionsReading | None = None
        # The last reading that succeeded, kept after the page is left: a
        # visit walks away from the missions page before anyone reads its
        # result, so the current frame is nearly always a menu by then. It
        # carries its own `observed_at`, which is what says how old it is.
        self._latest: MissionsReading | None = None
        self._error: str | None = None
        self._scanned = False
        # Targets for the frame just scanned, and never for any other. A tap
        # located on one frame and made on the next is the failure this
        # module's whole style exists to prevent, so these are cleared by
        # every observe() that does not supply new ones.
        self._claims: tuple[ClaimTarget, ...] = ()
        self._reward_modal: WeeklyRewardModal | None = None

    def snapshot(self) -> dict[str, Any]:
        """Detached payload. Carries no coordinate: a card's or a chest's
        `rect` is reading evidence, and nothing here may become a tap target.

        `latest` is retained and may be from an earlier frame;
        `current_screen_id` is about the frame just scanned. Keeping them in
        one payload under two names is what stops a stale reading being read
        as "the missions page is up right now".
        """
        with self._lock:
            latest = None if self._latest is None else {
                k: v for k, v in asdict(self._latest).items() if k != 'frame_digest'}
            if latest is not None:
                # Both entry types name this field `rect`. Stripping it by
                # any other name is a filter that quietly ships it instead.
                for key in ('missions', 'milestones'):
                    latest[key] = [{k: v for k, v in entry.items() if k != 'rect'}
                                   for entry in latest[key]]
            current = (self._reading.screen_id if self._reading is not None else
                       WEEKLY_REWARD_SCREEN if self._reward_modal is not None else None)
            return {'latest': latest, 'current_screen_id': current,
                    'error': self._error, 'scanned': self._scanned}

    def current_evidence(self) -> dict[str, Any]:
        """What the last frame showed, for a transaction step to check."""
        with self._lock:
            screen_id = (self._reading.screen_id if self._reading is not None else
                         WEEKLY_REWARD_SCREEN if self._reward_modal is not None else None)
            return {'screen_id': screen_id, 'error': self._error, 'scanned': self._scanned}

    def claim_evidence(self) -> dict[str, Any]:
        """What a claim transaction needs from the frame just scanned.

        Separate from current_evidence rather than an extension of it:
        that payload is what every transaction tests home against, and
        widening it would make an unrelated caller's assertion depend on
        claiming.

        `completed` is the daily counter and is the success test for a claim -
        it moves the moment a reward is taken. `claims` are targets on THIS
        frame; an unscanned or unreadable frame carries none rather than the
        last frame's.
        """
        with self._lock:
            reading = self._reading
            return {'screen_id': (reading.screen_id if reading is not None else
                                  WEEKLY_REWARD_SCREEN if self._reward_modal is not None
                                  else None),
                    'error': self._error, 'scanned': self._scanned,
                    'completed': None if reading is None else reading.completed,
                    'completed_target': None if reading is None else reading.completed_target,
                    'claims': self._claims,
                    'reward_modal': self._reward_modal,
                    'chests': (() if reading is None else tuple(
                        ChestTarget(entry.threshold, entry.rect)
                        for entry in reading.milestones
                        if entry.status == 'unlocked'
                        and entry.claim_state == 'claimable')),
                    'milestones': (() if reading is None else tuple(
                        (entry.threshold, entry.claim_state)
                        for entry in reading.milestones)),
                    'visible': (() if reading is None else tuple(
                        (entry.mission_id, entry.raw_text, entry.status)
                        for entry in reading.missions))}

    def observe(self, reading: MissionsReading | None, *, error: str | None = None,
                scanned: bool = False,
                claims: tuple[ClaimTarget, ...] = (),
                reward_modal: WeeklyRewardModal | None = None) -> None:
        with self._lock:
            self._reading = reading
            if reading is not None:
                self._latest = reading
            self._error = error
            self._scanned = scanned or reading is not None
            self._claims = claims
            self._reward_modal = reward_modal

    def scan(self, screen: Image, *,
             boxes: tuple[ocr.TextBox, ...] | None = None,
             reward_modal: bool = False) -> bool:
        """Update from this frame; return whether all actions must hold.

        Actions hold whenever the missions page is up or might be: the bot
        has no verified target on that page, so a tap aimed at the menu
        underneath it would land somewhere nobody chose.

        `boxes` lets the caller hand in a frame read it already paid for. A
        tick runs more than one full-frame reader over the same frame, and
        RapidOCR's cost here is near-fixed rather than proportional to pixels
        - measured at 261.9 ms for a full frame - so a second read of the same
        bytes is a second full price for nothing.

        `boxes`, when supplied, MUST be a read of THIS `screen`. Nothing here
        verifies that pairing - only pixel-level checks (the shape guard
        below, screen_discovery's own frame checks, `_cards`) touch `screen`
        itself - so a foreign box set is accepted as a confident reading of a
        frame that is not actually on screen.
        """
        self.observe(None)
        if not supported_frame(screen.shape[1], screen.shape[0]):
            return False
        try:
            if boxes is None:
                boxes = ocr.read(screen, strict=True)
            if reward_modal:
                modal = weekly_reward_modal(screen, boxes)
                if modal is not None:
                    self.observe(None, scanned=True, reward_modal=modal)
                    return True
            if screen_discovery._missions_title(boxes) is None:
                # Examined the whole frame, and no missions title anywhere in
                # the band screen_discovery searches (0..MAX_TOP_INSET, wide
                # enough for every device this reader has measured). This is
                # the one branch that may stand for "no missions page is up".
                self.observe(None, scanned=True)
                return False
            reading = parse_frame(screen, boxes)
            self.observe(reading, scanned=True,
                         claims=claim_targets(reading, boxes) if reading else (),
                         error=None if reading else
                         'The missions page is up but could not be read reliably')
            return True
        except Exception:
            # Unscanned, not clear, and without the engine's own words: a
            # failed reader has looked at nothing.
            self.observe(None, error='Missions reading failed; actions held for this scan')
            return True
