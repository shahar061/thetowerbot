"""The recorded Cards page, and everything it deliberately refuses to claim.

One capture exists: menu_cards.png, a 1080x2400 English Cards page on an
account that owns no card at all. It proves the page identity, the ACTIVE
"0 / 1" slot band and the locked next slot. It proves nothing about a card
row, a preset, an in-run lock or a mastery, because none of those are drawn
on it - so those stay explicitly unknown rather than becoming zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import pytest

import config
import ocr

FIXTURES = Path(__file__).parent / 'fixtures'


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / 'ocr' / f'{name}.json').read_text()))


def frame(name: str) -> Any:
    image = cv2.imread(str(FIXTURES / f'{name}.png'))
    assert image is not None, f'missing fixture: {name}.png'
    return image


def replaced(boxes: tuple[ocr.TextBox, ...], text: str,
             *, into: str, confidence: float | None = None) -> tuple[ocr.TextBox, ...]:
    """The recorded boxes with one box's text (and confidence) swapped.

    Geometry is kept exactly where it was measured, so a case only ever
    changes what the page says - never where the reader believes it sits.
    """
    return tuple(ocr.TextBox(into, confidence if confidence is not None else b.confidence, b.rect)
                 if b.text == text else b for b in boxes)


def reading() -> Any:
    import cards
    result = cards.parse_frame(frame('menu_cards'), recorded('menu_cards'), now=1.)
    assert result is not None
    return result


def test_the_recorded_cards_page_is_identified_and_its_slots_are_exact() -> None:
    import cards
    import screen_discovery
    boxes = recorded('menu_cards')
    assert screen_discovery.discover(frame('menu_cards'), boxes, 'cards') == (
        screen_discovery.ScreenDiscovery('cards.inventory', True, 'recorded_layout'))
    result = reading()
    assert result.screen_id == 'cards.inventory'
    assert (result.slots.equipped, result.slots.capacity) == (0, 1)
    assert result.slots.status == 'observed'
    # Not "no further slot exists": one is drawn, and it is bought, not owned.
    assert result.slots.next_slot == 'locked'
    # Zero equipped of one slot is an exact equipped set, not an empty guess.
    assert result.equipped == ()
    assert cards.equipment_known(result)


def test_a_locked_collection_is_not_a_collection_of_zeroes() -> None:
    import cards
    result = reading()
    observed = {o.concept_id: o for o in result.cards}
    assert set(observed) == set(cards.CARD_IDS) | set(cards.MASTERY_IDS)
    assert len(cards.CARD_IDS) == 31 and len(cards.MASTERY_IDS) == 31
    for observation in result.cards:
        assert observation.status == 'unknown'
        assert observation.copies is None and observation.level is None
    assert set(cards.missing(result)) == set(observed)


def test_the_five_missing_states_never_collapse_into_each_other() -> None:
    import cards
    assert len(set(cards.MISSING_STATES)) == len(cards.MISSING_STATES) == 5
    assert set(cards.MISSING_STATES) == {'unknown', 'locked', 'unavailable',
                                         'maxed', 'unreadable'}
    assert 'observed' not in cards.MISSING_STATES


def test_effective_stat_inputs_report_unknown_rather_than_zero() -> None:
    """The acceptance gate, stated the way a calculator consumer meets it."""
    import cards
    inputs = cards.effective_stat_inputs(reading())
    assert inputs['equipment_known'] is True
    assert inputs['equipped'] == ()
    assert inputs['slots'] == {'equipped': 0, 'capacity': 1, 'next_slot': 'locked'}
    # Nothing about the collection is known, and nothing about it is zero.
    assert inputs['card_bonus_known'] is False
    assert inputs['card_bonus'] is None
    assert set(inputs['unknown_identities']) == set(cards.CARD_IDS) | set(cards.MASTERY_IDS)
    # Never read at all is its own answer, and not a clean empty account.
    absent = cards.effective_stat_inputs(None)
    assert absent['equipment_known'] is False
    assert absent['card_bonus_known'] is False
    assert absent['slots'] == {'equipped': None, 'capacity': None, 'next_slot': 'unknown'}
    assert absent['reason'] == 'not_read'


def test_equipped_cards_that_cannot_be_named_are_not_treated_as_known() -> None:
    """Two cards in three slots, and no way to say which two.

    The slot band still reads, so the counts are recorded. The equipped set
    is not, and a consumer must be able to tell that apart from "empty".
    """
    import cards
    result = cards.parse_frame(frame('menu_cards'),
                               replaced(recorded('menu_cards'), '0/1', into='2/3'), now=1.)
    assert result is not None
    assert (result.slots.equipped, result.slots.capacity) == (2, 3)
    assert result.equipped is None
    assert not cards.equipment_known(result)
    inputs = cards.effective_stat_inputs(result)
    assert inputs['equipment_known'] is False
    assert inputs['reason'] == 'equipped_identities_unreadable'


@pytest.mark.parametrize('boxes_name,mutation', [
    ('menu_cards', {'text': 'ACTIVE', 'into': 'INVENTORY'}),
    ('menu_cards', {'text': '0/1', 'into': '1/0'}),
    ('menu_cards', {'text': 'CARDS', 'into': 'CARDS', 'confidence': .4}),
    ('menu_cards', {'text': '0/1', 'into': 'O/l'}),
])
def test_ambiguous_evidence_yields_no_claim_and_no_action(
    boxes_name: str, mutation: dict[str, Any],
) -> None:
    import cards
    import screen_discovery
    boxes = replaced(recorded(boxes_name), mutation.pop('text'), **mutation)
    image = frame(boxes_name)
    assert cards.parse_frame(image, boxes, now=1.) is None
    discovery = screen_discovery.discover(image, boxes, 'cards')
    assert discovery.readable is False
    assert discovery.screen_id is None
    assert cards.actions(None) == ()


def test_an_upgrade_page_is_never_read_as_the_cards_page() -> None:
    import cards
    import screen_discovery
    boxes, image = recorded('menu_workshop_attack'), frame('menu_workshop_attack')
    assert screen_discovery.discover(image, boxes, 'cards').readable is False
    assert cards.parse_frame(image, boxes, now=1.) is None
    # And the Cards page carries no upgrade heading, so it stays refused there.
    assert screen_discovery.discover(
        frame('menu_cards'), recorded('menu_cards'), 'workshop').readable is False


def test_reading_the_page_never_plans_a_tap() -> None:
    """C02 owns buying and equipping. This module observes and stops."""
    import cards
    assert cards.actions(reading()) == ()
    assert not any(hasattr(cards, name) for name in ('tap', 'click', 'press'))


def test_card_state_survives_a_fresh_repository_over_the_same_file(tmp_path: Path) -> None:
    import cards
    from account_state import AccountRepository, AccountRevision, AccountState
    path = tmp_path / 'state.db'
    facts = cards.facts(reading())
    assert {f.concept_id for f in facts} == {cards.SLOT_EQUIPPED_KEY, cards.SLOT_CAPACITY_KEY}
    assert all(f.status == 'observed' for f in facts)
    AccountRepository(path).save_account(AccountRevision(cards=facts), facts)
    restored = AccountRepository(path).latest()
    assert restored is not None
    assert restored.cards == facts
    snapshot = AccountState(AccountRepository(path)).snapshot()['revision']
    assert {f['concept_id']: f['value'] for f in snapshot['cards']} == {
        cards.SLOT_EQUIPPED_KEY: 0, cards.SLOT_CAPACITY_KEY: 1}
    # A revision that never saw the Cards page says so, rather than saying zero.
    assert AccountRevision().cards is None


def test_slot_keys_can_never_be_mistaken_for_a_catalog_identity() -> None:
    import cards
    from concepts import REGISTRY
    ids = {c.concept_id for c in REGISTRY.concepts}
    for key in (cards.SLOT_EQUIPPED_KEY, cards.SLOT_CAPACITY_KEY):
        assert key not in ids
        assert key.count('.') == 2  # every catalog identity has exactly one dot


def test_passive_and_active_come_from_the_catalog_or_stay_unknown() -> None:
    """The Outcome asks for the split; v1 of the catalog does not carry it."""
    import cards
    from concepts import REGISTRY
    kinds = {c.kind for c in REGISTRY.concepts if c.concept_id in cards.CARD_IDS}
    assert kinds == {'card'}
    assert {cards.effect_kind(i) for i in cards.CARD_IDS} == {'unknown'}
    assert {o.effect_kind for o in reading().cards} == {'unknown'}
    import screen_discovery
    owners = screen_discovery.capabilities()['unsupported_owners']
    assert owners['card_passive_or_active_classification'] == 'B02'


def test_the_support_matrix_names_the_cards_evidence_and_its_gaps() -> None:
    import screen_discovery
    capabilities = screen_discovery.capabilities()
    assert capabilities['readers']['cards.inventory'] == 'menu_cards'
    assert (FIXTURES / 'menu_cards.png').exists()
    assert (FIXTURES / 'ocr' / 'menu_cards.json').exists()
    unsupported = capabilities['unsupported']
    owners = capabilities['unsupported_owners']
    for scope in ('card_identity_rows', 'card_mastery_ownership', 'card_presets',
                  'in_run_card_locks'):
        assert scope in unsupported
        assert owners[scope]


def test_observed_cards_persist_their_level_and_copies_and_nothing_else() -> None:
    import dataclasses
    import cards
    page = reading()
    observed = dataclasses.replace(page, cards=(
        cards.CardObservation('cards.damage', 'cards', 'unknown', 'observed', 12, 5, 'read'),
        cards.CardObservation('cards.health', 'cards', 'unknown', 'observed', None, 2, 'copies unread'),
        cards.CardObservation('cards.range', 'cards', 'unknown', 'locked', None, None, 'padlock'),
    ))

    values = {f.concept_id: f.value for f in cards.facts(observed)}

    assert values == {cards.SLOT_EQUIPPED_KEY: 0, cards.SLOT_CAPACITY_KEY: 1,
                      'cards.damage.level': 5, 'cards.damage.copies': 12,
                      'cards.health.level': 2}
    level = next(f for f in cards.facts(observed) if f.concept_id == 'cards.damage.level')
    assert (level.status, level.evidence.raw_name, level.evidence.raw_value) == (
        'observed', 'cards.damage', '5')


def test_card_fact_keys_can_never_be_mistaken_for_a_catalog_identity() -> None:
    import cards
    from concepts import REGISTRY
    ids = {c.concept_id for c in REGISTRY.concepts}
    for key in (cards.card_level_key('cards.damage'), cards.card_copies_key('cards.damage')):
        assert key not in ids
        assert key.count('.') == 2
