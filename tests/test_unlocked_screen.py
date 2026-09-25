"""The generic "<feature> unlocked" card reader, against its recorded frame."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2

import config
import ocr
import unlocked_screen

FIXTURES = Path(__file__).parent / 'fixtures'


def recorded(name: str) -> tuple[Any, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / 'ocr' / f'{name}.json').read_text()))


FRAME = cv2.imread(str(FIXTURES / 'menu_content_unlocked.png'))
BOXES = recorded('menu_content_unlocked')
TOURNAMENT_FRAME = cv2.imread(str(FIXTURES / 'menu_tournament_unlocked.png'))
TOURNAMENT_BOXES = recorded('menu_tournament_unlocked')


def box(text: str, rect: tuple[int, int, int, int], confidence: float = .98) -> Any:
    return ocr.TextBox(text, confidence, config.Rect(*rect))


def test_the_recorded_lab_card_is_read_with_its_ok_centre() -> None:
    assert unlocked_screen.read(FRAME, BOXES) == unlocked_screen.Unlocked(
        'Lab unlocked', (541, 1891))


def test_any_feature_name_is_read_the_same_way() -> None:
    for caption in ('Cards unlocked', 'Ultimate Weapons unlocked!', 'Labunlocked'):
        boxes = tuple(box(caption, b.rect) if 'unlocked' in b.text else b for b in BOXES)
        found = unlocked_screen.read(FRAME, boxes)
        assert found is not None and found.caption == caption.strip()


def test_lab_unlock_caption_normalization_handles_missing_space_and_split_ocr() -> None:
    for caption in ("Lab unlocked", "Labunlocked", "LABS UNLOCKED"):
        assert unlocked_screen.is_labs_unlock(caption)
    assert not unlocked_screen.is_labs_unlock("Cards unlocked")


def test_recorded_two_line_tournament_card_is_read_with_its_ok_centre() -> None:
    assert unlocked_screen.read(TOURNAMENT_FRAME, TOURNAMENT_BOXES) == unlocked_screen.Unlocked(
        'Tournament unlocked', (541, 1891))


def test_split_unlock_caption_requires_one_nearby_centred_feature() -> None:
    skip, feature, unlocked, ok = (TOURNAMENT_BOXES[0], TOURNAMENT_BOXES[2],
                                   TOURNAMENT_BOXES[3], TOURNAMENT_BOXES[4])
    cases = {
        'no feature': (skip, unlocked, ok),
        'off-centre feature': (skip, box('Tournament', (10, 1356, 521, 75)), unlocked, ok),
        'distant feature': (skip, box('Tournament', (285, 900, 521, 75)), unlocked, ok),
        'ambiguous feature': (skip, feature, box('Events', (285, 1356, 521, 75)),
                              unlocked, ok),
    }
    for name, boxes in cases.items():
        assert unlocked_screen.read(TOURNAMENT_FRAME, boxes) is None, name


def test_the_milestones_screens_are_not_taken_for_an_unlock_card() -> None:
    """The ladder names the same reward as "Unlock Lab" - never "unlocked"."""
    for name in ('menu_milestones_claimable', 'menu_milestones_reward_modal'):
        frame = cv2.imread(str(FIXTURES / f'{name}.png'))
        assert unlocked_screen.read(frame, recorded(name)) is None, name


def test_a_card_without_exactly_one_centred_ok_below_the_caption_is_refused() -> None:
    skip, caption, ok = BOXES
    cases = {
        'no ok': (skip, caption),
        'two oks': (skip, caption, ok, box('OK', (488, 2000, 107, 71))),
        'off-centre ok': (skip, caption, box('OK', (100, 1856, 107, 71))),
        'ok above caption': (skip, box('OK', (488, 900, 107, 71)), caption),
        'unsure caption': (skip, box('Lab unlocked', caption.rect, .5), ok),
        'bare label': (skip, box('Unlocked', caption.rect), ok),
        'crowded page': (skip, caption, ok) + tuple(box(f'row {i}', (0, 40 * i, 50, 20))
                                                    for i in range(8)),
    }
    for name, boxes in cases.items():
        assert unlocked_screen.read(FRAME, boxes) is None, name


def test_unlock_card_requires_skip_in_the_recorded_top_right_area() -> None:
    skip, caption, ok = BOXES
    cases = {
        'no skip': (caption, ok),
        'two skips': (skip, box('SKIP', (760, 272, 110, 48)), caption, ok),
        'skip at bottom': (box('SKIP', (842, 1800, 110, 48)), caption, ok),
        'skip on left': (box('SKIP', (100, 272, 110, 48)), caption, ok),
        'ok at top': (skip, caption, box('OK', (488, 1600, 107, 71))),
    }
    for name, boxes in cases.items():
        assert unlocked_screen.read(FRAME, boxes) is None, name
