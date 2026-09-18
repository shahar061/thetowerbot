"""The MILESTONES reader, against the three committed captures.

Every value asserted here was measured off tests/fixtures/ocr/menu_milestones_*.json,
not hand-written. Slice 2's first task was blocked for a whole round by expected
ids that the code could not produce for any input; measure, do not guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2

import config
import milestones_screen
import ocr

FIXTURES = Path(__file__).parent / 'fixtures'


def boxes(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / 'ocr' / f'{name}.json').read_text()))


def frame(name: str):
    img = cv2.imread(str(FIXTURES / f'{name}.png'), cv2.IMREAD_COLOR)
    assert img is not None, f'missing fixture: {name}.png'
    return img


def test_a_claimable_ladder_offers_claim_all_at_its_measured_rect() -> None:
    reading = milestones_screen.parse_frame(
        frame('menu_milestones_claimable'), boxes('menu_milestones_claimable'))
    assert reading is not None
    assert reading.screen_id == 'milestones.ladder'
    assert reading.tier == 1
    assert reading.claim_all == (420, 255, 246, 53)


def test_native_1920_ladder_uses_its_own_footer_position() -> None:
    """The title and Claim All stay put while the footer moves up 480 px."""
    reading = milestones_screen.parse_frame(
        frame('menu_milestones_claimable_1920'),
        boxes('menu_milestones_claimable_1920'))
    assert reading is not None
    assert reading.screen_id == 'milestones.ladder'
    assert reading.tier == 1
    assert reading.claim_all == (421, 256, 245, 51)


def test_a_claimed_ladder_offers_nothing_and_that_ends_the_walk() -> None:
    """The terminal condition. `Claim All` is GONE one tap later, while the
    premium 50 COINS still glows - which is why the loop is keyed on this and
    never on a reward's appearance."""
    reading = milestones_screen.parse_frame(
        frame('menu_milestones_claimed'), boxes('menu_milestones_claimed'))
    assert reading is not None
    assert reading.screen_id == 'milestones.ladder'
    assert reading.claim_all is None


def test_the_still_glowing_premium_reward_is_present_on_both_frames() -> None:
    """Pins the fact the terminal condition exists for: the right-hand
    `50 COINS` at wave 10 is on the claimable frame AND the claimed one, with
    no check mark, because it is gated behind Premium Pass 1. A loop keyed on
    'a reward is still showing' would never end."""
    for name in ('menu_milestones_claimable', 'menu_milestones_claimed'):
        texts = [b.text for b in boxes(name)]
        assert '50 COINS' in texts, name


def test_the_modal_names_its_own_reward_and_currency() -> None:
    reading = milestones_screen.parse_frame(
        frame('menu_milestones_reward_modal'), boxes('menu_milestones_reward_modal'))
    assert reading is not None
    assert reading.screen_id == 'milestones.reward_modal'
    assert reading.reward_text == '25 COINS'
    assert (reading.currency, reading.amount) == ('coins', 25)


def test_multi_reward_modal_locates_next_and_claim_at_both_heights() -> None:
    cases = (
        ('menu_milestones_reward_next_1920', 'next', 1, 2, (447, 1622, 186, 59)),
        ('menu_milestones_reward_next_2400', 'next', 1, 2, (444, 1860, 191, 63)),
        ('menu_milestones_reward_claim_1920', 'claim', 2, 2, (433, 1621, 214, 61)),
        ('menu_milestones_reward_claim_2400', 'claim', 2, 2, (432, 1860, 217, 62)),
    )
    for name, action, index, total, rect in cases:
        reading = milestones_screen.parse_frame(frame(name), boxes(name))
        assert reading is not None, name
        assert reading.screen_id == 'milestones.reward_modal'
        assert (reading.modal_action, reading.modal_index, reading.modal_total) == (
            action, index, total)
        assert reading.modal_control == rect


def test_the_reward_regex_reads_the_unspaced_spelling_too() -> None:
    """The captures genuinely contain BOTH `25 COINS` and `15GEMS`. That is why
    this is a regex against raw text and not a tiles.normalise comparison."""
    assert milestones_screen.reward_of('15GEMS') == ('gems', 15)
    assert milestones_screen.reward_of('25 COINS') == ('coins', 25)
    assert milestones_screen.reward_of('10 GEMS') == ('gems', 10)


def test_a_non_currency_reward_is_read_as_no_currency_not_as_a_failure() -> None:
    """`Unlock Lab` is a real reward on the recorded ladder. It moved no
    currency, which the ledger encodes as delta=0 - a different fact from an
    amount that could not be read."""
    assert milestones_screen.reward_of('Unlock Lab') == (None, None)


def test_claim_all_is_matched_by_exact_equality_not_by_substring() -> None:
    """`tiles.normalise('Claim All')` is 'claimall'. The missions reader matches
    'claim' by EXACT equality precisely so that this button is rejected there;
    this reader matches 'claimall' by exact equality for the mirror reason - a
    substring test here would accept a card reading 'Claim All Rewards Now'."""
    import tiles
    assert tiles.normalise('Claim All') == 'claimall'
    constructed = boxes('menu_milestones_claimable') + (
        ocr.TextBox('Claim All Rewards', .99, config.Rect(420, 320, 300, 53)),)
    reading = milestones_screen.parse_frame(
        frame('menu_milestones_claimable'), constructed)
    assert reading is not None
    assert reading.claim_all == (420, 255, 246, 53)


def test_two_claim_all_boxes_yield_no_target_rather_than_a_guessed_one() -> None:
    doubled = boxes('menu_milestones_claimable') + (
        ocr.TextBox('Claim All', .99, config.Rect(420, 400, 246, 53)),)
    reading = milestones_screen.parse_frame(
        frame('menu_milestones_claimable'), doubled)
    assert reading is not None
    assert reading.claim_all is None


def test_scan_holds_actions_on_both_milestones_screens() -> None:
    for name in ('menu_milestones_claimable', 'menu_milestones_claimed',
                 'menu_milestones_reward_modal'):
        readings = milestones_screen.MilestonesReadings()
        assert readings.scan(frame(name), boxes=boxes(name)) is True, name


def test_scan_releases_actions_on_a_frame_that_is_not_a_milestones_page() -> None:
    """`scanned=True` with `screen_id=None` is this reader's one branch meaning
    'examined, and no milestones page is up'. It is the only answer allowed to
    stand for a clear screen."""
    readings = milestones_screen.MilestonesReadings()
    name = 'menu_missions_claimable_no_status_bar'
    assert readings.scan(frame(name), boxes=boxes(name)) is False
    evidence = readings.current_evidence()
    assert evidence == {'screen_id': None, 'error': None, 'scanned': True}


def test_claim_evidence_carries_this_frames_target_and_nothing_older() -> None:
    readings = milestones_screen.MilestonesReadings()
    readings.scan(frame('menu_milestones_claimable'),
                  boxes=boxes('menu_milestones_claimable'))
    assert readings.claim_evidence()['claim_all'] == (420, 255, 246, 53)
    readings.scan(frame('menu_milestones_claimed'),
                  boxes=boxes('menu_milestones_claimed'))
    assert readings.claim_evidence()['claim_all'] is None


def test_a_modal_with_an_unreadable_claim_holds_rather_than_releases() -> None:
    """The hazard Ruling 7 exists for: a milestones screen that IS up but
    cannot be fully read must HOLD, never be read as 'nothing up' - releasing
    here would open the modal's own SKIP/CLAIM to shopping.py's OPEN_CARDS
    dismiss walk. Built by dropping CLAIM off the recorded modal boxes: SKIP
    alone is still presence (that is the whole point of keying the probe on
    SKIP), but discover() then cannot confirm which milestones screen this
    is, so parse_frame returns None and the frame must carry an explicit
    error rather than the all-clear 'error: None' that means nothing is up."""
    name = 'menu_milestones_reward_modal'
    without_claim = tuple(b for b in boxes(name) if b.text.strip().upper() != 'CLAIM')
    readings = milestones_screen.MilestonesReadings()
    assert readings.scan(frame(name), boxes=without_claim) is True
    evidence = readings.current_evidence()
    assert evidence['screen_id'] is None
    assert evidence['error'] is not None


def test_a_ladder_missing_its_title_still_holds_via_claim_all() -> None:
    """`claim_all` is exactly as discriminating as SKIP - present on ONE of 29
    committed OCR fixtures, this ladder - and closing on it matters because
    this is the one frame that actually has something to claim: without it,
    an unreadable heading here would release the guard onto a live Claim All
    button instead of holding."""
    name = 'menu_milestones_claimable'
    without_title = tuple(b for b in boxes(name) if b.text.strip().upper() != 'MILESTONES')
    readings = milestones_screen.MilestonesReadings()
    assert readings.scan(frame(name), boxes=without_title) is True
    evidence = readings.current_evidence()
    assert evidence['screen_id'] is None
    assert evidence['error'] is not None


def test_the_missions_page_does_not_trip_the_modal_presence_probe() -> None:
    """The missions page's lowest CLAIM box sits at y=2078, which now falls
    INSIDE the widened claim band (1840, 2080) by 2 pixels. Presence requires
    a trusted SKIP too, and the missions page has none, so this pins that the
    skip-AND-claim conjunction - not the band's upper edge - is what keeps
    this page from being read as a possible reward ceremony."""
    import screen_discovery
    name = 'menu_missions_claimable_no_status_bar'
    found = screen_discovery.discover(frame(name), boxes(name), 'milestones')
    assert found.screen_id is None
    readings = milestones_screen.MilestonesReadings()
    assert readings.scan(frame(name), boxes=boxes(name)) is False
    assert readings.current_evidence()['error'] is None


def test_supplied_boxes_are_used_instead_of_reading_the_frame(
        monkeypatch) -> None:
    name = 'menu_milestones_claimable'

    def boom(*args, **kwargs):
        raise AssertionError('ocr.read must not be called when boxes are supplied')

    monkeypatch.setattr(milestones_screen.ocr, 'read', boom)
    readings = milestones_screen.MilestonesReadings()
    assert readings.scan(frame(name), boxes=boxes(name)) is True


def test_a_malformed_box_holds_without_ever_calling_ocr(monkeypatch) -> None:
    """The except branch is reachable from _milestones_title/_modal_skip/
    parse_frame raising on a bad box, not only from ocr.read itself - once a
    caller can supply boxes, 'OCR failed' is no longer always true. Pin the
    behaviour (HOLD, with an error recorded) without pinning the wording,
    which is free to change as long as it never blames a stage that did not
    run."""
    class MalformedBox:
        @property
        def text(self) -> str:
            raise AssertionError('boxes must not be read this way')

        @property
        def confidence(self) -> float:
            raise AssertionError('boxes must not be read this way')

    calls: list[int] = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise AssertionError('ocr.read must not be called when boxes are supplied')

    monkeypatch.setattr(milestones_screen.ocr, 'read', boom)
    readings = milestones_screen.MilestonesReadings()
    name = 'menu_milestones_claimable'
    assert readings.scan(frame(name), boxes=(MalformedBox(),)) is True
    assert readings.current_evidence()['error'] is not None
    assert calls == []
