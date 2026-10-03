"""Read selected Labs slots and research rows without declaring route support."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import math
from pathlib import Path
import re
import time

import cv2

from config import Rect
from device import Image
from geometry import supported_frame
from labs import LabEntry, LabJob, LabsReading
import ocr


_NAME_LEVEL = re.compile(r"^(?P<name>.+?)\s*Lv\.?\s*(?P<level>\d+)$", re.I)
_DURATION = re.compile(r"(\d+)\s*([dhms])", re.I)
_MIN_CONFIDENCE = .9
# "Unlock Nth lab" labels as OCR reads them ("2nd" is sometimes "Znd").
_ORDINALS = {2: ("2ND", "ZND"), 3: ("3RD",), 4: ("4TH",), 5: ("5TH",)}
# One Labs card spans about 392 px of a 2400 px frame. It bounds the last
# visible tile when no header follows it.
_TILE_PITCH = .165


@dataclass(frozen=True)
class LabRepeatControl:
    """One calibrated on/off control belonging to a visible running lab."""
    slot: int
    state: str
    point: tuple[int, int]


@dataclass(frozen=True)
class LockedSlot:
    """The first locked lab tile: its slot, gem price and the price's centre."""
    slot: int
    price: int | None
    point: tuple[int, int] | None
    tile: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class LabHomeReading:
    page: bool
    slot_status: str
    job: LabJob | None
    slot_point: tuple[int, int] | None
    coin_balance: int | None = None
    slots_owned: int | None = None
    gem_balance: int | None = None
    next_locked: LockedSlot | None = None


@dataclass(frozen=True)
class LabPickerReading:
    page: bool
    game_speed: LabEntry | None
    coin_balance: int | None
    buy_point: tuple[int, int] | None

    @property
    def entry(self) -> LabEntry | None:
        """Selected research; game_speed remains a backwards-compatible field."""
        return self.game_speed


@dataclass(frozen=True)
class LabConfirmationReading:
    page: bool
    name: str | None
    coin_balance: int | None
    price: int | None
    research_point: tuple[int, int] | None
    cancel_point: tuple[int, int] | None

    @property
    def research_id(self) -> str | None:
        return _research_identity(self.name)

    @property
    def target_level(self) -> int | None:
        match = _NAME_LEVEL.fullmatch(self.name.strip()) if self.name else None
        return int(match['level']) if match else None


@dataclass(frozen=True)
class LabGemConfirmationReading:
    page: bool
    price: int | None
    gem_balance: int | None
    confirm_point: tuple[int, int] | None
    cancel_point: tuple[int, int] | None


@dataclass(frozen=True)
class PickerCard:
    lab_id: str | None
    raw_name: str
    level: int
    price: int | None
    seconds: float | None
    maxed: bool
    rect: tuple[int, int, int, int]
    name_box: ocr.TextBox
    fully_visible: bool
    border: str


@dataclass(frozen=True)
class PickerPage:
    open: bool
    balance: int | None
    viewport: tuple[int, int] | None
    cards: tuple[PickerCard, ...] = ()

    def card(self, lab_id: str) -> PickerCard | None:
        found = [card for card in self.cards if card.lab_id == lab_id]
        return found[0] if len(found) == 1 else None

    def signature(self) -> tuple[tuple[str, int], ...]:
        """What the list shows and where: equal on two frames means it did not move."""
        return tuple((card.raw_name, card.rect[1]) for card in self.cards)


def replace_card_y(card: PickerCard, dy: int) -> PickerCard:
    x, y, w, h = card.rect
    return replace(card, rect=(x, y + dy, w, h))


def _research_identity(name: str | None) -> str | None:
    from concepts import REGISTRY
    match = _NAME_LEVEL.fullmatch(name.strip()) if name else None
    if match is None:
        return None
    # OCR may drop or double inner spaces ("GameSpeed"), as the legacy
    # Game\s*Speed filter tolerated; identity must still be unique.
    wanted = re.sub(r'\s+', '', match['name']).casefold()
    identities = [c.concept_id for c in REGISTRY.concepts if c.domain == 'labs'
                  and re.sub(r'\s+', '', c.name).casefold() == wanted]
    return identities[0] if len(identities) == 1 else None


def _trusted(box: ocr.TextBox) -> bool:
    return box.confidence >= _MIN_CONFIDENCE


def _normalized(text: str) -> str:
    return "".join(text.upper().split())


def _duration_seconds(text: str) -> float | None:
    matches = _DURATION.findall(text)
    if not matches or _normalized(text) != _normalized("".join(f"{n}{unit}" for n, unit in matches)):
        return None
    factors = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return float(sum(int(number) * factors[unit.lower()] for number, unit in matches))


def _coin_balance(boxes: tuple[ocr.TextBox, ...], width: int, height: int) -> int | None:
    candidates = [ocr.parse_number(box.text) for box in boxes
                  if _trusted(box) and box.rect.x < width * .26
                  and box.rect.y < height * .043]
    values = [value for value in candidates if value is not None]
    return values[0] if len(values) == 1 else None


def _gem_balance(screen: Image, boxes: tuple[ocr.TextBox, ...], width: int, height: int) -> int | None:
    """The gem header, or None unless exactly one number is read there.

    The whole-frame read drops a short isolated balance outright: the "7"
    left by a 100-gem unlock produced no box at all, so the unlock could
    never be proven. With nothing in the band, the band is read off its own
    crop (see shopping.header_numbers), where a lone digit reads at .89-.92
    and is accepted below .9, like the lone "0" there. A misread digit only
    changes a balance too small to buy anything, and a debit proven against
    it must still equal the price.
    """
    band = [box for box in boxes if _trusted(box) and width * .35 < box.rect.x < width * .55
            and box.rect.y < height * .043]
    if not band:
        region = Rect(int(width * .35), 0, int(width * .2), int(height * .043))
        band = [box for box in ocr.read_region(screen, region)
                if box.confidence >= (.8 if len(box.text.strip()) == 1 else _MIN_CONFIDENCE)]
    candidates = [ocr.parse_number(box.text) for box in band]
    values = [value for value in candidates if value is not None]
    return values[0] if len(values) == 1 else None


def _card_border(screen: Image, name: ocr.TextBox) -> str:
    """The frame colour just above-left of a card's name: white, red, or unknown.

    The border shifts a few pixels vertically between picker rows. Require a
    continuous colour segment beside the title; a single bright pixel is not
    enough to authorize a purchase.
    """
    x = name.rect.x - 26
    if not 0 <= x < screen.shape[1]:
        return "unknown"
    white_run = red_run = 0
    seen: set[str] = set()
    for offset in range(-35, -14):
        y = name.rect.y + offset
        if not 0 <= y < screen.shape[0]:
            continue
        blue, green, red = (int(channel) for channel in screen[y, x])
        colour = ("white" if min(blue, green, red) >= 215 else
                  "red" if red >= 200 and red - max(blue, green) >= 80 else "unknown")
        white_run = white_run + 1 if colour == "white" else 0
        red_run = red_run + 1 if colour == "red" else 0
        if white_run >= 3:
            seen.add("white")
        if red_run >= 3:
            seen.add("red")
    return next(iter(seen)) if len(seen) == 1 else "unknown"


def _lab_title_and_headers(boxes: Sequence[ocr.TextBox], width: int, height: int
                           ) -> dict[int, list[ocr.TextBox]] | None:
    """The per-slot "Lab N" headers below the single page title.

    None means the page title wasn't read exactly once (a wrong page, or an
    unreadable one). An empty dict means the title read but no slot headers did.
    """
    titles = [b for b in boxes if b.text.strip().upper() == "LAB"
              and b.rect.x < width * .2 and b.rect.y < height * .1]
    if len(titles) != 1:
        return None
    headers: dict[int, list[ocr.TextBox]] = {}
    for box in boxes:
        match = re.fullmatch(r"Lab\s+([1-5])", box.text.strip(), re.I)
        if match and box.rect.x < width * .25 and box.rect.y > titles[0].rect.y:
            headers.setdefault(int(match[1]), []).append(box)
    return headers


@lru_cache(maxsize=2)
def _repeat_template(state: str) -> Image | None:
    path = Path(__file__).resolve().parent / "templates" / "nav" / f"lab_repeat_{state}.png"
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


def read_repeat_controls(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> tuple[LabRepeatControl, ...]:
    """Read recorded repeat icons; unknown, dimmed and ambiguous controls have no target.

    The timer anchors the search inside its own lab card. Absolute-intensity
    matching distinguishes the dim Off icon from On; a bright page title also
    rejects modal dimming, which must never be mistaken for an Off control.
    """
    height, width = screen.shape[:2]
    if not supported_frame(width, height) or screen.ndim != 3 or screen.shape[2] != 3:
        return ()
    valid = tuple(box for box in boxes if math.isfinite(box.confidence) and .9 <= box.confidence <= 1
                  and box.rect.w > 0 and box.rect.h > 0
                  and 0 <= box.rect.x < box.rect.x + box.rect.w <= width
                  and 0 <= box.rect.y < box.rect.y + box.rect.h <= height)
    headers = _lab_title_and_headers(valid, width, height)
    if not headers or any(len(headings) != 1 for headings in headers.values()):
        return ()
    title = next(box for box in valid if box.text.strip().upper() == "LAB"
                 and box.rect.x < width * .2 and box.rect.y < height * .1)
    crop = screen[title.rect.y:title.rect.y + title.rect.h, title.rect.x:title.rect.x + title.rect.w]
    if (crop.min(axis=2) >= 215).mean() < .15:
        return ()
    controls: list[LabRepeatControl] = []
    for slot, headings in sorted(headers.items()):
        heading = headings[0]
        bottom = min((items[0].rect.y for items in headers.values() if items[0].rect.y > heading.rect.y),
                     default=min(height, heading.rect.y + int(width * .4)))
        within = [box for box in valid if heading.rect.y + heading.rect.h <= box.rect.y
                  and box.rect.y + box.rect.h < bottom]
        names = [box for box in within if _NAME_LEVEL.fullmatch(box.text.strip())]
        timers = [box for box in within if _duration_seconds(box.text) is not None]
        if (len(names) != 1 or len(timers) != 1 or _research_identity(names[0].text) is None
                or any(_normalized(box.text) == "LABOFFLINE" for box in within)):
            continue
        name = _NAME_LEVEL.fullmatch(names[0].text.strip())
        if name is None or int(name["level"]) < 1:
            continue
        timer = timers[0]
        if timer.rect.x <= 130 or timer.rect.y < names[0].rect.y + names[0].rect.h:
            continue
        center_y = timer.rect.y + timer.rect.h // 2
        left, top, right, lower = 20, center_y - 48, 130, center_y + 48
        if top < heading.rect.y + heading.rect.h or lower >= bottom:
            continue
        region = screen[top:lower, left:right]
        matches: list[LabRepeatControl] = []
        for state, asset in (("enabled", "on"), ("disabled", "off")):
            template = _repeat_template(asset)
            if template is None or any(a < b for a, b in zip(region.shape[:2], template.shape[:2])):
                continue
            scores = cv2.matchTemplate(region, template, cv2.TM_SQDIFF_NORMED)
            components, _ = cv2.connectedComponents((scores <= .02).astype("uint8"))
            if components != 2:  # Background plus exactly one matching location cluster.
                continue
            _, _, point, _ = cv2.minMaxLoc(scores)
            point = (left + point[0] + template.shape[1] // 2,
                     top + point[1] + template.shape[0] // 2)
            if 67 <= point[0] <= 83 and abs(point[1] - center_y) <= 8:
                matches.append(LabRepeatControl(slot, state, point))
        if len(matches) == 1:
            controls.append(matches[0])
    return tuple(controls)


def read_slots(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
               observed_at: float) -> LabsReading:
    """Read owned cards independently; this observer never supplies tap targets.

    A next-slot lock or all five understood cards proves the owned strip.
    Cropped, duplicate or unrecognised cards retain unknown status instead of
    disappearing from a purportedly complete observation.
    """
    height, width = screen.shape[:2]
    digest = hashlib.sha256(screen.tobytes()).hexdigest()
    empty = LabsReading(observed_at, width, height, digest, None, "unknown", (), ())
    if not math.isfinite(observed_at) or observed_at < 0:
        return empty
    valid = tuple(box for box in boxes
                  if math.isfinite(box.confidence) and .9 <= box.confidence <= 1
                  and box.rect.w > 0 and box.rect.h > 0
                  and 0 <= box.rect.x < box.rect.x + box.rect.w <= width
                  and 0 <= box.rect.y < box.rect.y + box.rect.h <= height)
    headers = _lab_title_and_headers(valid, width, height)
    if not headers:
        return empty
    repeats = {control.slot: control.state for control in read_repeat_controls(screen, valid)}

    jobs: list[LabJob] = []
    locked: list[int] = []
    for slot, headings in sorted(headers.items()):
        heading = headings[0]
        bottom = min((h.rect.y for group in headers.values() for h in group
                      if h.rect.y > heading.rect.y), default=height)
        within = [b for b in valid if heading.rect.y + heading.rect.h <= b.rect.y
                  and b.rect.y + b.rect.h < bottom]
        rect = (0, heading.rect.y, width, bottom - heading.rect.y)
        unknown = LabJob(slot, None, "", None, None, None, "unknown", "unknown",
                         heading.confidence, rect)
        if len(headings) != 1:
            jobs.append(unknown)
            continue
        locks = [b for b in within if _normalized(b.text) in
                 {f"UNLOCK{number}LAB" for number in _ORDINALS.get(slot, ())}]
        offline = [b for b in within if _normalized(b.text) == "LABOFFLINE"]
        names = [b for b in within if _NAME_LEVEL.fullmatch(b.text.strip())]
        if len(locks) == 1 and not offline and not names:
            locked.append(slot)
            continue
        if locks:
            jobs.append(unknown)
        elif len(offline) == 1 and not names:
            jobs.append(LabJob(slot, None, offline[0].text, None, None, None,
                               "unknown", "idle", offline[0].confidence, rect))
        elif len(names) == 1 and not offline:
            name = names[0]
            match = _NAME_LEVEL.fullmatch(name.text.strip())
            assert match is not None
            # OCR may drop inner spaces ("DefenseAbsoluteLv.5"); identity ignores them.
            identity = _research_identity(name.text)
            timers = [(b, _duration_seconds(b.text)) for b in within]
            timers = [(b, seconds) for b, seconds in timers if seconds is not None]
            target = int(match["level"])
            if identity is None or len(timers) != 1 or target < 1:
                jobs.append(unknown)
                continue
            timer, seconds = timers[0]
            jobs.append(LabJob(slot, identity, name.text, observed_at + seconds,
                               seconds, None, "unknown", "researching",
                               min(name.confidence, timer.confidence), rect,
                               source_level=target - 1, target_level=target,
                               native_repeat=repeats.get(slot, "unknown")))
        else:
            jobs.append(unknown)

    boundary = min(locked) - 1 if locked else (5 if 5 in headers else None)
    complete = (boundary is not None and supported_frame(width, height)
                and {j.slot for j in jobs} == set(range(1, boundary + 1))
                and all(j.status in {"idle", "researching"} for j in jobs)
                and all(len(group) == 1 for group in headers.values())
                and [headers[n][0].rect.y for n in sorted(headers)]
                == sorted(headers[n][0].rect.y for n in headers))
    return LabsReading(observed_at, width, height, digest,
                       boundary if complete else None,
                       "observed" if complete else "unreadable", (), tuple(jobs))


def read_next_locked(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LockedSlot | None:
    """The first "Unlock Nth lab" tile (N = 2..5), its gem price and the price's centre.

    Slots unlock in order, so only the first locked tile matters. None means no
    locked tile was read. With a complete strip read, that means every slot is owned.
    A tile whose price box is missing or ambiguous keeps its slot but offers no point.
    """
    height, width = screen.shape[:2]
    trusted = [box for box in boxes if _trusted(box)]
    headers = _lab_title_and_headers(trusted, width, height)
    if headers is None:
        return None
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


def read_home(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LabHomeReading:
    """A named slot-1 card is required; other slots never authorize a tap."""
    height, width = screen.shape[:2]
    title = [box for box in boxes if _trusted(box) and box.text.strip().upper() == "LAB"
             and box.rect.x < width * .2 and box.rect.y < height * .1]
    slot = [box for box in boxes if _trusted(box) and box.text.strip().lower() == "lab 1"
            and box.rect.x < width * .2 and height * .08 < box.rect.y < height * .2]
    if len(title) != 1 or len(slot) != 1:
        return LabHomeReading(False, "unknown", None, None)

    balance = _coin_balance(boxes, width, height)
    # The new-account Labs page explicitly labels the next card "Unlock 2nd
    # lab". That is the only layout for which this reader knows the complete
    # owned strip; an older account may have several active slots below.
    second_locked = any(_trusted(box) and box.rect.y > slot[0].rect.y
                        and box.rect.y < height * .45
                        and _normalized(box.text) in ("UNLOCK2NDLAB", "UNLOCKZNDLAB")
                        for box in boxes)
    slots_owned = 1 if second_locked else None
    gem_balance = _gem_balance(screen, boxes, width, height)
    next_locked = read_next_locked(screen, boxes)
    lab2_labels = [box for box in boxes if _trusted(box)
                   and _normalized(box.text) == "LAB2"
                   and height * .22 < box.rect.y < height * .34]
    unlock_labels = [box for box in boxes if _trusted(box)
                     and _normalized(box.text) in ("UNLOCK2NDLAB", "UNLOCKZNDLAB")
                     and height * .28 < box.rect.y < height * .4]
    third_labels = [box for box in boxes if _trusted(box)
                    and _normalized(box.text) == "LAB3"
                    and height * .38 < box.rect.y < height * .52]
    third_unlocks = [box for box in boxes if _trusted(box)
                     and _normalized(box.text) == "UNLOCK3RDLAB"
                     and height * .44 < box.rect.y < height * .57]
    if (not len(lab2_labels) == len(unlock_labels) == 1
            and len(lab2_labels) == len(third_labels) == len(third_unlocks) == 1):
        slots_owned = 2

    next_slot = min((box.rect.y for box in boxes if _trusted(box)
                     and box.text.strip().lower() == "lab 2"
                     and box.rect.y > slot[0].rect.y), default=height * .37)
    within = [box for box in boxes if slot[0].rect.y + slot[0].rect.h < box.rect.y
              < next_slot and _trusted(box)]
    offline = [box for box in within if _normalized(box.text) == "LABOFFLINE"]
    jobs = [box for box in within if _NAME_LEVEL.fullmatch(box.text.strip())]
    if len(offline) == 1 and not jobs:
        return LabHomeReading(True, "idle", None,
                              (width // 2, (slot[0].rect.y + next_slot) // 2),
                              balance, slots_owned, gem_balance,
                              next_locked=next_locked)
    if len(jobs) != 1 or offline:
        return LabHomeReading(True, "unknown", None, None, balance, slots_owned,
                              gem_balance, next_locked=next_locked)

    identity = _research_identity(jobs[0].text)
    timers = [_duration_seconds(box.text) for box in within]
    remaining = [value for value in timers if value is not None]
    if identity is None or len(remaining) != 1:
        return LabHomeReading(True, "unknown", None, None, balance, slots_owned,
                              gem_balance, next_locked=next_locked)
    job = LabJob(1, identity, jobs[0].text, time.time() + remaining[0],
                 remaining[0], None, "unknown", "researching", jobs[0].confidence,
                 tuple(jobs[0].rect))
    return LabHomeReading(True, "researching", job, None, balance, slots_owned,
                          gem_balance, next_locked=next_locked)


def read_selected_home(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
                       slot: int, observed_at: float) -> LabHomeReading:
    """Selected idle-card geometry is observation, never route authorization."""
    from dataclasses import replace
    home = read_home(screen, boxes)
    reading = read_slots(screen, boxes, observed_at=observed_at)
    job = next((job for job in reading.jobs if job.slot == slot), None)
    if job is None:
        return replace(home, slot_status='unknown', job=None, slot_point=None,
                       slots_owned=reading.slots_owned)
    point = None
    if job.status == 'idle' and reading.strip_read() and job.rect is not None:
        x, y, width, height = job.rect
        point = (x + width // 2, y + height // 2)
    return replace(home, slot_status=job.status,
                   job=job if job.status == 'researching' else None,
                   slot_point=point, slots_owned=reading.slots_owned)


# Card geometry on the portrait picker: a card is half the width, and its price,
# duration and MAX label sit 0.04-0.085 h below its name; the card's frame spans
# about 0.03 h above the name to 0.095 h below it.
_CARD_ABOVE, _CARD_BELOW = .03, .095
_ROW_TOP, _ROW_BOTTOM = .04, .085


def read_picker_page(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> PickerPage:
    """Every "Name Lv.N" card on the SELECT RESEARCH panel, with what can be read of it."""
    height, width = screen.shape[:2]
    titles = [box for box in boxes if _trusted(box)
              and _normalized(box.text) == "SELECTRESEARCH" and box.rect.y < height * .12]
    if len(titles) != 1:
        return PickerPage(False, None, None)
    balance = _coin_balance(boxes, width, height)
    # The list scrolls between the History / Hide Completed row and the panel's bottom edge.
    controls = [box for box in boxes if _trusted(box)
                and _normalized(box.text) in ("HISTORY", "HIDECOMPLETED")]
    top = (max(box.rect.y + box.rect.h for box in controls) if controls
           else int(height * .18)) + int(height * .01)
    bottom = int(height * .905)
    cards = []
    for name in boxes:
        match = _NAME_LEVEL.fullmatch(name.text.strip()) if _trusted(name) else None
        if match is None or not top <= name.rect.y <= bottom:
            continue
        left = 0 if name.rect.x < width / 2 else width // 2
        right = left + width / 2

        def row(box: ocr.TextBox) -> bool:
            return (_trusted(box) and left < box.rect.x < right
                    and name.rect.y + height * _ROW_TOP < box.rect.y < name.rect.y + height * _ROW_BOTTOM)

        prices = [value for value in (ocr.parse_number(box.text) for box in boxes if row(box))
                  if value is not None]
        seconds = [value for value in (_duration_seconds(box.text) for box in boxes if row(box))
                   if value is not None]
        maxed = any(row(box) and _normalized(box.text) in ("MAX", "MAXED") for box in boxes)
        rect = (int(left), int(name.rect.y - height * _CARD_ABOVE), int(width / 2),
                int(height * (_CARD_ABOVE + _CARD_BELOW)))
        cards.append(PickerCard(
            _research_identity(name.text), name.text, int(match["level"]),
            prices[0] if len(prices) == 1 and not maxed else None,
            seconds[0] if len(seconds) == 1 else None, maxed, rect, name,
            rect[1] >= top - height * _CARD_ABOVE and rect[1] + rect[3] <= bottom,
            _card_border(screen, name)))
    return PickerPage(True, balance, (top, bottom), tuple(sorted(cards, key=lambda c: (c.rect[1], c.rect[0]))))


def read_picker(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LabPickerReading:
    """Legacy Game Speed selection; general callers name a research explicitly."""
    return read_selected_picker(screen, boxes, research_id='labs.game-speed')


def read_selected_picker(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
                         research_id: str) -> LabPickerReading:
    """Read one named research row. Its geometry does not enable its route."""
    height, width = screen.shape[:2]
    page = read_picker_page(screen, boxes)
    if not page.open:
        return LabPickerReading(False, None, None, None)
    card = page.card(research_id)
    if card is None:
        return LabPickerReading(True, None, page.balance, None)
    name = card.name_box
    if card.maxed:
        return LabPickerReading(True, LabEntry(
            research_id, name.text, card.level, card.level, None, None,
            "maxed", name.confidence, tuple(name.rect)), page.balance, None)
    if card.price is None:
        return LabPickerReading(True, None, page.balance, None)
    affordable = (page.balance is not None and page.balance >= card.price
                  and card.border == "white" and card.fully_visible)
    entry = LabEntry(research_id, name.text, card.level, None, float(card.price), card.seconds,
                     "available" if affordable else "unavailable", name.confidence, tuple(name.rect))
    left = 0 if name.rect.x < width / 2 else width / 2
    point = (int(left + width * .27), int(name.rect.y + height * .04)) if affordable else None
    return LabPickerReading(True, entry, page.balance, point)


def read_confirmation(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LabConfirmationReading:
    """Read the modal's final coin-funded Research control, never the page behind it."""
    height, width = screen.shape[:2]
    names = [box for box in boxes if _trusted(box)
             and _research_identity(box.text) is not None
             and width * .1 < box.rect.x
             and box.rect.x + box.rect.w < width * .9
             and width * .35 < box.rect.x + box.rect.w / 2 < width * .65
             and height * .32 < box.rect.y < height * .4]
    cancels = [box for box in boxes if _trusted(box)
               and box.text.strip().lower() == "cancel"
               and box.rect.x < width * .5
               and height * .65 < box.rect.y < height * .75]
    buttons = [box for box in boxes if _trusted(box)
               and box.text.strip().lower() == "research"
               and box.rect.x > width * .5
               and height * .65 < box.rect.y < height * .75]
    headings = [box for box in boxes if _trusted(box)
                and box.text.strip().upper() == "RESEARCH"
                and width * .3 < box.rect.x < width * .5
                and height * .26 < box.rect.y < height * .33]
    prices = [ocr.parse_number(box.text) for box in boxes if _trusted(box)
              and width * .25 < box.rect.x < width * .5
              and height * .56 < box.rect.y < height * .65]
    amounts = [value for value in prices if value is not None]
    visible = len(headings) == len(cancels) == len(buttons) == 1
    balance = _coin_balance(boxes, width, height)
    if not visible:
        return LabConfirmationReading(False, None, balance, None, None, None)
    cancel = cancels[0]
    cancel_point = (cancel.rect.x + cancel.rect.w // 2,
                    cancel.rect.y + cancel.rect.h // 2)
    name = names[0].text if len(names) == 1 else None
    if not amounts and name is not None:
        # The full-frame detector can miss the short coin price even when the
        # modal is otherwise complete. Read only its price area, away from the
        # duration and wallet, and still require one unambiguous number.
        region = Rect(int(width * .25), int(height * .58),
                      int(width * .25), int(height * .05))
        edge = ocr.CROP_PADDING + 4
        amounts = [value for box in ocr.read_region(screen, region)
                   if _trusted(box)
                   if (box.rect.x > edge and box.rect.y > edge
                       and box.rect.x + box.rect.w < ocr.CROP_PADDING + region.w - 4
                       and box.rect.y + box.rect.h < ocr.CROP_PADDING + region.h - 4)
                   if (value := ocr.parse_number(box.text)) is not None]
    if len(amounts) != 1 or name is None:
        return LabConfirmationReading(True, name, balance, None, None,
                                      cancel_point)
    button = buttons[0]
    point = (button.rect.x + button.rect.w // 2,
             button.rect.y + button.rect.h // 2)
    return LabConfirmationReading(True, name, balance, amounts[0], point,
                                  cancel_point)


def read_gem_unlock_confirmation(
    screen: Image, boxes: tuple[ocr.TextBox, ...]
) -> LabGemConfirmationReading:
    """Read only the Lab unlock's exact gem-spend dialog and its Yes/No labels."""
    height, width = screen.shape[:2]
    empty = LabGemConfirmationReading(False, None, None, None, None)
    if not supported_frame(width, height):
        return empty
    shift = (height - 2400) // 2
    trusted = [box for box in boxes if _trusted(box)]
    lab_titles = [box for box in trusted if box.text.strip().upper() == "LAB"
                  and box.rect.x < width * .2 and box.rect.y < height * .1]
    headings = [box for box in trusted if box.text.strip().upper() == "CONFIRMATION"
                and 250 < box.rect.x < 550 and 900 + shift < box.rect.y < 990 + shift]
    prompts = [box for box in trusted
               if _normalized(box.text) == "AREYOUSURETHATYOUWANTTOSPEND"
               and 100 < box.rect.x < 350 and 1000 + shift < box.rect.y < 1080 + shift]
    prices = [match for box in trusted
              if 180 < box.rect.x < 400 and 1050 + shift < box.rect.y < 1160 + shift
              if (match := re.fullmatch(r"([0-9][0-9,]*)GEMSTOUNLOCKTHISLAB\?",
                                        _normalized(box.text))) is not None]
    cancels = [box for box in trusted if box.text.strip().lower() == "no"
               and 180 < box.rect.x < 480 and 1230 + shift < box.rect.y < 1350 + shift]
    confirms = [box for box in trusted if box.text.strip().lower() == "yes"
                and 570 < box.rect.x < 900 and 1230 + shift < box.rect.y < 1350 + shift]
    if not all(len(group) == 1 for group in
               (lab_titles, headings, prompts, prices, cancels, confirms)):
        return empty
    balance = _gem_balance(screen, boxes, width, height)
    if balance is None:
        return empty
    confirm, cancel = confirms[0], cancels[0]
    return LabGemConfirmationReading(
        True, int(prices[0].group(1).replace(",", "")), balance,
        (confirm.rect.x + confirm.rect.w // 2, confirm.rect.y + confirm.rect.h // 2),
        (cancel.rect.x + cancel.rect.w // 2, cancel.rect.y + cancel.rect.h // 2))
