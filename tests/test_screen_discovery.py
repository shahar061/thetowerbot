from __future__ import annotations

import json
from pathlib import Path

import cv2
import pytest

import battle_tab
import config
import ocr
import perception
import tiles

FIXTURES = Path(__file__).parent / 'fixtures'


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / 'ocr' / f'{name}.json').read_text()))


@pytest.mark.parametrize('name,context,expected', [
    ('menu_workshop_attack', 'workshop', 'workshop.attack'),
    ('menu_workshop_defense', 'workshop', 'workshop.defense'),
    ('menu_workshop_utility', 'workshop', 'workshop.utility'),
    ('in_run_lit', 'battle', 'battle.attack'),
])
def test_recorded_supported_screens(name: str, context: str, expected: str) -> None:
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    result = screen_discovery.discover(frame, recorded(name), context)
    assert result.screen_id == expected
    assert result.readable


def test_native_1920_battle_defense_exposes_only_frame_local_buy_targets() -> None:
    import screen_discovery

    name = 'in_run_defense_1920'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    boxes = recorded(name)
    assert screen_discovery.discover(frame, boxes, 'battle').screen_id == 'battle.defense'
    observation = perception.parse_frame(frame, boxes, 'battle')
    assert observation.category == 'DEFENSE'
    targets = [row.tap for row in observation.rows if row.tap is not None]
    assert targets and all(0 <= x < 1080 and 0 <= y < 1920 for x, y in targets)


def test_bluestacks_native_missions_page_keeps_its_top_anchored_cards() -> None:
    import missions_screen
    import screen_discovery

    name = 'menu_missions_bluestacks_1920'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    boxes = recorded(name)
    assert screen_discovery.discover(frame, boxes, 'missions').screen_id == 'missions.daily'
    reading = missions_screen.parse_frame(frame, boxes, now=123.)
    assert reading is not None and reading.frame_height == 1920
    assert reading.shown == 2 and reading.offered == 8
    assert len(reading.missions) == 2


@pytest.mark.parametrize('category', ('attack', 'defense', 'utility'))
def test_native_1920_workshop_keeps_buy_targets_on_visible_rows(category: str) -> None:
    import screen_discovery

    name = f'menu_workshop_{category}_1920'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    boxes = recorded(name)
    assert screen_discovery.discover(frame, boxes, 'workshop').screen_id == f'workshop.{category}'
    observation = perception.parse_frame(frame, boxes, 'workshop')
    assert observation.category == category.upper()
    targets = [row.tap for row in observation.rows if row.tap is not None]
    assert targets and all(0 <= x < 1080 and 0 <= y < 1920 for x, y in targets)


@pytest.mark.parametrize('name,screen_id', [
    ('menu_workshop_info_panel', 'workshop.info_overlay'),
    ('menu_workshop_explainer_modal', 'workshop.ultimate_explainer'),
])
def test_recorded_overlays_keep_identity_and_never_expose_purchase_rows(
    name: str, screen_id: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    boxes = recorded(name)
    assert screen_discovery.discover(frame, boxes, 'workshop') == (
        screen_discovery.ScreenDiscovery(screen_id, False, 'overlay'))
    monkeypatch.setattr(ocr, 'read', lambda _: boxes)
    result = perception.observe_frame(frame, 'workshop')
    assert result.rows == ()


@pytest.mark.parametrize('frame_name,boxes_name,context', [
    ('menu_workshop_info_panel', 'menu_workshop_info_panel', 'workshop'),
    ('menu_workshop_explainer_modal', 'menu_workshop_explainer_modal', 'workshop'),
    ('game_over_fade', 'in_run_lit', 'battle'),
    ('game_over_stats', 'in_run_lit', 'battle'),
])
def test_central_bordered_overlay_fails_closed_when_expected_heading_remains(
    frame_name: str, boxes_name: str, context: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / f'{frame_name}.png'))
    boxes = tuple(b for b in recorded(boxes_name)
                  if tiles.normalise(b.text) not in (
                      'currentlevel', 'maxlevel', 'ultimateweapons', 'ok'))
    if frame_name == 'menu_workshop_explainer_modal':
        boxes += tuple(b for b in recorded('menu_workshop_attack')
                       if tiles.normalise(b.text) in ('workshop', 'attackupgrades'))

    result = screen_discovery.discover(frame, boxes, context)

    assert result == screen_discovery.ScreenDiscovery(None, False, 'overlay_geometry')
    monkeypatch.setattr(ocr, 'read', lambda _: boxes)
    assert perception.observe_frame(frame, context).rows == ()


def test_runtime_rejects_geometry_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_workshop_attack.png'))[:2300]
    monkeypatch.setattr(ocr, 'read', lambda _: recorded('menu_workshop_attack'))
    assert perception.observe_frame(frame, 'workshop').rows == ()


def test_locale_and_context_are_explicitly_validated() -> None:
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / 'menu_workshop_attack.png'))
    boxes = recorded('menu_workshop_attack')
    assert not screen_discovery.discover(frame, boxes, 'workshop', locale='de').readable
    assert not screen_discovery.discover(frame, boxes, 'battle').readable


def test_unseen_is_distinct_from_visible_unreadable() -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_workshop_attack.png'))
    observation = perception.parse_frame(frame, recorded('menu_workshop_attack'), 'workshop')
    assert observation.status_for('health') == 'unseen'
    assert observation.status_for('damage') == 'available'


@pytest.mark.parametrize('marker,status', [('LOCKED', 'locked'), ('MAXED', 'maxed'),
                                           ('UNAVAILABLE', 'unavailable'), ('???', 'unreadable')])
def test_row_availability_markers(marker: str, status: str) -> None:
    frame = cv2.imread(str(FIXTURES / 'in_run_lit.png'))
    boxes = tuple(ocr.TextBox(marker if b.text == '$4' else b.text, b.confidence, b.rect)
                  for b in recorded('in_run_lit'))
    row = next(r for r in perception.parse_frame(frame, boxes, 'battle').rows
               if r.upgrade_id == 'critical_chance')
    assert row.status == status
    assert row.tap is None


def test_row_identity_follows_text_when_row_order_changes() -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_workshop_attack.png'))
    original = recorded('menu_workshop_attack')
    # Simulate a changed row ordering on the recorded geometry. This proves
    # identity binding, not support for an unrecorded game unlock stage.
    boxes = tuple(ocr.TextBox('Critical' if b.text == 'Damage' else
                             'Damage' if b.text == 'Critical' and b.rect.x < 540 else
                             '' if b.text == 'Chance' else b.text, b.confidence, b.rect)
                  for b in original)
    result = perception.parse_frame(frame, boxes, 'workshop')
    damage = next(r for r in result.rows if r.upgrade_id == 'damage')
    assert damage.price == 50
    assert damage.rect.y > 680


def test_duplicate_identity_and_unknown_label_cannot_expose_taps() -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_workshop_attack.png'))
    boxes = tuple(ocr.TextBox('Damage' if b.text == 'Critical' else
                             '' if b.text in ('Chance', 'Factor') else b.text, b.confidence, b.rect)
                  for b in recorded('menu_workshop_attack'))
    result = perception.parse_frame(frame, boxes, 'workshop')
    assert all(r.tap is None and r.status == 'unreadable' for r in result.rows if r.upgrade_id == 'damage')
    boxes = tuple(ocr.TextBox('Unknown Power' if b.text == 'Damage' else b.text, b.confidence, b.rect)
                  for b in recorded('menu_workshop_attack'))
    unknown = next(r for r in perception.parse_frame(frame, boxes, 'workshop').rows
                   if r.upgrade_id.startswith('discovered:'))
    assert unknown.tap is None


def test_capability_matrix_names_recorded_evidence_and_missing_scope() -> None:
    import screen_discovery
    capabilities = screen_discovery.capabilities()
    assert capabilities['resolution'] == [1080, 2400]
    assert capabilities['locale'] == 'en'
    assert capabilities['complete'] is False
    assert 'battle_history_export' in capabilities['unsupported']
    assert 'unknown_overlays' in capabilities['unsupported']
    assert capabilities['guarded_overlay_geometry'] == {
        'minimum_size': [700, 600],
        'must_cover_screen_center': True,
        'recorded_evidence': [
            'menu_workshop_info_panel', 'menu_workshop_explainer_modal',
            'game_over_fade', 'game_over_stats',
        ],
    }
    for name in capabilities['readers'].values():
        assert (FIXTURES / f'{name}.png').exists()
        assert (FIXTURES / 'ocr' / f'{name}.json').exists()


@pytest.mark.parametrize('name', [
    'menu_missions_claimable', 'menu_missions_scrolled', 'menu_missions_weekly',
])
def test_a_mission_whose_text_ends_in_upgrades_still_reads_as_missions(name: str) -> None:
    """A mission is described in prose, and the prose is not a heading.

    "Buy 20 battle upgrades" is an ordinary daily. Refusing the whole page
    because a card mentions upgrades makes the reader fail on any day that
    mission is drawn, which strands a visit on the page with every action
    held. These three captures all carry it.
    """
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    boxes = recorded(name)
    assert any(tiles.normalise(b.text).endswith('upgrades') for b in boxes), (
        f'{name} no longer carries the mission this guards against')
    result = screen_discovery.discover(frame, boxes, 'missions')
    assert (result.screen_id, result.readable) == ('missions.daily', True)


@pytest.mark.parametrize('name', ['menu_workshop_attack', 'menu_workshop_attack_early'])
def test_a_real_upgrade_screen_is_still_refused_in_the_missions_context(name: str) -> None:
    """The guard's actual job, kept: an upgrade page is never read as missions."""
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    result = screen_discovery.discover(frame, recorded(name), 'missions')
    assert result.screen_id is None and not result.readable


def test_an_upgrade_heading_away_from_its_measured_band_does_not_gate_missions() -> None:
    """The guard keys on a heading where a heading actually sits.

    A box reading exactly "ATTACK UPGRADES" but at a card's y is not the
    heading of an upgrade page; treating it as one is the bug being fixed,
    just with a label instead of a description.
    """
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / 'menu_missions_claimable.png'))
    boxes = recorded('menu_missions_claimable') + (
        ocr.TextBox('ATTACK UPGRADES', .99, config.Rect(40, 1100, 400, 40)),)
    result = screen_discovery.discover(frame, boxes, 'missions')
    assert (result.screen_id, result.readable) == ('missions.daily', True)


def test_the_missions_band_counting_drawn_is_no_longer_an_inference() -> None:
    """`menu_missions_claimable` reads 8/8 on a page holding four claimable
    and four in-progress missions. Finished-of-offered would read 4/8 there,
    so the band counts what was drawn. That was listed as unproven while the
    only capture was a 2/8 page with nothing completed on it."""
    import missions_screen, screen_discovery
    assert 'missions.shown_of_offered' not in screen_discovery.capabilities()['unproven']
    holder = missions_screen.MissionsReadings()
    holder.scan(cv2.imread(str(FIXTURES / 'menu_missions_claimable.png')))
    latest = holder.snapshot()['latest']
    assert (latest['shown'], latest['offered'], latest['unseen']) == (8, 8, 0)
    # `shown` cannot be a count of what the reader parsed: the band says 8
    # while only five cards are on the frame. Three of those are CLAIM rows,
    # which the reader identifies as 'claimable' rather than as unreadable -
    # a bar being replaced by a button is not a failed read - but they still
    # carry no "N / M" progress text, so the band's 8 is not five parsed cards
    # stretched to fit either.
    assert len(latest['missions']) < latest['shown']
    assert [m for m in latest['missions'] if m['status'] == 'claimable']


def test_new_heading_cannot_validate_an_existing_target() -> None:
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / 'menu_workshop_attack.png'))
    boxes = recorded('menu_workshop_attack') + (
        ocr.TextBox('UTILITY UPGRADES', .99, config.Rect(50, 401, 450, 40)),)
    assert not screen_discovery.discover(frame, boxes, 'workshop').readable


@pytest.mark.parametrize('category,label,identity', [
    ('DEFENSE', 'Health', 'health'), ('UTILITY', 'Cash Bonus', 'cash_bonus'),
])
def test_existing_battle_categories_keep_semantic_targets(
    category: str, label: str, identity: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Compatibility regression using existing recorded geometry; these are
    # not recordings of Defense/Utility and do not claim recorded coverage.
    # The frame's heading bar is Attack-blue; silence the colour so the
    # relabelled text alone names the tab.
    monkeypatch.setattr(battle_tab, 'classify_frame', lambda _: None)
    frame = cv2.imread(str(FIXTURES / 'in_run_lit.png'))
    boxes = tuple(ocr.TextBox(category + 'UPGRADES' if b.text == 'ATTACKUPGRADES'
                              else label if b.text == 'Damage' else b.text,
                              b.confidence, b.rect) for b in recorded('in_run_lit'))
    monkeypatch.setattr(ocr, 'read', lambda _: boxes)
    row = next(r for r in perception.observe_frame(frame, 'battle').rows if r.upgrade_id == identity)
    assert row.tap is not None
    assert row.price == 10


@pytest.mark.parametrize('name,context', [
    ('in_run_lit', 'battle'), ('menu_workshop_defense', 'workshop'),
])
def test_defence_heading_alias_preserves_runtime_health_target(
    name: str, context: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import screen_discovery
    # in_run_lit's heading bar is Attack-blue; silence the colour so the
    # relabelled text alone names the tab.
    monkeypatch.setattr(battle_tab, 'classify_frame', lambda _: None)
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    boxes = tuple(ocr.TextBox('DEFENCE UPGRADES' if b.text in ('ATTACKUPGRADES', 'DEFENSEUPGRADES')
                             else 'Health' if b.text == 'Damage' else b.text,
                             b.confidence, b.rect) for b in recorded(name))
    monkeypatch.setattr(ocr, 'read', lambda _: boxes)
    discovery = screen_discovery.discover(frame, boxes, context)
    assert discovery.screen_id == f'{context}.defense'
    row = next(r for r in perception.observe_frame(frame, context).rows if r.upgrade_id == 'health')
    assert row.tap is not None


# --- Second unlock stage -----------------------------------------------------
#
# The captures below were recorded on a younger account than the originals.
# They are the evidence behind dropping 'later_unlock_stage_layouts' from the
# support matrix: the same reader must name the same screen on an account whose
# rows are not the same rows.

@pytest.mark.parametrize('name,context,expected', [
    ('menu_workshop_attack_early', 'workshop', 'workshop.attack'),
    ('menu_workshop_defense_early', 'workshop', 'workshop.defense'),
    ('menu_workshop_utility_early', 'workshop', 'workshop.utility'),
    ('in_run_early', 'battle', 'battle.attack'),
    ('in_run_attack_paused', 'battle', 'battle.attack'),
    ('in_run_defense', 'battle', 'battle.defense'),
    ('in_run_utility', 'battle', 'battle.utility'),
])
def test_a_second_unlock_stage_resolves_the_same_screen_ids(
    name: str, context: str, expected: str,
) -> None:
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    result = screen_discovery.discover(frame, recorded(name), context)
    assert (result.screen_id, result.readable) == (expected, True)


# Every recorded upgrade capture, in both stages. Shared by the two tests
# that have to answer for rows the catalog cannot name: neither may pin
# itself to a single frame, because a frame stops showing such a row the
# moment the catalog learns its name.
_WORKSHOP_CAPTURES = (
    ('menu_workshop_attack', 'workshop'), ('menu_workshop_attack_early', 'workshop'),
    ('menu_workshop_defense', 'workshop'), ('menu_workshop_defense_early', 'workshop'),
    ('menu_workshop_utility', 'workshop'), ('menu_workshop_utility_early', 'workshop'),
)


def _rows_by_id(name: str, context: str) -> dict[str, object]:
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    return {row.upgrade_id: row for row in
            perception.parse_frame(frame, recorded(name), context).rows}


@pytest.mark.parametrize('name,context', [
    ('menu_workshop_attack', 'workshop'), ('menu_workshop_attack_early', 'workshop'),
    ('menu_workshop_defense', 'workshop'), ('menu_workshop_defense_early', 'workshop'),
    ('menu_workshop_utility', 'workshop'), ('menu_workshop_utility_early', 'workshop'),
    ('in_run_lit', 'battle'), ('in_run_early', 'battle'),
    ('in_run_defense', 'battle'), ('in_run_utility', 'battle'),
])
def test_a_tap_belongs_to_exactly_the_row_that_produced_it(name: str, context: str) -> None:
    """The acceptance gate's second clause, stated per frame.

    "A new tab does not shift a purchase to a different target" is only true
    if a row's tap point is inside that row and inside no other. A tap that
    lands in a neighbour is the exact failure the gate names, and it would not
    be caught by checking identities alone.
    """
    rows = _rows_by_id(name, context)
    taps = {key: row.tap for key, row in rows.items() if row.tap is not None}
    assert taps, f'{name} exposed no purchasable row to check'
    for key, (x, y) in taps.items():
        own = rows[key].rect
        assert own.x <= x < own.x + own.w and own.y <= y < own.y + own.h, (
            f'{key} taps outside its own row on {name}')
        for other, row in rows.items():
            if other == key:
                continue
            assert not (row.rect.x <= x < row.rect.x + row.rect.w
                        and row.rect.y <= y < row.rect.y + row.rect.h), (
                f'{key} taps inside {other} on {name}')


@pytest.mark.parametrize('late,early,context', [
    ('menu_workshop_attack', 'menu_workshop_attack_early', 'workshop'),
    ('menu_workshop_defense', 'menu_workshop_defense_early', 'workshop'),
    ('in_run_lit', 'in_run_early', 'battle'),
])
def test_an_upgrade_keeps_its_identity_and_its_own_target_across_stages(
    late: str, early: str, context: str,
) -> None:
    """An upgrade seen on both accounts is the same upgrade, priced differently.

    The price is asserted to be read independently per stage precisely because
    the identity is shared: binding a stale price to a live row is the way a
    correct identity still produces a wrong purchase.
    """
    first, second = _rows_by_id(late, context), _rows_by_id(early, context)
    shared = set(first) & set(second)
    assert shared, f'{late} and {early} share no upgrade to compare'
    for key in shared:
        for rows in (first, second):
            row = rows[key]
            assert row.upgrade_id == key
            if row.tap is not None:
                assert row.price is not None
                x, y = row.tap
                assert row.rect.x <= x < row.rect.x + row.rect.w
                assert row.rect.y <= y < row.rect.y + row.rect.h


def test_a_row_the_catalog_cannot_name_is_never_tappable() -> None:
    """A real upgrade the catalog has never heard of.

    The second-stage Attack page used to carry Attack Range and an Unlock
    Multishot row, neither of which resolved; the catalog names both now, and
    no recorded capture is left with an unnameable row. The guard still has to
    hold for the next one the game adds, so it is asserted across the whole
    corpus rather than against one capture that has since been catalogued: a
    row the reader cannot name must never be offered a tap.
    """
    discovered = [row for name, context in _WORKSHOP_CAPTURES
                  for row in _rows_by_id(name, context).values()
                  if row.upgrade_id.startswith('discovered:')]
    for row in discovered:
        assert row.tap is None


def test_the_ultimate_weapons_page_is_not_mistaken_for_a_purchase_screen() -> None:
    """Recorded, deliberately unclaimed.

    Reading Ultimate Weapons belongs to its own task. What must hold here is
    that an unclaimed page is refused outright rather than parsed as whichever
    upgrade screen it most resembles.
    """
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / 'menu_workshop_ultimate.png'))
    boxes = recorded('menu_workshop_ultimate')
    result = screen_discovery.discover(frame, boxes, 'workshop')
    assert result.screen_id is None and not result.readable
    assert perception.parse_frame(frame, boxes, 'workshop').rows == ()


def test_a_first_wave_game_over_never_exposes_battle_rows() -> None:
    """The wave-1 capture joins the existing game-over evidence."""
    import screen_discovery
    frame = cv2.imread(str(FIXTURES / 'game_over_wave1.png'))
    boxes = recorded('game_over_wave1')
    result = screen_discovery.discover(frame, boxes, 'battle')
    assert result.screen_id is None and not result.readable
    assert perception.parse_frame(frame, boxes, 'battle').rows == ()


def test_the_support_matrix_no_longer_claims_stages_and_nested_menus_are_unseen() -> None:
    """Recorded evidence must move an item out of 'unsupported'.

    'unsupported' means no capture exists. Once one does, leaving the entry in
    place understates the bot and, worse, makes the list stop meaning anything.
    """
    import screen_discovery
    capabilities = screen_discovery.capabilities()
    unsupported = capabilities['unsupported']
    assert 'later_unlock_stage_layouts' not in unsupported
    assert 'nested_account_menus' not in unsupported
    # Narrowed rather than dropped: the Workshop tabs have a second stage and
    # nothing else does, so the honest entry names the remainder.
    assert 'later_unlock_stage_layouts_outside_workshop' in unsupported
    # Still genuinely out of scope, and each attributed to the task that owns it.
    assert {'other_locales', 'other_resolutions', 'unknown_overlays'} <= set(unsupported)
    assert capabilities['unsupported_owners']['missions_claim_actions'] == 'T01'
    assert capabilities['unsupported_owners']['other_resolutions'] == 'V06'


def test_every_recorded_reader_names_evidence_that_exists() -> None:
    import screen_discovery
    readers = screen_discovery.capabilities()['readers']
    assert {'battle.defense', 'battle.utility'} <= set(readers)
    for name in readers.values():
        assert (FIXTURES / f'{name}.png').exists()
        assert (FIXTURES / 'ocr' / f'{name}.json').exists()


def test_every_claimed_unlock_stage_pair_really_shows_different_rows() -> None:
    """The table has to earn each row, not just list it.

    Two captures of one account would satisfy any check that only counts
    entries, and would quietly turn this table into decoration. So read both
    captures of every claimed pair and require the row sets to differ. This is
    the check that rejected battle, account stats and missions: their second
    captures parse identically, or differ only by a daily rotation.
    """
    import screen_discovery
    stages = screen_discovery.capabilities()['recorded_unlock_stages']
    assert set(stages) == {'workshop.attack', 'workshop.defense', 'workshop.utility'}
    for screen, names in stages.items():
        assert len(names) >= 2, f'{screen} claims a stage list of one capture'
        context = screen.split('.')[0]
        seen = [set(_rows_by_id(name, context)) for name in names]
        for later in seen[1:]:
            assert later != seen[0], f'{screen} claims two stages that read alike'


def test_a_capture_that_is_not_stage_evidence_is_not_claimed_as_stage_evidence() -> None:
    """in_run_early is real evidence, and not evidence of a second stage.

    It is recorded, it is read, and it parses to exactly the rows in_run_lit
    parses to. Keeping it out of the stage table while keeping it in the
    fixtures is the distinction the matrix exists to make.
    """
    import screen_discovery
    stages = screen_discovery.capabilities()['recorded_unlock_stages']
    assert 'battle.attack' not in stages
    assert set(_rows_by_id('in_run_lit', 'battle')) == set(_rows_by_id('in_run_early', 'battle'))


def test_uncatalogued_labels_match_what_the_captures_actually_show() -> None:
    """Naming the gap is the point - and so is not naming a closed one.

    These rows exist in the game and not in the catalog. The reader already
    refuses to tap them; recording them here is what turns a silent refusal
    into a piece of work someone can pick up. Checked against the corpus
    rather than against a hardcoded label, so the table cannot claim work
    that a catalog entry has since finished - which is exactly what it did
    claim for attackrange, unlockmultishotupgrades and unlockthornupgrades.
    """
    import screen_discovery
    pending = screen_discovery.capabilities()['uncatalogued_labels']
    assert all(isinstance(where, str) and where for where in pending.values())
    seen = {row.upgrade_id.removeprefix('discovered:'): name
            for name, context in _WORKSHOP_CAPTURES
            for row in _rows_by_id(name, context).values()
            if row.upgrade_id.startswith('discovered:')}
    assert pending == seen


def test_every_enabled_capability_declares_replay_coverage_or_an_owned_gap() -> None:
    """The matrix has to answer for the readers it turns on.

    'readers' and 'account_screens' are the list of things this bot will act
    on the output of. Until B08 there was no place that said, for any of
    them, what happens on a state they must NOT act on - a maxed row, a
    control drawn twice, a page the reader should refuse. `replay_coverage`
    is that place: every enabled capability is either covered by three
    replayed examples in tests/fixtures/replay/manifest.json, or named as a
    gap with the task that owns closing it. A capability in neither list is
    the silent case this check exists to prevent.
    """
    import screen_discovery
    capabilities = screen_discovery.capabilities()
    coverage = capabilities['replay_coverage']
    enabled = (set(capabilities['readers']) | set(capabilities['account_screens'])
               | set(capabilities.get('game_over', ())))
    gapped = {key.rsplit('.', 1)[0] for key in coverage['gaps']}

    assert set(coverage['covered']) | gapped == enabled
    assert not set(coverage['covered']) & gapped, (
        'a capability is either fully covered or has a named gap, never both')
    # A gap is work, so it carries an owner and never quietly re-enters
    # 'unsupported' - the screen is read, it is only one state that has no
    # example.
    assert coverage['gaps'] == {
        'account.stats.tiers.unavailable': 'B08',
        'labs.home.locked': 'B08',
        'labs.research.affordable': 'L02',
    }
    assert not gapped & set(capabilities['unsupported'])


def test_replay_coverage_never_claims_a_capability_the_matrix_calls_unsupported() -> None:
    """Coverage is for what is enabled. Listing an unsupported capability
    here would read as evidence for a reader that does not exist."""
    import screen_discovery
    capabilities = screen_discovery.capabilities()
    assert not set(capabilities['replay_coverage']['covered']) & set(
        capabilities['unsupported'])


def test_a_device_without_a_status_bar_still_reads_the_workshop() -> None:
    """The same page, drawn edge to edge, is still that page.

    Every recorded menu capture comes from a device that reserves a status
    bar, and the Workshop's title, heading and rows were measured below one.
    A device that draws the game to the top of the screen paints the very
    same page ~137px higher, and keying the gate on frame y refused it as an
    unsupported layout - so a live visit read no row, skipped its whole list
    as unreadable and aborted having bought nothing.

    Recorded from such a device, so the fixture is evidence of the page a
    real phone draws rather than a fixture translated to argue a point.
    """
    import screen_discovery
    name = 'menu_workshop_utility_no_status_bar'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    boxes = recorded(name)

    assert screen_discovery.discover(frame, boxes, 'workshop') == (
        screen_discovery.ScreenDiscovery('workshop.utility', True, 'recorded_layout'))

    rows = {row.upgrade_id: row for row
            in perception.parse_frame(frame, boxes, 'workshop').rows}
    assert set(rows) == {'cash_bonus', 'cash_per_wave', 'unlock_coin_bonuses'}
    assert all(row.tap is not None and row.price is not None for row in rows.values())
    # The rows sit where this device draws them, not where the inset-bearing
    # captures do: a tap taken from the recorded page would miss them.
    inset_bearing = _rows_by_id('menu_workshop_utility_early', 'workshop')
    for key, row in rows.items():
        assert row.tap[1] < inset_bearing[key].tap[1]


def test_the_page_title_still_has_to_be_above_its_own_heading() -> None:
    """Relative geometry is still geometry: the gap is bounded both ways.

    A 'WORKSHOP' box anywhere above the heading would satisfy "the title is
    higher up"; only a title the measured distance above it is evidence that
    this is the Workshop page and not a frame that merely says the word.
    """
    import screen_discovery
    name = 'menu_workshop_utility_no_status_bar'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    boxes = tuple(b for b in recorded(name) if tiles.normalise(b.text) != 'workshop')
    strayed = boxes + (ocr.TextBox('WORKSHOP', .99, config.Rect(30, 30, 244, 45)),)

    result = screen_discovery.discover(frame, strayed, 'workshop')
    assert (result.screen_id, result.readable) == (None, False)
    assert result.reason == 'unsupported_layout'


def test_a_device_without_a_status_bar_still_reads_the_missions_page() -> None:
    """The same page, drawn edge to edge, is still that page.

    Recorded from such a device. The missions anchors were measured below a
    status bar, and keying them on frame y made the reader answer
    `scanned=True, screen_id=None` - its one branch meaning "examined, and no
    missions page is up" - while the missions page filled the screen.
    """
    import screen_discovery
    name = 'menu_missions_claimable_no_status_bar'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    boxes = recorded(name)

    assert screen_discovery.discover(frame, boxes, 'missions') == (
        screen_discovery.ScreenDiscovery('missions.daily', True, 'recorded_layout'))
    assert screen_discovery.missions_count(boxes) == (8, 8)


def test_the_inset_bearing_missions_capture_still_reads() -> None:
    """The fix is a widening, not a move: the recorded layout still passes."""
    import screen_discovery
    for name in ('menu_missions', 'menu_missions_claimable', 'menu_missions_weekly'):
        frame = cv2.imread(str(FIXTURES / f'{name}.png'))
        result = screen_discovery.discover(frame, recorded(name), 'missions')
        assert (result.screen_id, result.readable) == ('missions.daily', True), name


def test_the_missions_title_still_has_to_be_above_its_own_banner() -> None:
    """Relative geometry is still geometry: the gap is bounded both ways.

    A 'DAILY MISSIONS' box anywhere higher up would satisfy "the title is
    above the banner"; only a title the measured distance above it is evidence
    that this is the missions page rather than a frame that says the words.
    """
    import screen_discovery
    name = 'menu_missions_claimable_no_status_bar'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    kept = tuple(b for b in recorded(name)
                 if tiles.normalise(b.text) != 'dailymissions')
    # Inside the widened title band, but far too high above the banner.
    strayed = kept + (ocr.TextBox('DAILYMISSIONS', .99, config.Rect(32, 60, 432, 40)),)

    result = screen_discovery.discover(frame, strayed, 'missions')
    assert (result.screen_id, result.readable) == (None, False)


def test_an_upgrade_page_is_still_refused_on_an_inset_free_device() -> None:
    """The guard the widening could plausibly have broken, kept.

    Not the title band: a Workshop title normalises to `workshop` and was
    never a candidate missions title. It is the HEADING band that widened,
    from 380 down to 380 - MAX_TOP_INSET, and this capture's UTILITY
    UPGRADES heading sits at y=261 - inside the widened band and outside the
    original. So the widening is what lets the upgrade guard see this frame
    at all, and the reason is what proves the guard is what refused it
    rather than the mere absence of a missions title.
    """
    import screen_discovery
    name = 'menu_workshop_utility_no_status_bar'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    result = screen_discovery.discover(frame, recorded(name), 'missions')
    assert (result.screen_id, result.readable) == (None, False)
    assert result.reason == 'unsupported_layout'


def test_the_milestones_ladder_is_identified_on_both_captures() -> None:
    import screen_discovery
    for name in ('menu_milestones_claimable', 'menu_milestones_claimed'):
        frame = cv2.imread(str(FIXTURES / f'{name}.png'))
        found = screen_discovery.discover(frame, recorded(name), 'milestones')
        assert found.screen_id == 'milestones.ladder', name
        assert found.readable is True, name


def test_the_reward_modal_is_identified_and_is_not_the_ladder() -> None:
    import screen_discovery
    name = 'menu_milestones_reward_modal'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    found = screen_discovery.discover(frame, recorded(name), 'milestones')
    assert found.screen_id == 'milestones.reward_modal'
    assert found.readable is True


def test_the_ladder_and_the_modal_reject_each_other() -> None:
    """Separation measured, not assumed: the ladder carries no `skip`, and the
    modal's whole OCR is four boxes with no title. Each page must fail the
    other's anchors, or one walk step could act on the other's screen."""
    import screen_discovery
    ladder = recorded('menu_milestones_claimable')
    modal = recorded('menu_milestones_reward_modal')
    assert screen_discovery._milestones_title(modal) is None
    assert not [b for b in ladder if b.confidence >= .9
                and tiles.normalise(b.text) == 'skip']


def test_the_missions_page_is_not_a_milestones_ladder() -> None:
    """The missions page carries four CLAIM boxes and a return bar identical to
    the ladder's. Only the title tells them apart."""
    import screen_discovery
    name = 'menu_missions_claimable_no_status_bar'
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    found = screen_discovery.discover(frame, recorded(name), 'milestones')
    assert found.screen_id is None


def test_the_milestones_title_band_widens_downward_not_upward() -> None:
    """Every milestones capture is edge-to-edge, with the title at y=113, where
    the missions anchors were recorded on a status-bar device at y=249. A
    status-bar device draws THIS page LOWER, so the band opens downward. A band
    widened upward like the missions one would miss the very layout this page
    has never been captured on."""
    import screen_discovery
    boxes = recorded('menu_milestones_claimable')
    title = screen_discovery._milestones_title(boxes)
    assert title is not None and title.rect.y == 113
    shifted = tuple(
        ocr.TextBox(b.text, b.confidence,
                    config.Rect(b.rect.x, b.rect.y + 136, b.rect.w, b.rect.h))
        for b in boxes)
    moved = screen_discovery._milestones_title(shifted)
    assert moved is not None and moved.rect.y == 249


def test_the_tier_is_read_by_regex_not_by_a_normalised_label() -> None:
    """`tiles.normalise('Tier 1')` is 'tier1', which bakes the tier number into
    the label - so 'Tier 2' would need a second constant. Matched against raw
    text for the same reason the missions count band is."""
    import screen_discovery
    assert screen_discovery.milestones_tier(recorded('menu_milestones_claimable')) == 1


def test_two_titles_are_not_evidence_of_a_milestones_page() -> None:
    import screen_discovery
    boxes = recorded('menu_milestones_claimable')
    title = screen_discovery._milestones_title(boxes)
    assert title is not None
    doubled = boxes + (ocr.TextBox('MILESTONES', .99,
                                   config.Rect(31, title.rect.y + 60, 331, 40)),)
    assert screen_discovery._milestones_title(doubled) is None


def test_two_tier_labels_make_the_tier_ambiguous_rather_than_the_first_one() -> None:
    """A doubled anchor is not evidence of a page. Constructed rather than
    captured: no recorded frame has two Tier labels in band, and this pins the
    uniqueness rule itself rather than the page."""
    import screen_discovery
    boxes = recorded('menu_milestones_claimable')
    title = screen_discovery._milestones_title(boxes)
    assert title is not None
    doubled = boxes + (ocr.TextBox('Tier 2', .99,
                                   config.Rect(453, title.rect.y + 2000, 174, 63)),)
    assert screen_discovery.milestones_tier(doubled) is None
    frame = cv2.imread(str(FIXTURES / 'menu_milestones_claimable.png'))
    found = screen_discovery.discover(frame, doubled, 'milestones')
    assert found.screen_id is None
    assert found.reason == 'ambiguous_or_unreadable_heading'


def test_a_lone_claim_in_the_modal_band_is_not_a_reward_ceremony() -> None:
    """The modal is claimed on SKIP *and* CLAIM together. A lone CLAIM must not
    be enough: the missions page carries four CLAIM boxes, and reading one as a
    reward ceremony would have the walk tap a button nobody chose."""
    import screen_discovery
    boxes = recorded('menu_milestones_claimable')
    with_claim = boxes + (ocr.TextBox('CLAIM', .99, config.Rect(432, 1860, 217, 62)),)
    assert screen_discovery._single(with_claim, 'skip', screen_discovery._MODAL_SKIP_Y) is None
    assert screen_discovery._single(with_claim, 'claim', screen_discovery._MODAL_CLAIM_Y) is not None
    frame = cv2.imread(str(FIXTURES / 'menu_milestones_claimable.png'))
    found = screen_discovery.discover(frame, with_claim, 'milestones')
    assert found.screen_id == 'milestones.ladder'


def test_the_modal_bands_widen_downward_too_on_a_status_bar_device() -> None:
    """The modal has no title to measure a gap from, unlike the ladder - but it
    is the same device fact: a status-bar device draws this overlay LOWER than
    the edge-to-edge capture, not higher, so SKIP and CLAIM's absolute bands
    must widen downward exactly like the ladder title band does. Shifted by
    +136, the same measured offset the ladder title-band test uses, because
    both anchors come from the same one fact about the device."""
    import screen_discovery
    boxes = recorded('menu_milestones_reward_modal')
    shifted = tuple(
        ocr.TextBox(b.text, b.confidence,
                    config.Rect(b.rect.x, b.rect.y + 136, b.rect.w, b.rect.h))
        for b in boxes)
    frame = cv2.imread(str(FIXTURES / 'menu_milestones_reward_modal.png'))
    found = screen_discovery.discover(frame, shifted, 'milestones')
    assert found.screen_id == 'milestones.reward_modal'


# --- Game Over modal (reroll-relevant outcome evidence) ---------------------
#
# Five real captures back game_over.py, all from the same status-bar-inset
# device (see tests/fixtures/replay/manifest.json's `frames` table): a stable
# non-record modal in two coin layouts (game_over, game_over_stats), a fuller
# account's stable modal (game_over_wave1), and two record-run modals whose
# 'New Highest Wave!' line shifts every caption below Wave down (game_over_fade,
# game_over_newhigh). Nothing here is derived - every status below is what the
# real OCR for that capture produces.

_GAME_OVER_EXPECTED: dict[str, dict[str, tuple[str, str | None]]] = {
    # Single-line coins layout, no BONUS banner, no record line.
    'game_over': {
        'wave': ('observed', '1'), 'new_highest_wave': ('absent', None),
        'tier': ('observed', '1'), 'highest_wave': ('observed', '2'),
        'killed_by': ('observed', 'Basic'), 'bonus_status': ('absent', None),
        'coins_earned': ('unreadable', None), 'ad_coins_earned': ('absent', None),
        'total_coins': ('absent', None),
    },
    # Same single-line layout, but the run set a new record.
    'game_over_fade': {
        'wave': ('observed', '2'), 'new_highest_wave': ('observed', 'NewHighestWave!'),
        'tier': ('observed', '1'), 'highest_wave': ('observed', '2'),
        'killed_by': ('observed', 'Basic'), 'bonus_status': ('absent', None),
        'coins_earned': ('unreadable', None), 'ad_coins_earned': ('absent', None),
        'total_coins': ('absent', None),
    },
    # Three-column coins layout, BONUS banner drawn, no record line. The
    # coins_earned digit reads at .8862 - below the .90 floor - and the
    # total_coins column picks up the currency glyph as a second candidate;
    # two independent real reasons to land on 'unreadable'.
    'game_over_stats': {
        'wave': ('observed', '1'), 'new_highest_wave': ('absent', None),
        'tier': ('observed', '1'), 'highest_wave': ('observed', '2'),
        'killed_by': ('observed', 'Basic'), 'bonus_status': ('observed', 'Inactive'),
        'coins_earned': ('unreadable', None), 'ad_coins_earned': ('observed', '0'),
        'total_coins': ('unreadable', None),
    },
    # Three-column layout, BONUS banner, AND a new record - the shifted
    # layout the manifest's positive example is deliberately drawn from.
    'game_over_newhigh': {
        'wave': ('observed', '6'), 'new_highest_wave': ('observed', 'NewHighestWave!'),
        'tier': ('observed', '1'), 'highest_wave': ('observed', '6'),
        'killed_by': ('observed', 'Basic'), 'bonus_status': ('observed', 'Inactive'),
        'coins_earned': ('unreadable', None), 'ad_coins_earned': ('observed', '0'),
        'total_coins': ('observed', '21'),
    },
    # A different, fuller account (Highest Wave: 10, not a record this run).
    # total_coins draws a label with no value beneath it at all.
    'game_over_wave1': {
        'wave': ('observed', '1'), 'new_highest_wave': ('absent', None),
        'tier': ('observed', '1'), 'highest_wave': ('observed', '10'),
        'killed_by': ('observed', 'Basic'), 'bonus_status': ('observed', 'Inactive'),
        'coins_earned': ('observed', '2'), 'ad_coins_earned': ('observed', '0'),
        'total_coins': ('unreadable', None),
    },
}


@pytest.mark.parametrize('name', sorted(_GAME_OVER_EXPECTED))
def test_game_over_reads_every_recorded_field_state(name: str) -> None:
    """Every field, on every recorded capture, pinned to its real read.

    This is the exhaustive form of the three canonical examples the replay
    manifest carries for game_over.result: it is what proves "stable modal",
    "New Highest Wave layout shift", "absent field" and "unreadable field"
    are all real, not just the one observation each the manifest names.
    Absent and unreadable are never the same row here - contrast
    ad_coins_earned (absent on the single-line captures, never even drawn)
    with total_coins on game_over_wave1 (its caption IS drawn, and is
    'unreadable' because the game over draws no value beneath it).
    """
    import game_over
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    reading = game_over.parse_frame(frame, recorded(name), now=1_700_000_000.0)
    assert reading is not None
    assert reading.screen_id == 'game_over.result'
    actual = {f.key: (f.status, f.raw_value) for f in reading.fields}
    assert actual == _GAME_OVER_EXPECTED[name]


@pytest.mark.parametrize('name', ['menu_workshop_attack', 'menu_missions', 'in_run_lit'])
def test_game_over_refuses_a_frame_that_is_not_the_death_modal(name: str) -> None:
    """No GAMESTATS title, no reading - never a guess built from whatever
    labels happen to be on screen."""
    import game_over
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    assert game_over.parse_frame(frame, recorded(name), now=1_700_000_000.0) is None


def test_game_over_refuses_a_low_confidence_title() -> None:
    """A title present but not trusted is refused exactly like no title at
    all - the closest this fixture set can get to a still-fading-in modal
    with no capture of one. See the 'unproven' entry this gap is filed
    under in screen_discovery.capabilities()."""
    import game_over
    boxes = recorded('game_over')
    faint = tuple(
        ocr.TextBox(b.text, .5, b.rect) if b.text == 'GAMESTATS' else b
        for b in boxes)
    assert any(b.confidence == .5 for b in faint)
    frame = cv2.imread(str(FIXTURES / 'game_over.png'))
    assert game_over.parse_frame(frame, faint, now=1_700_000_000.0) is None
