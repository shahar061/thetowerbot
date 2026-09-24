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


def test_the_milestones_screens_are_not_taken_for_an_unlock_card() -> None:
    """The ladder names the same reward as "Unlock Lab" - never "unlocked"."""
    for name in ('menu_milestones_claimable', 'menu_milestones_reward_modal'):
        frame = cv2.imread(str(FIXTURES / f'{name}.png'))
        assert unlocked_screen.read(frame, recorded(name)) is None, name


def test_a_card_without_exactly_one_centred_ok_below_the_caption_is_refused() -> None:
    caption, ok = BOXES[1], BOXES[2]
    cases = {
        'no ok': (caption,),
        'two oks': (caption, ok, box('OK', (488, 2000, 107, 71))),
        'off-centre ok': (caption, box('OK', (100, 1856, 107, 71))),
        'ok above caption': (box('OK', (488, 900, 107, 71)), caption),
        'unsure caption': (box('Lab unlocked', caption.rect, .5), ok),
        'bare label': (box('Unlocked', caption.rect), ok),
        'crowded page': (caption, ok) + tuple(box(f'row {i}', (0, 40 * i, 50, 20))
                                              for i in range(8)),
    }
    for name, boxes in cases.items():
        assert unlocked_screen.read(FRAME, boxes) is None, name
