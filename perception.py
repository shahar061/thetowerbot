"""Read upgrade tiles and the battle HUD from one immutable screenshot."""
from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, replace

import cv2

import config
import ocr
import screen_discovery
import tiles
import upgrades
from device import Image

_NUMBER = re.compile(r"(?:x|\$)?\s*(\d+(?:\.\d+)?)\s*([KMBTqQ]?)\s*(?:%|/s|/sec|s|sec)?")
_MULTIPLIERS = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12, "q": 1e15, "Q": 1e18}
# The game speed readout, the health recovery rate and the pause banner. The
# `x` and the `/s` are required, not decoration: they are what separates the
# speed multiplier from a stat and the recovery rate from a health total, so
# an unlabelled number in the same band is never mistaken for either.
_SPEED = re.compile(r"x\s*(\d+(?:\.\d+)?)")
_REGEN = re.compile(r"([\d.]+[KMBTqQ]?)\s*/\s*s(?:ec)?")
_PAUSED = re.compile(r"game\s*paused", re.I)


def stat_number(text: str) -> float | None:
    match = _NUMBER.fullmatch(text.strip())
    if not match:
        return None
    result = float(match[1]) * _MULTIPLIERS[match[2]]
    return result if math.isfinite(result) else None


def price_number(text: str) -> int | None:
    if any(c in text for c in ("%", "/", "x", "s")):
        return None
    value = stat_number(text)
    return round(value) if value is not None else None


def contains(rect: config.Rect, box: config.Rect) -> bool:
    x, y = box.x + box.w / 2, box.y + box.h / 2
    return rect.x <= x < rect.x + rect.w and rect.y <= y < rect.y + rect.h


@dataclass(frozen=True)
class ObservedUpgrade:
    upgrade_id: str
    name: str
    category: str
    context: str
    value: float | None
    price: int | None
    status: str
    observed_at: float
    rect: config.Rect
    tap: tuple[int, int] | None

    confidence: float = 0.
    raw_name: str = ""
    raw_value: str | None = None

    @property
    def concept_id(self) -> str | None:
        entry = upgrades.by_id(self.upgrade_id)
        return entry.concept_id if entry and upgrades.resolve(self.name, self.category) == entry else None

    def payload(self) -> dict:
        return {k: getattr(self, k) for k in (
            "upgrade_id", "concept_id", "name", "category", "context", "value", "price", "status", "observed_at"
        )}


@dataclass(frozen=True)
class Observation:
    category: str | None
    rows: tuple[ObservedUpgrade, ...]
    combat: dict[str, float]
    cash: int | None
    observed_at: float
    heading_y: int | None = None
    context: str | None = None
    frame_digest: str | None = None
    frame_width: int = 0
    frame_height: int = 0
    # None means the frame never identified itself as a battle screen, which
    # is not the same as the game running: absence of the banner is evidence
    # only on a frame whose upgrade panel was read.
    paused: bool | None = None

    def status_for(self, upgrade_id: str) -> str:
        """Absence on this frame means unseen, never locked or unavailable."""
        matches = [row for row in self.rows if row.upgrade_id == upgrade_id]
        if not matches:
            return 'unseen'
        return matches[0].status if len(matches) == 1 else 'unreadable'


def parse_frame(
    screen: Image, boxes: tuple[ocr.TextBox, ...], context: str, *, now: float | None = None,
    digest: str | None = None,
) -> Observation:
    """`digest` is the frame's SHA-256 when the caller already has it
    (ocr.FrameReads.digest); None hashes the frame here, as before."""
    now = time.time() if now is None else now
    raw_boxes = boxes
    frame_digest = digest if digest is not None else hashlib.sha256(screen.tobytes()).hexdigest()
    evidence = dict(context=context, frame_digest=frame_digest,
                    frame_width=screen.shape[1], frame_height=screen.shape[0])
    boxes = tuple(b for b in boxes if b.confidence >= .9)
    headings = [(c, b) for c in ("ATTACK", "DEFENSE", "UTILITY") for b in boxes
                if tiles.normalise(b.text).replace("defence", "defense") == c.lower() + "upgrades"]
    if len(headings) != 1:
        return Observation(None, (), {}, None, now, **evidence)
    category, heading = headings[0]
    found = tuple(r for r in tiles.find_tiles(screen) if r.y > heading.rect.y)
    rows = []
    for rect in found:
        inside = sorted((b for b in boxes if contains(rect, b.rect)), key=lambda b: (b.rect.y, b.rect.x))
        markers = {b.text.strip().upper() for b in inside}
        labels = [b.text for b in inside if stat_number(b.text) is None
                  and b.text.strip().upper() not in ("MAX", "MAXED", "LOCKED", "UNAVAILABLE")
                  and not any(c.isdigit() for c in b.text)]
        raw_name = " ".join(labels)
        if not raw_name:
            continue
        entry = upgrades.resolve(raw_name, category)
        price_boxes = [b for b in inside if price_number(b.text) is not None
                       and b.rect.y >= rect.y + rect.h * config.TILE_PRICE_TOP_FRACTION]
        price_box = max(price_boxes, key=lambda b: b.rect.y, default=None)
        price = price_number(price_box.text) if price_box else None
        value_boxes = [b for b in inside
                  if b.rect.x > rect.x + rect.w * .5
                  and b.rect.y < rect.y + rect.h * config.TILE_PRICE_TOP_FRACTION
                  and stat_number(b.text) is not None]
        value = stat_number(value_boxes[0].text) if len(value_boxes) == 1 and not (entry and entry.unlock) else None
        status = "available" if price is not None else "unreadable"
        if markers & {"MAX", "MAXED"}:
            status, price = "maxed", None
        elif "LOCKED" in markers:
            status, price = "locked", None
        elif "UNAVAILABLE" in markers:
            status, price = "unavailable", None
        rows.append(ObservedUpgrade(
            entry.id if entry else "discovered:" + tiles.normalise(raw_name),
            entry.name if entry else raw_name, category, context, value, price, status, now,
            rect, (price_box.rect.x + price_box.rect.w // 2,
                   price_box.rect.y + price_box.rect.h // 2) if entry and price_box and price is not None else None,
            confidence=min([heading.confidence, *(b.confidence for b in raw_boxes if contains(rect, b.rect))]),
            raw_name=raw_name, raw_value=value_boxes[0].text if len(value_boxes) == 1 else None,
        ))
    counts = Counter(row.upgrade_id for row in rows)
    rows = [replace(row, status="unreadable", tap=None, price=None, confidence=0.)
            if counts[row.upgrade_id] > 1 else row for row in rows]
    combat: dict[str, float] = {}
    cash = None
    paused: bool | None = None
    if context == "battle":
        # The banner names itself, so it needs no geometry of its own; on an
        # identified battle frame its absence is what says the game is running.
        # The speed readout below is the independent second opinion, and the
        # two are kept as separate observations precisely so a disagreement
        # stays visible instead of averaging into one confident answer.
        paused = any(_PAUSED.fullmatch(b.text.strip()) for b in boxes)
        # The speed readout sits just above the upgrade heading, in the same
        # band config.SPEED_READOUT_REGION measures from the IN_RUN anchor -
        # recorded at y 1394..1398 against a heading at y 1659 in in_run_lit,
        # in_run_early, in_run_defense, in_run_utility and
        # in_run_attack_paused. Two candidates mean the band is not the
        # widget; refuse rather than pick one.
        speeds = [m for m in (_SPEED.fullmatch(b.text.strip()) for b in boxes
                              if heading.rect.y - 285 < b.rect.y < heading.rect.y - 235) if m]
        if len(speeds) == 1:
            combat["game_speed"] = float(speeds[0][1])
        # Constrain unlabeled numbers to their HUD, never the upgrade grid.
        hud = [b for b in boxes if heading.rect.y - 260 < b.rect.y < heading.rect.y]
        # Health recovery per second, in the same half of the HUD as the
        # health total it feeds. The regen tiles in the grid below carry the
        # same "/sec" text, which is exactly why this is bounded to the HUD.
        regens = [m for m in (_REGEN.fullmatch(b.text.strip()) for b in hud
                              if b.rect.x < screen.shape[1] / 2) if m]
        if len(regens) == 1:
            recovery = stat_number(regens[0][1])
            if recovery is not None and recovery >= 0:
                combat["health_regen"] = recovery
        waves = [(b, re.fullmatch(r"Wave\s*(\d+)", b.text, re.I)) for b in hud]
        waves = [(b, m) for b, m in waves if m]
        if len(waves) == 1:
            wave_box, wave_match = waves[0]
            combat["wave"] = int(wave_match[1])
            enemies = [b for b in hud if b.rect.x > wave_box.rect.x + wave_box.rect.w
                       and wave_box.rect.y - 65 < b.rect.y < wave_box.rect.y + 65
                       and stat_number(b.text) is not None]
            enemies.sort(key=lambda b: b.rect.y)
            if len(enemies) == 2 and enemies[0].rect.y < wave_box.rect.y:
                combat["enemy_damage"] = stat_number(enemies[0].text)
        health = [re.fullmatch(r"([\d.]+[KMBTqQ]?)\s*/\s*([\d.]+[KMBTqQ]?)", b.text)
                  for b in hud if b.rect.x < screen.shape[1] / 2]
        health = [m for m in health if m]
        if len(health) == 1:
            hp, cap = stat_number(health[0][1]), stat_number(health[0][2])
            if hp is not None and cap is not None and 0 <= hp <= cap and cap > 0:
                combat.update(health=hp, max_health=cap)
        cash_boxes = [b for b in boxes if b.rect.y < screen.shape[0] * .2
                      and b.rect.x < screen.shape[1] * .4 and b.text.strip().startswith("$")]
        if len(cash_boxes) == 1:
            cash = price_number(cash_boxes[0].text)
    return Observation(category, tuple(rows), combat, cash, now, heading.rect.y,
                       paused=paused, **evidence)


def _reread_values(screen: Image, observation: Observation) -> tuple[ocr.TextBox, ...]:
    """Value boxes the whole-frame read missed, re-read off padded crops.

    A lone digit - Damage "9" on a fresh account - gets no box at all from
    a whole-frame read, the same detection gap `ocr.CROP_PADDING` documents
    for the gems header, and a row without a value is never bought. Only
    priced rows with no value are re-read, so a frame that read cleanly
    costs nothing extra. Boxes come back in frame coordinates, inside the
    band parse_frame takes a value from.
    """
    found: list[ocr.TextBox] = []
    for row in observation.rows:
        entry = upgrades.by_id(row.upgrade_id)
        if row.value is not None or row.price is None or entry is None or entry.unlock:
            continue
        rect = row.rect
        band = config.Rect(rect.x + rect.w // 2 + 1, rect.y, rect.w - rect.w // 2 - 1,
                           int(rect.h * config.TILE_PRICE_TOP_FRACTION))
        boxes = [b for b in ocr.read_region(screen, band) if stat_number(b.text) is not None]
        if len(boxes) == 1:
            box = boxes[0]
            found.append(replace(box, rect=config.Rect(
                band.x + box.rect.x - ocr.CROP_PADDING, band.y + box.rect.y - ocr.CROP_PADDING,
                box.rect.w, box.rect.h)))
    return tuple(found)


def _shared_boxes(reads: ocr.FrameReads, context: str) -> tuple[ocr.TextBox, ...]:
    """The scan's shared read, with ocr.read()'s empty-on-error rule."""
    try:
        return reads.full()
    except Exception:  # noqa: BLE001 - a failed read degrades, never stops the scan
        return ()


def observe_frame(screen: Image, context: str, *, locale: str = 'en',
                  reads: ocr.FrameReads | None = None) -> Observation:
    """`reads`, when it holds THIS screen, supplies the scan's shared OCR and
    digest; None (tests, shopping) reads the frame here, as before."""
    if reads is not None and reads.screen is not screen:
        reads = None
    digest = reads.digest if reads is not None else None
    boxes = ocr.read(screen) if reads is None else _shared_boxes(reads, context)
    discovery = screen_discovery.discover(screen, boxes, context, locale=locale)
    # Empty OCR preserves frame evidence while preventing unsupported frames
    # from promoting account facts or exposing price/tap targets.
    if not discovery.readable:
        return parse_frame(screen, (), context, digest=digest)
    observation = parse_frame(screen, boxes, context, digest=digest)
    recovered = _reread_values(screen, observation)
    return parse_frame(screen, boxes + recovered, context, digest=digest) if recovered else observation


def read_cash(
    screen: Image,
    anchor: tuple[int, int],
    region: config.Region | None = None,
) -> int | None:
    """OCR the anchored wallet with margin so adjacent digits stay one word.

    `region` defaults to WALLET_REGION, which is measured from the IN_RUN
    panel anchor. Callers holding the cash counter's own match pass
    WALLET_FROM_CASH instead, so the read does not depend on the distance
    between the top and bottom of the screen.
    """
    region = config.WALLET_REGION if region is None else region
    x, y = anchor[0] + region.dx, anchor[1] + region.dy
    h, w = screen.shape[:2]
    if x < 0 or y < 0 or x + region.w > w or y + region.h > h:
        return None
    crop = screen[y:y + region.h, x:x + region.w]
    padded = cv2.copyMakeBorder(crop, 20, 20, 20, 20, cv2.BORDER_CONSTANT)
    boxes = ocr.read(padded)
    if len(boxes) != 1 or boxes[0].confidence < .9:
        return None
    return price_number(boxes[0].text)
