"""Real captures establish recognition; mutations only exercise refusal paths."""
from __future__ import annotations

from dataclasses import replace

import pytest

import config
import ocr
from card_models import CardItem, CardSnapshot
from evidence_scope import FactScope
from tests.card_fixtures import frame, recorded, replaced

SCOPE = FactScope('account-a', 'lease-a', 'generation-a', 1)


def snapshot(name: str = 'menu_cards_stocked', *, boxes: tuple[ocr.TextBox, ...] | None = None,
             now: float = 10.) -> CardSnapshot:
    import card_screen
    result = card_screen.read_snapshot(frame(name), recorded(name) if boxes is None else boxes,
                                      scope=SCOPE, visit_id='visit-a', now=now)
    assert result is not None
    return result


def test_real_stocked_tiles_have_ownership_and_max_levels_but_partial_coverage() -> None:
    result = snapshot()
    items = {item.card_id: item for item in result.items}
    assert len(items) == 12
    assert items['cards.damage'].ownership == 'owned'
    assert (items['cards.damage'].maxed, items['cards.damage'].level) == (True, 7)
    assert items['cards.damage'].copies is None
    assert items['cards.damage'].copies_needed is None
    assert items['cards.enemy-balance'].ownership == 'owned'
    assert items['cards.enemy-balance'].equipped is True
    assert items['cards.enemy-balance'].battle_locked is True
    assert items['cards.range'].equipped is None  # absence of a check is not calibrated
    assert 'cards.plasma-canon' not in items  # clipped fourth row
    assert result.capacity == 14
    assert result.equipped is None
    assert not result.equipment_complete and not result.collection_complete
    assert result.scope == SCOPE and result.visit_id == 'visit-a'
    assert all(item.observed_at == 10. and item.evidence_ref for item in result.items)


def test_real_empty_screen_proves_empty_equipment_but_no_card_identity() -> None:
    result = snapshot('menu_cards')
    assert result.items == ()
    assert result.capacity == 1 and result.equipped == ()
    assert result.equipment_complete and not result.collection_complete


@pytest.mark.parametrize('name,has_damage,equipment_empty', [
    ('menu_cards_compact_empty', False, True),
    ('menu_cards_compact_damage', True, True),
    ('menu_cards_compact_equipped', True, False),
    ('menu_cards_compact_restored', True, True),
    ('menu_cards_compact_duplicate', True, True),
])
def test_real_compact_cards_preserve_passive_facts_and_unknowns(
        name: str, has_damage: bool, equipment_empty: bool) -> None:
    import card_screen
    result = snapshot(name)
    assert result.layout_id == 'cards.compact.1080x2400.en'
    assert result.capacity == 1
    assert result.equipped == (() if equipment_empty else ('cards.damage',))
    assert result.equipment_complete
    assert not result.collection_complete
    assert [item.card_id for item in result.items] == (['cards.damage'] if has_damage else [])
    for item in result.items:
        assert item.ownership == 'owned'
        assert item.layout_id == result.layout_id
        # One-star/copy counters and this check variant are not calibrated.
        assert (item.level, item.copies, item.copies_needed, item.maxed,
                item.equipped, item.battle_locked) == (None,) * 6
        assert item.field_evidence['ownership'].layout_id == result.layout_id
    caps = card_screen.capabilities(result.layout_id)
    assert caps.inventory
    assert caps.buy_one and caps.assign
    assert not caps.buy_ten and not caps.buy_slot


@pytest.mark.parametrize('mutation', ['mixed_title', 'mixed_count', 'duplicate_count',
                                    'duplicate_active', 'duplicate_title',
                                    'uncertain_active', 'uncertain_count'])
def test_compact_cards_require_one_coherent_measured_anchor_profile(mutation: str) -> None:
    import card_screen
    boxes = recorded('menu_cards_compact_empty')
    if mutation == 'mixed_title':
        boxes = tuple(replace(b, rect=config.Rect(b.rect.x, 246, b.rect.w, b.rect.h))
                      if b.text == 'CARDS' else b for b in boxes)
    elif mutation == 'mixed_count':
        boxes = tuple(replace(b, rect=config.Rect(b.rect.x, 555, b.rect.w, b.rect.h))
                      if b.text == '0/1' else b for b in boxes)
    elif mutation.startswith('duplicate_'):
        text = {'duplicate_active': 'ACTIVE', 'duplicate_count': '0/1',
                'duplicate_title': 'CARDS'}[mutation]
        duplicate = next(b for b in boxes if b.text == text)
        if mutation == 'duplicate_title':
            duplicate = replace(duplicate, rect=config.Rect(400, duplicate.rect.y,
                                                           duplicate.rect.w, duplicate.rect.h))
        boxes += (duplicate,)
    else:
        text = 'ACTIVE' if mutation == 'uncertain_active' else '0/1'
        boxes = replaced(boxes, text, into=text, confidence=.4)
    assert card_screen.read_snapshot(frame('menu_cards_compact_empty'), boxes,
                                    scope=SCOPE, visit_id='visit-a', now=10.) is None


def test_compact_inventory_requires_its_own_measured_heading() -> None:
    boxes = recorded('menu_cards_compact_damage')
    moved = tuple(replace(b, rect=config.Rect(b.rect.x, 965, b.rect.w, b.rect.h))
                  if b.text == 'INVENTORY' else b for b in boxes)
    result = snapshot('menu_cards_compact_damage', boxes=moved)
    assert result.items == () and result.equipped == ()


def test_low_confidence_or_duplicate_names_do_not_become_owned_cards() -> None:
    boxes = recorded('menu_cards_stocked')
    damage = next(b for b in boxes if b.text == 'Damage' and b.rect.y > 1000)
    low = tuple(replace(b, confidence=.4) if b is damage else b for b in boxes)
    assert 'cards.damage' not in {item.card_id for item in snapshot(boxes=low).items}
    assert 'cards.damage' not in {item.card_id for item in snapshot(boxes=boxes + (damage,)).items}
    # Two different tiles resolving to the same identity invalidate that identity.
    duplicate = replaced(boxes, 'Attack Speed', into='Damage')
    assert 'cards.damage' not in {item.card_id for item in snapshot(boxes=duplicate).items}


def test_unreadable_max_never_converts_visible_stars_to_a_numeric_level() -> None:
    boxes = replaced(recorded('menu_cards_stocked'), 'Max', into='Max', confidence=.4)
    assert all(item.level is None and item.maxed is None for item in snapshot(boxes=boxes).items)
    # No non-max counter/level sequence has been recorded yet.
    boxes = replaced(recorded('menu_cards_stocked'), 'Max', into='0/32')
    assert all(item.copies is None and item.level is None for item in snapshot(boxes=boxes).items)


def test_counters_from_another_tile_or_an_ambiguous_label_are_not_used() -> None:
    boxes = recorded('menu_cards_stocked')
    label = next(b for b in boxes if b.text == 'Max')
    items = {item.card_id: item for item in snapshot(boxes=boxes + (label,)).items}
    assert items['cards.damage'].maxed is None
    assert items['cards.attack-speed'].maxed is True


def test_reading_rejects_unknown_dimensions_and_incoherent_page_anchors() -> None:
    import card_screen
    image = frame('menu_cards_stocked')
    boxes = recorded('menu_cards_stocked')
    assert card_screen.read_snapshot(image[1:], boxes, scope=SCOPE, visit_id='visit-a', now=10.) is None
    assert card_screen.read_snapshot(image, replaced(boxes, 'CARDS', into='OTHER'),
                                     scope=SCOPE, visit_id='visit-a', now=10.) is None
    assert card_screen.read_snapshot(image, boxes, scope=SCOPE, visit_id='visit-a', now=float('nan')) is None


def test_missing_inventory_anchor_preserves_only_slots() -> None:
    result = snapshot(boxes=replaced(recorded('menu_cards_stocked'), 'INVENTORY', into='OTHER'))
    assert result.items == ()
    assert not result.collection_complete and not result.equipment_complete


def test_only_calibrated_compact_single_purchase_and_assignment_are_supported() -> None:
    import card_screen
    supported = (card_screen.EMPTY_LAYOUT, card_screen.STOCKED_LAYOUT, card_screen.COMPACT_LAYOUT)
    for layout in (*supported, None, 'invented'):
        caps = card_screen.capabilities(layout)
        assert caps.inventory is (layout in supported)
        assert caps.buy_one is (layout == card_screen.COMPACT_LAYOUT)
        assert caps.assign is (layout == card_screen.COMPACT_LAYOUT)
        assert not caps.buy_ten and not caps.buy_slot
        assert all(caps.reasons[key] for key in ('buy_one', 'buy_ten', 'buy_slot', 'assign')
                   if not getattr(caps, key))


def test_reward_reader_refuses_inventory_and_synthetic_repeated_or_duplicate_rewards() -> None:
    import card_screen
    image = frame('menu_cards_stocked')
    boxes = recorded('menu_cards_stocked')
    assert card_screen.read_rewards(image, boxes) is None
    fake = (ocr.TextBox('Damage', .99, config.Rect(100, 1000, 100, 30)),)
    assert card_screen.read_rewards(image, fake) is None
    assert card_screen.read_rewards(image, fake) is None  # repeated frame
    assert card_screen.read_rewards(image, fake + (replace(fake[0], rect=config.Rect(400, 1000, 100, 30)),)) is None


def _item(card_id: str, *, level: int, copies: int, now: float) -> CardItem:
    return CardItem(card_id=card_id, ownership='owned', level=level, copies=copies,
                    observed_at=now, evidence_ref=f'capture-{now}')


def _page(*items: CardItem, now: float = 10., scope: FactScope = SCOPE, visit: str = 'visit-a') -> CardSnapshot:
    return CardSnapshot(scope=scope, revision=0, observed_at=now, visit_id=visit, items=items,
                        capacity=14, collection_complete=False, equipment_complete=False)


def test_partial_overlap_preserves_missing_cards_and_replaces_copy_counter_after_level_up() -> None:
    """Synthetic model observations test assembly, not unrecorded visual recognition."""
    import card_screen
    previous = _page(_item('cards.damage', level=1, copies=2, now=10.),
                     _item('cards.health', level=1, copies=1, now=10.))
    observed = _page(_item('cards.damage', level=2, copies=0, now=11.), now=11.)
    result = card_screen.merge_snapshots(previous, observed)
    assert {item.card_id: (item.level, item.copies) for item in result.items} == {
        'cards.damage': (2, 0), 'cards.health': (1, 1)}
    assert result.revision == 1
    assert not result.collection_complete and not result.equipment_complete
    assert result.equipped is None
    assert card_screen.merge_snapshots(result, observed) == result


@pytest.mark.parametrize('field,value', [('lease_id', 'other'), ('generation', 'other'), ('epoch', 2), ('run_id', 1)])
def test_same_account_scope_change_retains_history_without_current_authority(field: str, value: object) -> None:
    import card_screen
    previous = snapshot()
    new_scope = replace(SCOPE, **{field: value})
    result = card_screen.merge_snapshots(previous, _page(now=11., scope=new_scope, visit='visit-b'))
    damage = next(item for item in result.items if item.card_id == 'cards.damage')
    assert damage.level == 7
    assert damage.field_evidence['level'].scope == SCOPE
    assert damage.field_evidence['level'].visit_id == 'visit-a'
    assert not card_screen.field_is_current(damage, 'level', scope=new_scope, visit_id='visit-b')
    assert result.scope == new_scope and result.visit_id == 'visit-b'
    assert not result.collection_complete and not result.equipment_complete


def test_account_change_drops_all_history() -> None:
    import card_screen
    observed = _page(now=11., scope=replace(SCOPE, account_id='other'))
    assert card_screen.merge_snapshots(snapshot(), observed) == observed


def test_new_visit_retains_fields_but_refuses_older_same_visit_observation() -> None:
    import card_screen
    previous = snapshot()
    result = card_screen.merge_snapshots(previous, _page(now=11., visit='visit-b'))
    assert result.items == previous.items
    assert result.visit_id == 'visit-b'
    assert not card_screen.field_is_current(result.items[0], 'level', scope=SCOPE, visit_id='visit-b')
    assert card_screen.merge_snapshots(previous, _page(now=9.)) == previous


def test_snapshot_and_each_item_keep_frame_layout_and_observation_confidence() -> None:
    import hashlib
    import card_screen
    result = snapshot()
    digest = hashlib.sha256(frame('menu_cards_stocked').tobytes()).hexdigest()
    assert result.frame_digest == digest and result.layout_id == card_screen.STOCKED_LAYOUT
    assert .9 <= result.confidence <= 1.
    for item in result.items:
        assert item.frame_digest == digest and item.layout_id == card_screen.STOCKED_LAYOUT
        assert .9 <= item.confidence <= 1.


@pytest.mark.parametrize('confidence', [float('nan'), float('inf'), 1.1, -.1])
def test_invalid_confidence_cannot_create_owned_cards(confidence: float) -> None:
    boxes = recorded('menu_cards_stocked')
    bad = tuple(replace(b, confidence=confidence) if b.text == 'Damage' and b.rect.y > 1000 else b
                for b in boxes)
    assert 'cards.damage' not in {item.card_id for item in snapshot(boxes=bad).items}


def test_ocr_on_a_blank_grid_cannot_create_inventory_observations() -> None:
    import card_screen
    image = frame('menu_cards_stocked')
    image[1030:2230] = 0
    result = card_screen.read_snapshot(image, recorded('menu_cards_stocked'),
                                      scope=SCOPE, visit_id='visit-a', now=10.)
    assert result is not None and result.items == ()


def test_erased_markers_become_unknown_instead_of_negative_equipment_or_ownership() -> None:
    import card_screen
    image = frame('menu_cards_stocked')
    image[1320:1390, 220:296] = 0
    image[1810:1880, 295:355] = 0
    result = card_screen.read_snapshot(image, recorded('menu_cards_stocked'),
                                      scope=SCOPE, visit_id='visit-a', now=10.)
    assert result is not None
    items = {item.card_id: item for item in result.items}
    assert items['cards.damage'].equipped is None
    assert items['cards.enemy-balance'].battle_locked is None
    assert items['cards.enemy-balance'].ownership == 'owned'


def test_partial_item_preserves_old_known_fields_without_promoting_their_freshness() -> None:
    import card_screen
    previous = _page(_item('cards.damage', level=2, copies=1, now=10.).model_copy(update={'equipped': True}))
    partial = CardItem(card_id='cards.damage', ownership='owned', observed_at=11., evidence_ref='new-frame')
    result = card_screen.merge_snapshots(previous, _page(partial, now=11.))
    damage = result.items[0]
    assert (damage.level, damage.copies, damage.equipped) == (2, 1, True)
    assert damage.field_evidence['level'].observed_at == 10.
    assert damage.field_evidence['equipped'].observed_at == 10.
    assert damage.field_evidence['equipped'].evidence_ref == 'capture-10.0'
    assert damage.field_evidence['ownership'].observed_at == 11.
    assert damage.field_evidence['ownership'].evidence_ref == 'new-frame'
    assert result.equipped is None and not result.equipment_complete
    reset = _page(_item('cards.damage', level=3, copies=0, now=12.), now=12.)
    damage = card_screen.merge_snapshots(result, reset).items[0]
    assert (damage.level, damage.copies, damage.equipped) == (3, 0, True)
    assert damage.field_evidence['copies'].observed_at == 12.
    assert damage.field_evidence['equipped'].observed_at == 10.


def test_zero_equipped_band_conflicting_with_visible_checks_is_refused() -> None:
    import card_screen
    boxes = replaced(recorded('menu_cards_stocked'), '14/14', into='0/14')
    assert card_screen.read_snapshot(frame('menu_cards_stocked'), boxes,
                                     scope=SCOPE, visit_id='visit-a', now=10.) is None


@pytest.mark.parametrize('text', ['CARDS', 'ACTIVE', '14/14'])
@pytest.mark.parametrize('confidence', [float('inf'), 1.1])
def test_invalid_heading_confidence_refuses_the_page(text: str, confidence: float) -> None:
    import card_screen
    boxes = replaced(recorded('menu_cards_stocked'), text, into=text, confidence=confidence)
    assert card_screen.read_snapshot(frame('menu_cards_stocked'), boxes,
                                     scope=SCOPE, visit_id='visit-a', now=10.) is None


def test_reader_supplies_field_scope_and_visit_but_legacy_values_are_historical_only() -> None:
    import card_screen
    damage = snapshot().items[0]
    assert card_screen.field_is_current(damage, 'level', scope=SCOPE, visit_id='visit-a')
    assert not card_screen.field_is_current(damage, 'level', scope=SCOPE, visit_id='visit-b')
    assert not card_screen.field_is_current(damage, 'level', scope=replace(SCOPE, epoch=2), visit_id='visit-a')
    assert not card_screen.field_is_current(damage, 'copies', scope=SCOPE, visit_id='visit-a')
    legacy = _item('cards.damage', level=2, copies=1, now=10.)
    retained = card_screen.merge_snapshots(_page(legacy), _page(now=11., visit='visit-b')).items[0]
    assert retained.level == 2
    assert not card_screen.field_is_current(retained, 'level', scope=SCOPE, visit_id='visit-b')


def test_complete_identity_coverage_keeps_old_fields_on_overlapping_cards() -> None:
    import card_screen
    previous = _page(_item('cards.damage', level=2, copies=1, now=10.),
                     _item('cards.health', level=1, copies=1, now=10.))
    partial = CardItem(card_id='cards.damage', ownership='owned', observed_at=11., evidence_ref='new-frame')
    observed = _page(partial, now=11.).model_copy(update={'collection_complete': True})
    result = card_screen.merge_snapshots(previous, observed)
    assert len(result.items) == 1
    assert result.items[0].level == 2 and result.items[0].copies == 1
    assert result.items[0].field_evidence['level'].observed_at == 10.
    assert result.collection_complete


def test_equal_time_conflicting_field_keeps_previous_value_and_provenance() -> None:
    import card_screen
    previous = _page(_item('cards.damage', level=2, copies=1, now=10.))
    conflicting = _item('cards.damage', level=3, copies=0, now=10.).model_copy(update={'evidence_ref': 'conflict'})
    result = card_screen.merge_snapshots(previous, _page(conflicting))
    item = result.items[0]
    assert (item.level, item.copies) == (2, 1)
    assert item.field_evidence['level'].evidence_ref == 'capture-10.0'
    assert card_screen.merge_snapshots(result, _page(conflicting)) == result
    newer = _item('cards.damage', level=3, copies=0, now=11.).model_copy(update={'confidence': .99})
    result = card_screen.merge_snapshots(result, _page(newer, now=11.))
    assert (result.items[0].level, result.items[0].copies) == (3, 0)


def test_partial_new_visit_keeps_equipment_history_with_original_source() -> None:
    import card_screen
    from card_models import CardFieldEvidence
    evidence = CardFieldEvidence(observed_at=10., evidence_ref='equipment-frame', confidence=.99,
                                 scope=SCOPE, visit_id='visit-a')
    previous = _page().model_copy(update={'equipped': ('cards.damage',), 'equipment_complete': True,
                                          'equipment_evidence': evidence})
    result = card_screen.merge_snapshots(previous, _page(now=11., visit='visit-b'))
    assert result.equipped == ('cards.damage',) and not result.equipment_complete
    assert result.equipment_evidence == evidence
    assert result.equipment_evidence.visit_id == 'visit-a'
    assert result.observed_at == 11.
    conflict = card_screen.merge_snapshots(result, snapshot('menu_cards'))
    assert conflict.equipped == ('cards.damage',) and not conflict.equipment_complete
    assert conflict.equipment_evidence == evidence
    empty = snapshot('menu_cards', now=12.)
    replaced = card_screen.merge_snapshots(result, empty)
    assert replaced.equipped == () and replaced.equipment_complete
    assert replaced.equipment_evidence.scope == SCOPE
    assert replaced.equipment_evidence.evidence_ref != 'equipment-frame'
    other = _page(now=13., scope=replace(SCOPE, account_id='other'))
    assert card_screen.merge_snapshots(previous, other).equipped is None


def test_legacy_equipment_has_unknown_scope_and_visit_when_retained() -> None:
    import card_screen
    previous = _page().model_copy(update={'equipped': ('cards.damage',), 'equipment_complete': True})
    result = card_screen.merge_snapshots(previous, _page(now=11., visit='visit-b'))
    assert result.equipped == ('cards.damage',) and not result.equipment_complete
    assert result.equipment_evidence.observed_at == 10.
    assert result.equipment_evidence.scope is None and result.equipment_evidence.visit_id is None
