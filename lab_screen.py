"""Read selected Labs slots and research rows without declaring route support."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import time

from device import Image
from geometry import supported_frame
from labs import LabEntry, LabJob, LabsReading
import ocr


_NAME_LEVEL = re.compile(r"^(?P<name>.+?)\s+Lv\.?\s*(?P<level>\d+)$", re.I)
_DURATION = re.compile(r"(\d+)\s*([dhms])", re.I)
_MIN_CONFIDENCE = .9
# "Unlock Nth lab" labels as OCR reads them ("2nd" is sometimes "Znd").
_ORDINALS = {2: ("2ND", "ZND"), 3: ("3RD",), 4: ("4TH",), 5: ("5TH",)}
# One Labs card spans about 392 px of a 2400 px frame. It bounds the last
# visible tile when no header follows it.
_TILE_PITCH = .165


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
    slot2_status: str = "unknown"
    slot2_price: int | None = None
    slot2_point: tuple[int, int] | None = None
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


def _gem_balance(boxes: tuple[ocr.TextBox, ...], width: int, height: int) -> int | None:
    candidates = [ocr.parse_number(box.text) for box in boxes
                  if _trusted(box) and width * .35 < box.rect.x < width * .55
                  and box.rect.y < height * .043]
    values = [value for value in candidates if value is not None]
    return values[0] if len(values) == 1 else None


def _enabled_card_border(screen: Image, name: ocr.TextBox) -> bool:
    # On the recorded picker, a disabled Game Speed card has a pink border
    # (BGR 138,138,255); an affordable card has a bright white border.
    # Refuse unmeasured colors instead of treating "not pink" as enabled.
    x, y = name.rect.x - 26, name.rect.y - 30
    if not (0 <= x < screen.shape[1] and 0 <= y < screen.shape[0]):
        return False
    blue, green, red = (int(channel) for channel in screen[y, x])
    return min(blue, green, red) >= 215


def read_slots(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
               observed_at: float) -> LabsReading:
    """Read owned cards independently; this observer never supplies tap targets.

    A next-slot lock or all five understood cards proves the owned strip.
    Cropped, duplicate or unrecognised cards retain unknown status instead of
    disappearing from a purportedly complete observation.
    """
    from concepts import REGISTRY

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
    titles = [b for b in valid if b.text.strip().upper() == "LAB"
              and b.rect.x < width * .2 and b.rect.y < height * .1]
    if len(titles) != 1:
        return empty
    headers: dict[int, list[ocr.TextBox]] = {}
    for box in valid:
        match = re.fullmatch(r"Lab\s+([1-5])", box.text.strip(), re.I)
        if match and box.rect.x < width * .25 and box.rect.y > titles[0].rect.y:
            headers.setdefault(int(match[1]), []).append(box)
    if not headers:
        return empty

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
            identities = [c.concept_id for c in REGISTRY.concepts if c.domain == "labs"
                          and c.name.casefold() == match["name"].casefold()]
            timers = [(b, _duration_seconds(b.text)) for b in within]
            timers = [(b, seconds) for b, seconds in timers if seconds is not None]
            target = int(match["level"])
            if len(identities) != 1 or len(timers) != 1 or target < 1:
                jobs.append(unknown)
                continue
            timer, seconds = timers[0]
            jobs.append(LabJob(slot, identities[0], name.text, observed_at + seconds,
                               seconds, None, "unknown", "researching",
                               min(name.confidence, timer.confidence), rect,
                               source_level=target - 1, target_level=target))
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
    gem_balance = _gem_balance(boxes, width, height)
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
    slot2_status = "unknown"
    slot2_price = None
    slot2_point = None
    if len(lab2_labels) == len(unlock_labels) == 1:
        slot2_status = "locked"
        prices = [box for box in boxes if _trusted(box)
                  and unlock_labels[0].rect.y < box.rect.y < height * .41
                  and width * .43 < box.rect.x < width * .65
                  and ocr.parse_number(box.text) is not None]
        if len(prices) == 1:
            slot2_price = ocr.parse_number(prices[0].text)
            slot2_point = (prices[0].rect.x + prices[0].rect.w // 2,
                           prices[0].rect.y + prices[0].rect.h // 2)
    elif len(lab2_labels) == len(third_labels) == len(third_unlocks) == 1:
        slot2_status = "owned"
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
                              balance, slots_owned, gem_balance, slot2_status,
                              slot2_price, slot2_point, next_locked=next_locked)
    if len(jobs) != 1 or offline:
        return LabHomeReading(True, "unknown", None, None, balance, slots_owned,
                              gem_balance, slot2_status, slot2_price, slot2_point,
                              next_locked=next_locked)

    match = _NAME_LEVEL.fullmatch(jobs[0].text.strip())
    assert match is not None
    name = match.group("name")
    from concepts import REGISTRY
    identities = [concept.concept_id for concept in REGISTRY.concepts
                  if concept.domain == "labs" and concept.name.casefold() == name.casefold()]
    timers = [_duration_seconds(box.text) for box in within]
    remaining = [value for value in timers if value is not None]
    if len(identities) != 1 or len(remaining) != 1:
        return LabHomeReading(True, "unknown", None, None, balance, slots_owned,
                              gem_balance, slot2_status, slot2_price, slot2_point,
                              next_locked=next_locked)
    job = LabJob(1, identities[0], jobs[0].text, time.time() + remaining[0],
                 remaining[0], None, "unknown", "researching", jobs[0].confidence,
                 tuple(jobs[0].rect))
    return LabHomeReading(True, "researching", job, None, balance, slots_owned,
                          gem_balance, slot2_status, slot2_price, slot2_point,
                          next_locked=next_locked)


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


def read_picker(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LabPickerReading:
    """Legacy Game Speed selection; general callers name a research explicitly."""
    return read_selected_picker(screen, boxes, research_id='labs.game-speed')


def read_selected_picker(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
                         research_id: str) -> LabPickerReading:
    """Read one named research row. Its geometry does not enable its route."""
    height, width = screen.shape[:2]
    titles = [box for box in boxes if _trusted(box)
              and _normalized(box.text) == "SELECTRESEARCH"
              and box.rect.y < height * .12]
    if len(titles) != 1:
        return LabPickerReading(False, None, None, None)
    balance = _coin_balance(boxes, width, height)
    names = [box for box in boxes if _trusted(box)
             and _research_identity(box.text) == research_id]
    if len(names) != 1:
        return LabPickerReading(True, None, balance, None)
    name = names[0]
    level_match = _NAME_LEVEL.fullmatch(name.text.strip())
    assert level_match is not None
    # Both columns contain prices. A price belongs to this row only within
    # the same half-width card, below its name and above the card's bottom.
    left = 0 if name.rect.x < width / 2 else width / 2
    right = left + width / 2
    prices = [ocr.parse_number(box.text) for box in boxes if _trusted(box)
              and left < box.rect.x < right and name.rect.y + height * .04
              < box.rect.y < name.rect.y + height * .085]
    amounts = [value for value in prices if value is not None]
    max_labels = [box for box in boxes if _trusted(box)
                  and _normalized(box.text) in ("MAX", "MAXED")
                  and left < box.rect.x < right
                  and name.rect.y + height * .04 < box.rect.y
                  < name.rect.y + height * .085]
    if len(max_labels) == 1 and not amounts:
        level = int(level_match.group("level"))
        return LabPickerReading(True, LabEntry(
            research_id, name.text, level, level, None, None,
            "maxed", name.confidence, tuple(name.rect)), balance, None)
    if len(amounts) != 1:
        return LabPickerReading(True, None, balance, None)
    price = amounts[0]
    durations = [_duration_seconds(box.text) for box in boxes
                 if _trusted(box) and left < box.rect.x < right
                 and name.rect.y + height * .04 < box.rect.y
                 < name.rect.y + height * .085]
    seconds = [value for value in durations if value is not None]
    affordable = balance is not None and balance >= price and _enabled_card_border(screen, name)
    entry = LabEntry(research_id, name.text, int(level_match.group("level")),
                     None, float(price), seconds[0] if len(seconds) == 1 else None,
                     "available" if affordable else "unavailable", name.confidence,
                     tuple(name.rect))
    point = (int(left + width * .27), int(name.rect.y + height * .04)) if affordable else None
    return LabPickerReading(True, entry, balance, point)


def read_confirmation(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LabConfirmationReading:
    """Read the modal's final coin-funded Research control, never the page behind it."""
    height, width = screen.shape[:2]
    names = [box for box in boxes if _trusted(box)
             and _research_identity(box.text) is not None
             and width * .2 < box.rect.x < width * .4
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
    if len(amounts) != 1 or name is None:
        return LabConfirmationReading(True, name, balance, None, None,
                                      cancel_point)
    button = buttons[0]
    point = (button.rect.x + button.rect.w // 2,
             button.rect.y + button.rect.h // 2)
    return LabConfirmationReading(True, name, balance, amounts[0], point,
                                  cancel_point)
