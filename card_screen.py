"""Cards observations and controls bounded by recorded English layouts.

See tests/fixtures/cards/README.md for measured geometry and source captures.
Only complete inventory tiles are read. Compact COMMON reward receipts and
one-card equipment are calibrated; stars, non-max counters and full scans are not.
"""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import hashlib
import math
import re
from pathlib import Path

import cv2

import card_catalog
from card_models import CardCapabilities, CardFieldEvidence, CardItem, CardObservationField, CardSnapshot, RewardItem
from device import Image
from evidence_scope import FactScope
import ocr
import screen_discovery
import tiles
from account_screens import ControlTarget

EMPTY_LAYOUT = 'cards.empty.1080x2400.en'
STOCKED_LAYOUT = 'cards.stocked.1080x2400.en'
COMPACT_LAYOUT = 'cards.compact.1080x2400.en'
REWARD_LAYOUT = 'cards.single-reward.1080x2400.en'
_MIN_CONFIDENCE = .90
_MARKERS = Path(__file__).parent / 'templates' / 'cards'


def layout_id(boxes: tuple[ocr.TextBox, ...]) -> str | None:
    """Use the same complete anchor profile as passive page discovery."""
    return screen_discovery.cards_layout_id(boxes)


def capabilities(layout_id: str | None) -> CardCapabilities:
    """Recognition is separate from calibrated confirmation/action sequences."""
    supported = layout_id in (EMPTY_LAYOUT, STOCKED_LAYOUT, COMPACT_LAYOUT)
    reasons = {
        'buy_one': 'reward_sequence_not_calibrated',
        'buy_ten': 'batch_reward_sequence_not_recorded',
        'buy_slot': 'slot_confirmation_and_result_not_recorded',
        'assign': 'equipment_toggle_sequence_not_calibrated',
    }
    if not supported:
        reasons['inventory'] = 'unsupported_layout'
    calibrated = layout_id == COMPACT_LAYOUT
    if calibrated:
        reasons.pop('buy_one')
        reasons.pop('assign')
    return CardCapabilities(inventory=supported, buy_one=calibrated, buy_ten=False,
                            buy_slot=False, assign=calibrated, layout_id=layout_id, reasons=reasons)


@lru_cache(maxsize=2)
def _marker(name: str) -> Image | None:
    return cv2.imread(str(_MARKERS / f'{name}.png'))


def _marker_confidence(screen: Image, name: str, rect: tuple[int, int, int, int]) -> float | None:
    """A close match supports presence only; missing/changed pixels say unknown."""
    template = _marker(name)
    x, y, w, h = rect
    area = screen[y:y + h, x:x + w]
    if template is None or area.shape[0] < template.shape[0] or area.shape[1] < template.shape[1]:
        return None
    score = float(cv2.minMaxLoc(cv2.matchTemplate(area, template, cv2.TM_CCOEFF_NORMED))[1])
    return min(score, 1.) if score >= .95 else None


def _inside(box: ocr.TextBox, x: int, y: int, w: int, h: int) -> bool:
    r = box.rect
    return x <= r.x and y <= r.y and r.x + r.w <= x + w and r.y + r.h <= y + h


def _tile_drawn(screen: Image, x: int, y: int) -> bool:
    """The recorded bright top edge must actually accompany the OCR label."""
    edge = screen[y:y + 15, x + 15:x + 220]
    bright = (edge[:, :, 0] > 180) & (edge[:, :, 1] > 180)
    return float(bright.mean()) > .10


def read_items(screen: Image, boxes: tuple[ocr.TextBox, ...], *, now: float,
               scope: FactScope | None = None, visit_id: str | None = None) -> tuple[CardItem, ...]:
    """Observed full tiles only; callers must first validate page discovery."""
    layout = layout_id(boxes)
    if layout not in (STOCKED_LAYOUT, COMPACT_LAYOUT):
        return ()
    heading_y = (945, 985) if layout == STOCKED_LAYOUT else (824, 864)
    headings = [b for b in boxes if tiles.normalise(b.text) == 'inventory'
                and _MIN_CONFIDENCE <= b.confidence <= 1. and heading_y[0] <= b.rect.y <= heading_y[1]
                and 350 <= b.rect.x <= 400]
    if len(headings) != 1:
        return ()
    # INVENTORY at y965 (stocked) or y844 (compact): first tile +89, width240,
    # height299; row pitch351 and column pitch257 in this recorded grid.
    origin_y = headings[0].rect.y + 89
    digest = hashlib.sha256(screen.tobytes()).hexdigest()
    found: list[CardItem] = []
    for row in range(3):
        y = origin_y + row * 351
        for col in range(4):
            x = 34 + col * 257
            if not _tile_drawn(screen, x, y):
                continue
            # Use all boxes, including uncertain ones, so duplicate or damaged
            # labels cannot be silently discarded to leave a convincing name.
            names = [b for b in boxes if _inside(b, x, y + 5, 240, 53)]
            if not names or any(not _MIN_CONFIDENCE <= b.confidence <= 1. for b in names):
                continue
            names.sort(key=lambda b: b.rect.x)
            card_id = card_catalog.resolve_legacy_name(' '.join(b.text for b in names))
            if card_id is None:
                continue
            counters = [b for b in boxes if _inside(b, x, y + 294, 240, 52)]
            maxed = (True if len(counters) == 1 and _MIN_CONFIDENCE <= counters[0].confidence <= 1.
                     and tiles.normalise(counters[0].text) == 'max' else None)
            equipped_score = _marker_confidence(screen, 'equipped_check', (x + 188, y + 260, 74, 74))
            locked_score = _marker_confidence(screen, 'battle_lock', (x + 3, y + 51, 68, 81))
            scores = [b.confidence for b in names + (counters if maxed else [])]
            scores += [score for score in (equipped_score, locked_score) if score is not None]
            found.append(CardItem(
                card_id=card_id, ownership='owned',
                level=card_catalog.max_level(card_id) if maxed else None,
                maxed=maxed,
                equipped=True if equipped_score is not None else None,
                battle_locked=True if locked_score is not None else None,
                observed_at=now, evidence_ref=f'{digest}:{row}:{col}',
                frame_digest=digest, layout_id=layout,
                confidence=min(scores),
            ))
    counts = Counter(item.card_id for item in found)
    return tuple(item.model_copy(update={'field_evidence': _field_evidence(item, scope=scope, visit_id=visit_id)})
                 for item in found if counts[item.card_id] == 1)


def read_snapshot(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
                  scope: FactScope, visit_id: str, now: float) -> CardSnapshot | None:
    """A single page never proves collection completeness or scroll endpoints."""
    if not math.isfinite(now) or now < 0:
        return None
    discovery = screen_discovery.discover(screen, boxes, 'cards')
    if not discovery.readable or discovery.screen_id != 'cards.inventory':
        return None
    counts = screen_discovery.cards_slots(boxes)
    if counts is None:
        return None
    equipped_count, capacity = counts
    anchors = [b for b in boxes if .9 <= b.confidence <= 1.
               and (tiles.normalise(b.text) in ('cards', 'active')
                    or b is screen_discovery.cards_slot_band(boxes))]
    items = read_items(screen, boxes, now=now, scope=scope, visit_id=visit_id)
    if sum(item.equipped is True for item in items) > equipped_count:
        return None
    digest = hashlib.sha256(screen.tobytes()).hexdigest()
    confidence = min(b.confidence for b in anchors)
    equipped: tuple[str, ...] | None = () if equipped_count == 0 else None
    if layout_id(boxes) == COMPACT_LAYOUT and capacity == equipped_count == 1:
        labels = [b for b in boxes if _inside(b, 265, 475, 255, 66)]
        if len(labels) == 1 and .9 <= labels[0].confidence <= 1.:
            identity = card_catalog.resolve_legacy_name(labels[0].text)
            if identity is not None and _tile_drawn(screen, 243, 483):
                equipped = (identity,)
                confidence = min(confidence, labels[0].confidence)
    equipment_evidence = CardFieldEvidence(
        observed_at=now, evidence_ref=digest, confidence=confidence,
        frame_digest=digest, layout_id=layout_id(boxes), scope=scope, visit_id=visit_id,
    ) if equipped is not None else None
    return CardSnapshot(scope=scope, revision=0, observed_at=now, visit_id=visit_id,
                        items=items, capacity=capacity,
                        frame_digest=digest, layout_id=layout_id(boxes),
                        confidence=confidence, equipment_evidence=equipment_evidence,
                        equipped=equipped,
                        collection_complete=False, equipment_complete=equipped is not None)


_OBSERVATION_FIELDS: tuple[CardObservationField, ...] = (
    'ownership', 'level', 'copies', 'copies_needed', 'maxed', 'equipped', 'battle_locked',
)


def _field_evidence(item: CardItem, *, scope: FactScope | None = None,
                    visit_id: str | None = None) -> dict[CardObservationField, CardFieldEvidence]:
    evidence = CardFieldEvidence(observed_at=item.observed_at, evidence_ref=item.evidence_ref,
                                 confidence=item.confidence, frame_digest=item.frame_digest,
                                 layout_id=item.layout_id, scope=scope, visit_id=visit_id)
    return {field: item.field_evidence.get(field, evidence) for field in _OBSERVATION_FIELDS
            if getattr(item, field) is not None and getattr(item, field) != 'unknown'}


def _merge_item(previous: CardItem, observed: CardItem) -> CardItem:
    old_evidence = _field_evidence(previous)
    new_evidence = _field_evidence(observed)
    changes: dict[str, object] = {}
    provenance = dict(new_evidence)
    for field in old_evidence:
        old = old_evidence[field]
        new = new_evidence.get(field)
        if (new is None or new.observed_at < old.observed_at
                or (new.observed_at == old.observed_at and getattr(previous, field) != getattr(observed, field))
                or (new.confidence is not None and new.confidence < _MIN_CONFIDENCE)):
            changes[field] = getattr(previous, field)
            provenance[field] = old_evidence[field]
    changes['field_evidence'] = provenance
    return observed.model_copy(update=changes)


def field_is_current(item: CardItem, field: CardObservationField, *,
                     scope: FactScope, visit_id: str) -> bool:
    """Scope/visit authority guard; missing legacy provenance is historical only.

    Callers still check freshness, goal semantics and action capabilities. The
    containing snapshot's visit must never confer authority on a retained field.
    """
    evidence = item.field_evidence.get(field)
    return (evidence is not None and evidence.scope == scope and evidence.visit_id == visit_id
            and getattr(item, field) is not None and getattr(item, field) != 'unknown')


def _equipment_evidence(snapshot: CardSnapshot) -> CardFieldEvidence:
    # A legacy snapshot cannot establish field-level scope/visit retrospectively.
    return snapshot.equipment_evidence or CardFieldEvidence(
        observed_at=snapshot.observed_at, evidence_ref=snapshot.frame_digest or 'legacy-equipment',
        confidence=snapshot.confidence, frame_digest=snapshot.frame_digest, layout_id=snapshot.layout_id,
    )


def merge_snapshots(previous: CardSnapshot | None, observed: CardSnapshot) -> CardSnapshot:
    """Retain same-account history without granting current-visit authority.

    Known fields retain original scope, visit, timestamp and evidence until a
    newer observation replaces them. Equal-time conflicting values keep prior
    evidence. Complete identity coverage drops absent IDs but still merges fields
    for IDs present. Global completeness always belongs to the new observation.
    Consumers must use field_is_current before applying a retained field.
    """
    if previous is None or previous.scope.account_id != observed.scope.account_id:
        return observed
    if (previous.scope == observed.scope and previous.visit_id == observed.visit_id
            and observed.observed_at < previous.observed_at):
        return previous
    old_items = {item.card_id: item for item in previous.items}
    merged = {} if observed.collection_complete else dict(old_items)
    for item in observed.items:
        merged[item.card_id] = _merge_item(old_items[item.card_id], item) if item.card_id in old_items else item
    changes: dict[str, object] = {'items': tuple(merged.values()), 'revision': previous.revision}
    if previous.equipped is not None:
        old_equipment = _equipment_evidence(previous)
        new_equipment = _equipment_evidence(observed) if observed.equipped is not None else None
        if (new_equipment is None or new_equipment.observed_at < old_equipment.observed_at
                or (new_equipment.observed_at == old_equipment.observed_at
                    and previous.equipped != observed.equipped)
                or (new_equipment.confidence is not None and new_equipment.confidence < _MIN_CONFIDENCE)):
            changes.update(equipped=previous.equipped, equipment_evidence=old_equipment,
                           equipment_complete=False)
    candidate = observed.model_copy(update=changes)
    if candidate == previous:
        return previous
    return candidate.model_copy(update={'revision': previous.revision + 1})


def read_rewards(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> tuple[RewardItem, ...] | None:
    """One settled recorded COMMON draw; CLAIM is absent during animation.

    Position zero belongs to the pinned single operation, not to an animation
    frame. Unsupported rarity/layout/batch remains unknown, never empty rewards.
    """
    if screen.shape != (2400, 1080, 3):
        return None
    claim = measured_box(boxes, r'CLAIM', (400, 1830, 280, 110))
    rarity = measured_box(boxes, r'COMMON', (345, 605, 410, 100))
    offer = measured_box(boxes, r'BUY\s*NEW\s*CARD', (150, 375, 400, 95))
    names = [b for b in boxes if _inside(b, 365, 770, 350, 95)]
    if (claim is None or rarity is None or offer is None or len(names) != 1
            or not .9 <= names[0].confidence <= 1. or not _tile_drawn(screen, 345, 767)):
        return None
    identity = card_catalog.resolve_legacy_name(names[0].text)
    return (RewardItem(position=0, card_id=identity, quantity=1),) if identity else None


def measured_box(boxes: tuple[ocr.TextBox, ...], pattern: str,
                 rect: tuple[int, int, int, int]) -> ocr.TextBox | None:
    """Unique complete OCR label in its recorded region; uncertainty is refusal."""
    candidates = [b for b in boxes if _inside(b, *rect) and re.fullmatch(pattern, b.text.strip(), re.I)]
    return candidates[0] if len(candidates) == 1 and .9 <= candidates[0].confidence <= 1. else None


def box_control(name: str, box: ocr.TextBox) -> ControlTarget:
    r = box.rect
    return ControlTarget(name, (r.x + r.w // 2, r.y + r.h // 2), 'located', box.confidence, tuple(r))


def exact_wallet(boxes: tuple[ocr.TextBox, ...], *, reward: bool = False) -> int | None:
    rect = (825, 20, 150, 100) if reward else (405, 10, 175, 90)
    value = measured_box(boxes, r'\d+', rect)
    return int(value.text) if value is not None and value.confidence >= .98 else None


def compact_controls(screen: Image, boxes: tuple[ocr.TextBox, ...],
                     snapshot: CardSnapshot) -> tuple[int | None, dict[str, ControlTarget]]:
    """Measured x1 label/price and current visible inventory identities only."""
    if snapshot.layout_id != COMPACT_LAYOUT:
        return None, {}
    controls: dict[str, ControlTarget] = {}
    single = measured_box(boxes, r'x1', (395, 235, 100, 90))
    price = measured_box(boxes, r'\d+', (545, 230, 100, 95))
    quote = int(price.text) if single is not None and price is not None and price.confidence >= .98 else None
    if quote is not None and quote > 0:
        controls['buy'] = box_control('buy', single)
    else:
        quote = None
    heading = measured_box(boxes, r'INVENTORY', (350, 824, 370, 85))
    if heading is not None:
        for item in snapshot.items:
            row, col = (int(value) for value in item.evidence_ref.rsplit(':', 2)[1:])
            x, y = 34 + col * 257, heading.rect.y + 89 + row * 351
            controls[item.card_id] = ControlTarget(item.card_id, (x + 120, y + 150),
                'located', item.confidence or 0., (x, y, 240, 299))
    return quote, controls
