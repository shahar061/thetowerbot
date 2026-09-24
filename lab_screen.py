"""Read the first Labs slot and the Game Speed research row from one frame."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time

from device import Image
from labs import LabEntry, LabJob
import ocr


_NAME_LEVEL = re.compile(r"^(?P<name>.+?)\s+Lv\.?\s*(?P<level>\d+)$", re.I)
_DURATION = re.compile(r"(\d+)\s*([dhms])", re.I)
_MIN_CONFIDENCE = .9


@dataclass(frozen=True)
class LabHomeReading:
    page: bool
    slot_status: str
    job: LabJob | None
    slot_point: tuple[int, int] | None
    coin_balance: int | None = None
    slots_owned: int | None = None


@dataclass(frozen=True)
class LabPickerReading:
    page: bool
    game_speed: LabEntry | None
    coin_balance: int | None
    buy_point: tuple[int, int] | None


@dataclass(frozen=True)
class LabConfirmationReading:
    page: bool
    name: str | None
    coin_balance: int | None
    price: int | None
    research_point: tuple[int, int] | None
    cancel_point: tuple[int, int] | None


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


def _enabled_card_border(screen: Image, name: ocr.TextBox) -> bool:
    # On the recorded picker, a disabled Game Speed card has a pink border
    # (BGR 138,138,255); an affordable card has a bright white border.
    # Refuse unmeasured colors instead of treating "not pink" as enabled.
    x, y = name.rect.x - 26, name.rect.y - 30
    if not (0 <= x < screen.shape[1] and 0 <= y < screen.shape[0]):
        return False
    blue, green, red = (int(channel) for channel in screen[y, x])
    return min(blue, green, red) >= 215


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
                              balance, slots_owned)
    if len(jobs) != 1 or offline:
        return LabHomeReading(True, "unknown", None, None, balance, slots_owned)

    match = _NAME_LEVEL.fullmatch(jobs[0].text.strip())
    assert match is not None
    name = match.group("name")
    from concepts import REGISTRY
    identities = [concept.concept_id for concept in REGISTRY.concepts
                  if concept.domain == "labs" and concept.name.casefold() == name.casefold()]
    timers = [_duration_seconds(box.text) for box in within]
    remaining = [value for value in timers if value is not None]
    if len(identities) != 1 or len(remaining) != 1:
        return LabHomeReading(True, "unknown", None, None, balance, slots_owned)
    job = LabJob(1, identities[0], jobs[0].text, time.time() + remaining[0],
                 remaining[0], None, "unknown", "researching", jobs[0].confidence,
                 tuple(jobs[0].rect))
    return LabHomeReading(True, "researching", job, None, balance, slots_owned)


def read_picker(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LabPickerReading:
    """Only the Game Speed card can produce a coin-funded purchase target."""
    height, width = screen.shape[:2]
    titles = [box for box in boxes if _trusted(box)
              and _normalized(box.text) == "SELECTRESEARCH"
              and box.rect.y < height * .12]
    if len(titles) != 1:
        return LabPickerReading(False, None, None, None)
    balance = _coin_balance(boxes, width, height)
    names = [box for box in boxes if _trusted(box)
             and re.fullmatch(r"Game\s*Speed\s*Lv\.?\s*\d+", box.text.strip(), re.I)]
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
            "labs.game-speed", name.text, level, level, None, None,
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
    entry = LabEntry("labs.game-speed", name.text, int(level_match.group("level")),
                     None, float(price), seconds[0] if len(seconds) == 1 else None,
                     "available" if affordable else "unavailable", name.confidence,
                     tuple(name.rect))
    point = (int(left + width * .27), int(name.rect.y + height * .04)) if affordable else None
    return LabPickerReading(True, entry, balance, point)


def read_confirmation(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LabConfirmationReading:
    """Read the modal's final coin-funded Research control, never the page behind it."""
    height, width = screen.shape[:2]
    names = [box for box in boxes if _trusted(box)
             and re.fullmatch(r"Game\s*Speed\s*Lv\.?\s*\d+", box.text.strip(), re.I)
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
