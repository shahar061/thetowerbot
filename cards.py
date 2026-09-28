"""Passive readings of the recorded English Cards page.

One capture backs this module: a 1080x2400 Cards page belonging to an account
that owns no card. It carries the page identity, the ACTIVE band - how many
cards are equipped, out of how many slots - and a locked next slot. It carries
no card row, no preset tab and no mastery tile, because none of those are
drawn on an empty collection.

So the slots are read exactly and the collection is not read at all. Those two
results are kept apart on purpose: an unobserved card contributes `unknown` to
an effective stat, never 0, and effective_stat_inputs() exists so a consumer
cannot total this page's contribution without first seeing which half of it
was actually observed.

Nothing here taps. Buying a card, buying a slot and equipping belong to C02.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
import time
from typing import Any

import ocr
import screen_discovery
from account_state import Evidence, Fact
from concepts import REGISTRY
from device import Image

SCREEN_ID = 'cards.inventory'

# The five ways an observation can be absent. They are listed together so it
# is obvious they are five and not one: a card nobody has looked at, a card
# the game shows behind a padlock, a card this account cannot hold, a card at
# its ceiling and a card whose text would not resolve are five different
# facts, and collapsing any of them into another - or into 0 - is the failure
# this module exists to prevent.
MISSING_STATES = ('unknown', 'locked', 'unavailable', 'maxed', 'unreadable')

# Identities come from the catalog, never from a string typed here.
CARD_IDS = tuple(c.concept_id for c in REGISTRY.concepts if c.kind == 'card')
MASTERY_IDS = tuple(c.concept_id for c in REGISTRY.concepts if c.kind == 'card-mastery')

# Slot counts are page facts, not card identities, and must not be mistaken
# for one. Three segments guarantees that: the registry validates every
# concept ID as exactly two, so no catalog entry can ever collide with these.
SLOT_EQUIPPED_KEY = 'cards.slots.equipped'
SLOT_CAPACITY_KEY = 'cards.slots.capacity'


def card_level_key(concept_id: str) -> str:
    """The fact key for one card's level: `cards.damage.level`.

    Three segments, like the slot keys, so it can never collide with a
    catalog identity (every one of those has exactly two).
    """
    return f'{concept_id}.level'


def card_copies_key(concept_id: str) -> str:
    """The fact key for how many copies of one card the account holds."""
    return f'{concept_id}.copies'

# Catalog kinds that would separate a passive bonus from an active ability.
# Registry v1 has neither - all 31 cards share the single kind `card` - so
# effect_kind() answers 'unknown' for every identity today, and will answer
# for real the moment the catalog carries the split. Classifying cards here
# instead would attach a rule to a name that no source ever verified.
_CATALOG_EFFECT_KINDS = {'passive-card': 'passive', 'active-card': 'active'}

_MIN_CONFIDENCE = .90
# The measured band of the "Unlock New Slot" tile's own label, at
# (589, 644, 183, 35) on the recorded page.
_NEXT_SLOT_Y = (624, 664)
_NEXT_SLOT_LABEL = 'unlocknew'


@dataclass(frozen=True)
class SlotReading:
    """How many cards are equipped, out of how many slots, and on what."""

    equipped: int | None
    capacity: int | None
    # 'locked' when the game drew a slot that has to be bought. 'unknown'
    # when it drew none: that is silence about further slots, not proof the
    # account has reached its last one.
    next_slot: str
    status: str
    confidence: float
    raw_value: str | None
    rect: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class CardObservation:
    """One catalog identity, and what this frame did or did not show of it."""

    concept_id: str
    domain: str
    effect_kind: str
    status: str
    copies: int | None
    level: int | None
    reason: str


@dataclass(frozen=True)
class CardsReading:
    screen_id: str
    observed_at: float
    frame_width: int
    frame_height: int
    frame_digest: str
    slots: SlotReading
    # () means "proven to be nothing equipped", None means "could not be
    # named". A calculator that treats those alike would silently assume an
    # empty loadout on an account that has a full one.
    equipped: tuple[str, ...] | None
    cards: tuple[CardObservation, ...]
    reason: str


def effect_kind(concept_id: str) -> str:
    """A card's passive/active split, taken from the catalog and nowhere else."""
    concept = REGISTRY.by_id(concept_id)
    return _CATALOG_EFFECT_KINDS.get(concept.kind, 'unknown') if concept else 'unknown'


def _unobserved(reason: str) -> tuple[CardObservation, ...]:
    """Every catalog identity, each explicitly not observed on this frame."""
    return tuple(CardObservation(concept_id, REGISTRY.by_id(concept_id).domain,
                                 effect_kind(concept_id), 'unknown', None, None, reason)
                 for concept_id in CARD_IDS + MASTERY_IDS)


def _next_slot(boxes: tuple[ocr.TextBox, ...]) -> str:
    """'locked' when one purchasable slot tile is drawn where it was measured."""
    matches = [b for b in boxes if b.confidence >= _MIN_CONFIDENCE
               and _NEXT_SLOT_Y[0] <= b.rect.y <= _NEXT_SLOT_Y[1]
               and b.text.replace(' ', '').casefold() == _NEXT_SLOT_LABEL]
    return 'locked' if len(matches) == 1 else 'unknown'


def parse_frame(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
                now: float | None = None, locale: str = 'en') -> CardsReading | None:
    """Read the Cards page, or return None having claimed nothing at all.

    Identity is delegated to screen_discovery, so this reader can never be
    the second opinion that talks the bot onto a page discovery refused.
    """
    observed_at = time.time() if now is None else now
    if not math.isfinite(observed_at):
        return None
    discovery = screen_discovery.discover(screen, boxes, 'cards', locale=locale)
    if not discovery.readable or discovery.screen_id != SCREEN_ID:
        return None
    band = screen_discovery.cards_slot_band(boxes)
    counts = screen_discovery.cards_slots(boxes)
    if band is None or counts is None:
        return None
    equipped, capacity = counts
    rect = band.rect
    slots = SlotReading(equipped, capacity, _next_slot(boxes), 'observed',
                        band.confidence, band.text.strip(),
                        (rect.x, rect.y, rect.w, rect.h))
    # Zero equipped is the one equipped set this page can state exactly: no
    # slot is filled, so there is nothing left to name. Any other count needs
    # card rows to name, and this capture proves none can be read.
    named: tuple[str, ...] | None = () if equipped == 0 else None
    reason = ('no_card_row_is_readable_on_this_page' if named is not None
              else 'equipped_identities_unreadable')
    return CardsReading(SCREEN_ID, observed_at, 1080, 2400,
                        hashlib.sha256(screen.tobytes()).hexdigest(), slots, named,
                        _unobserved('no_card_row_is_readable_on_this_page'), reason)


def equipment_known(reading: CardsReading | None) -> bool:
    """Whether the exact equipped set AND the slot counts are both known."""
    return (reading is not None and reading.equipped is not None
            and reading.slots.status == 'observed'
            and reading.slots.equipped is not None and reading.slots.capacity is not None)


def missing(reading: CardsReading | None) -> tuple[str, ...]:
    """Every identity this reading did not observe, named rather than counted."""
    if reading is None:
        return CARD_IDS + MASTERY_IDS
    return tuple(o.concept_id for o in reading.cards if o.status != 'observed')


def effective_stat_inputs(reading: CardsReading | None) -> dict[str, Any]:
    """What a stat calculator may use from this page, and what it may not.

    The acceptance gate in one call: equipment_known says whether the exact
    equipped cards and available slots are known before any effective stat is
    formed, and card_bonus stays None - never 0 - because no card level was
    ever observed. A consumer that wants a total has to look at these two
    answers first, and no arrangement of this payload offers it a zero.
    """
    slots = ({'equipped': reading.slots.equipped, 'capacity': reading.slots.capacity,
              'next_slot': reading.slots.next_slot} if reading is not None
             else {'equipped': None, 'capacity': None, 'next_slot': 'unknown'})
    return {'screen_id': reading.screen_id if reading is not None else None,
            'equipment_known': equipment_known(reading),
            'equipped': reading.equipped if reading is not None else None,
            'slots': slots,
            'card_bonus_known': False,
            'card_bonus': None,
            'unknown_identities': missing(reading),
            'reason': reading.reason if reading is not None else 'not_read'}


def facts(reading: CardsReading | None) -> tuple[Fact, ...]:
    """The permanent account facts this page supports.

    The two slot counts, plus a level and a copies fact for every card this
    frame actually observed. An identity with no observation behind it must
    reach the account revision as an absence, and the way to record an
    absence is to write nothing for it - so a card that is unknown, locked,
    unavailable, maxed or unreadable, or whose number is None, writes nothing.
    """
    if reading is None or reading.slots.status != 'observed' or reading.slots.rect is None:
        return ()
    evidence = Evidence(reading.observed_at, reading.slots.confidence, 'ACTIVE',
                        reading.slots.raw_value, reading.slots.rect, reading.frame_width,
                        reading.frame_height, reading.frame_digest)
    card_facts: list[Fact] = []
    for card in reading.cards:
        if card.status != 'observed':
            continue
        for key, value in ((card_level_key(card.concept_id), card.level),
                           (card_copies_key(card.concept_id), card.copies)):
            if value is not None:
                card_facts.append(Fact(key, value, 'observed',
                                       replace(evidence, raw_name=card.concept_id,
                                               raw_value=str(value))))
    return (Fact(SLOT_EQUIPPED_KEY, reading.slots.equipped, 'observed', evidence),
            Fact(SLOT_CAPACITY_KEY, reading.slots.capacity, 'observed', evidence),
            *card_facts)


def actions(reading: CardsReading | None) -> tuple[Any, ...]:
    """No tap is ever planned from this page.

    Buying a card, buying the locked slot and equipping are C02's, and each
    of them spends. Returning an empty plan for a page that was read
    perfectly is the point: reading a screen is not authority to touch it.
    """
    return ()
