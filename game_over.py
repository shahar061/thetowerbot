"""Passive readings of the recorded English Game Over ("GAME STATS") modal.

Five 1080x2400 captures back this module, all from the same status-bar-inset
device: a stable non-record modal (game_over, game_over_stats), a stable
non-record modal on a fuller account (game_over_wave1) and two modals where
the run set a new record (game_over_fade, game_over_newhigh). A record run
adds a "New Highest Wave!" line that pushes every caption below it down by
~97-102px while the panel re-centres, which is exactly why every field here
is found by its own caption rather than by a fixed offset from the frame or
from the modal title - the same rule digits.py documents for the production
death-modal reader this module does not replace.

Two independent layout axes are visible across the five captures, neither
tied to whether the run was a record:

  - the coins section is drawn as a single "coins earned" line on some
    captures and as a three-column "coins earned / ad coins earned / total
    coins" breakdown on others;
  - the BONUS banner in the top-right is drawn on some captures and not
    others.

Both are read as their own fields, `absent` when the game does not draw them
rather than folded into "coins_earned" or invented as zero.

Every value is read where the game actually put it. A label the game did not
draw is `absent`; a label the game drew whose value this reader could not
resolve - zero matching boxes, more than one, low confidence, or text that
does not parse as a plain count - is `unreadable`. Those two are never
merged: an account that has not unlocked ad-doubled coins looks nothing like
an account whose coin count OCR could not read, and a policy deciding whether
to retry must be able to tell them apart.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import time

import ocr
from config import Rect
from device import Image
from geometry import anchored_y, supported_frame

SCREEN_ID = 'game_over.result'

_MIN_CONFIDENCE = .90
# Covers the GAMESTATS title at both measured y's (671 stable, 621 on a
# record run) through the coins row at both measured y's, and stops well
# short of the Workshop panel bleeding through beneath the modal (Attack /
# Damage / Critical labels start at y=1785 on every recorded capture).
_MODAL = Rect(0, 550, 1080, 1050)
# The BONUS banner sits in the top-right HUD, above and outside the modal
# panel itself (measured at (897-900, 272-312, 125-130, 29-32) on the three
# captures that draw it), so it is bounded separately from `_MODAL`.
_BONUS_REGION = Rect(700, 200, 380, 200)
_TITLE = re.compile(r'game\s*stats', re.I)
_WAVE = re.compile(r'wave\s+(\d+)', re.I)
_NEW_HIGHEST = re.compile(r'new\s*highest\s*wave!?', re.I)
_TIER = re.compile(r'tier\s+(\d+)', re.I)
_HIGHEST_WAVE = re.compile(r'highest\s+wave:\s*(\d+)', re.I)
_KILLED_BY = re.compile(r'killed\s+by\s+(.+)', re.I)
_BONUS_LABEL = re.compile(r'bonus', re.I)
_BONUS_VALUE = re.compile(r'active|inactive', re.I)
_COINS_EARNED = re.compile(r'coins\s*earned', re.I)
_AD_COINS_EARNED = re.compile(r'ad\s*coins\s*earned', re.I)
_TOTAL_COINS = re.compile(r'total\s*coins', re.I)
_COUNT = re.compile(r'\d+(?:\.\d+)?[KMBT]?')

# How far below a caption its value may sit, and how far its value may drift
# sideways from the caption's own width - measured against the three-column
# coins row, whose values sit 62-95px below their caption and centred under
# it, never against the frame edge. See the module docstring for why.
_VALUE_MAX_DY = 150
_VALUE_X_PAD = 60


@dataclass(frozen=True)
class ResultField:
    """One modal field, read on its own. See the module docstring for the
    three-way split `status` keeps apart: `observed`, `absent`, `unreadable`.
    """

    key: str
    label: str
    raw_value: str | None
    status: str
    confidence: float
    rect: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class GameOverReading:
    screen_id: str
    observed_at: float
    frame_width: int
    frame_height: int
    frame_digest: str
    fields: tuple[ResultField, ...]


def _inside(box: ocr.TextBox, rect: Rect) -> bool:
    b = box.rect
    return (b.w > 0 and b.h > 0 and rect.x <= b.x and rect.y <= b.y
            and b.x + b.w <= rect.x + rect.w and b.y + b.h <= rect.y + rect.h)


def _trusted(box: ocr.TextBox) -> bool:
    return math.isfinite(box.confidence) and _MIN_CONFIDENCE <= box.confidence <= 1.


def _matches(boxes: tuple[ocr.TextBox, ...], pattern: re.Pattern[str],
             region: Rect = _MODAL) -> tuple[ocr.TextBox, ...]:
    return tuple(b for b in boxes if _inside(b, region) and pattern.fullmatch(b.text.strip()))


def _labelled(key: str, label: str, boxes: tuple[ocr.TextBox, ...], pattern: re.Pattern[str],
              *, missing: str, extract: re.Pattern[str] | None = None,
              region: Rect = _MODAL) -> ResultField:
    """A field whose own text carries its value (`Wave 6`, `Tier 1`, ...).

    `missing` is `absent` for a caption the game only sometimes draws and
    `unreadable` for one every recorded capture with this modal's title has
    drawn, so a frame this reader has confirmed IS the Game Over modal but
    that is somehow missing an always-drawn line reports a failed read, not
    a feature the account does not have.
    """
    matches = _matches(boxes, pattern, region)
    if len(matches) != 1 or not _trusted(matches[0]):
        return ResultField(key, label, None, missing if not matches else 'unreadable', 0., None)
    box = matches[0]
    raw = box.text.strip()
    if extract is not None:
        found = extract.search(raw)
        raw = found.group(1) if found else raw
    rect = box.rect
    return ResultField(key, label, raw, 'observed', box.confidence,
                       (rect.x, rect.y, rect.w, rect.h))


def _value_below(boxes: tuple[ocr.TextBox, ...], caption: ocr.TextBox,
                 region: Rect = _MODAL) -> tuple[ocr.TextBox, ...]:
    lo_x, hi_x = caption.rect.x - _VALUE_X_PAD, caption.rect.x + caption.rect.w + _VALUE_X_PAD
    lo_y = caption.rect.y + caption.rect.h
    hi_y = lo_y + _VALUE_MAX_DY
    return tuple(b for b in boxes if _inside(b, region) and b is not caption
                 and lo_y < b.rect.y <= hi_y and not (b.rect.x + b.rect.w < lo_x or b.rect.x > hi_x))


def _coin_column(key: str, label: str, boxes: tuple[ocr.TextBox, ...],
                 caption_pattern: re.Pattern[str], *, caption_missing: str = 'absent',
                 region: Rect = _MODAL) -> ResultField:
    """A coins caption plus the count the game draws beneath it.

    `ad_coins_earned` and `total_coins` are optional - the single-line
    layout never draws them at all, so no caption there is `absent`.
    `coins_earned` is drawn on every recorded capture, so its caller passes
    `caption_missing='unreadable'`: a frame confirmed to be this modal that
    somehow lacks it is a failed read, not a feature the account lacks. In
    both columns, once a caption IS present the count beneath it is not
    optional - the screenshots show a number is always drawn there - so a
    caption with no readable count below it is `unreadable`, never `absent`.
    """
    captions = _matches(boxes, caption_pattern, region)
    if len(captions) != 1 or not _trusted(captions[0]):
        return ResultField(key, label, None, caption_missing if not captions else 'unreadable', 0., None)
    caption = captions[0]
    values = _value_below(boxes, caption, region)
    if len(values) != 1 or not _trusted(values[0]) or not _COUNT.fullmatch(values[0].text.strip()):
        return ResultField(key, label, None, 'unreadable', 0., None)
    value = values[0]
    return ResultField(key, label, value.text.strip(), 'observed', value.confidence,
                       (value.rect.x, value.rect.y, value.rect.w, value.rect.h))


def _bonus_status(boxes: tuple[ocr.TextBox, ...]) -> ResultField:
    labels = _matches(boxes, _BONUS_LABEL, _BONUS_REGION)
    if len(labels) != 1 or not _trusted(labels[0]):
        return ResultField('bonus_status', 'Bonus', None, 'absent' if not labels else 'unreadable', 0., None)
    label = labels[0]
    values = _value_below(boxes, label, _BONUS_REGION)
    values = tuple(b for b in values if _BONUS_VALUE.fullmatch(b.text.strip()))
    if len(values) != 1 or not _trusted(values[0]):
        return ResultField('bonus_status', 'Bonus', None, 'unreadable', 0., None)
    value = values[0]
    return ResultField('bonus_status', 'Bonus', value.text.strip(), 'observed', value.confidence,
                       (value.rect.x, value.rect.y, value.rect.w, value.rect.h))


def parse_frame(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
                now: float | None = None, locale: str = 'en') -> GameOverReading | None:
    """Parse only the measured Game Over modal. Refuses anything else.

    Refusal (`None`) means what it means on every other reader in this
    codebase: wrong geometry, wrong locale, or the one anchor this reader
    trusts - the GAMESTATS title - not present at a trusted confidence. A
    title too faint to trust (a modal still fading in, say) is exactly the
    same refusal as no modal at all; this reader has no in-between "probably
    a death modal" answer to give a retry policy.
    """
    observed_at = time.time() if now is None else now
    height, width = screen.shape[:2]
    if not supported_frame(width, height) or locale != 'en' or not math.isfinite(observed_at):
        return None
    modal = Rect(_MODAL.x, anchored_y(_MODAL.y, height, 'center'),
                 _MODAL.w, _MODAL.h)
    titles = _matches(boxes, _TITLE, modal)
    if len(titles) != 1 or not _trusted(titles[0]):
        return None
    fields = (
        _labelled('wave', 'Wave', boxes, _WAVE, missing='unreadable',
                  extract=re.compile(r'(\d+)'), region=modal),
        _labelled('new_highest_wave', 'New Highest Wave', boxes, _NEW_HIGHEST,
                  missing='absent', region=modal),
        _labelled('tier', 'Tier', boxes, _TIER, missing='unreadable',
                  extract=re.compile(r'(\d+)'), region=modal),
        _labelled('highest_wave', 'Highest Wave', boxes, _HIGHEST_WAVE, missing='unreadable',
                  extract=re.compile(r'(\d+)'), region=modal),
        _labelled('killed_by', 'Killed By', boxes, _KILLED_BY, missing='unreadable',
                  extract=re.compile(r'killed\s+by\s+(.+)', re.I), region=modal),
        _bonus_status(boxes),
        _coin_column('coins_earned', 'Coins earned', boxes, _COINS_EARNED,
                     caption_missing='unreadable', region=modal),
        _coin_column('ad_coins_earned', 'Ad coins earned', boxes, _AD_COINS_EARNED,
                     region=modal),
        _coin_column('total_coins', 'Total coins', boxes, _TOTAL_COINS,
                     region=modal),
    )
    return GameOverReading(SCREEN_ID, observed_at, width, height,
                           hashlib.sha256(screen.tobytes()).hexdigest(), fields)
