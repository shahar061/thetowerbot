"""Recognize the real Workshop quantity control, including OCR omission."""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

import config
import vision
from ocr import TextBox

ROOT = Path(__file__).parent / 'fixtures'


def capture(name: str) -> tuple[np.ndarray, tuple[TextBox, ...]]:
    if name in ('x1', 'expanded'):
        stem = 'x1_header' if name == 'x1' else name
        image = ROOT / f'workshop_multiplier/{stem}.png'
        labels = ROOT / f'workshop_multiplier/{stem}.json'
    else:
        image = ROOT / (name + '.png')
        labels = ROOT / 'ocr' / (name + '.json')
    rows = json.loads(labels.read_text())
    return cv2.imread(str(image)), tuple(TextBox(b['text'], b['confidence'], config.Rect(*b['rect'])) for b in rows)


def test_x1_is_recognized_even_when_ocr_omits_its_text() -> None:
    import workshop_multiplier
    image, boxes = capture('x1')
    result = workshop_multiplier.read(image, vision.TemplateCache(config.TEMPLATE_DIR), boxes)
    assert result.mode == 'single'


def test_max_header_control_is_not_a_maxed_upgrade() -> None:
    import workshop_multiplier
    image, boxes = capture('menu_workshop_attack_late')
    result = workshop_multiplier.read(image, vision.TemplateCache(config.TEMPLATE_DIR), boxes)
    assert result.mode == 'multiple'
    assert result.point is not None
    assert 906 <= result.point[0] <= 1040 and 305 <= result.point[1] <= 369


@pytest.mark.parametrize('name', ['menu_workshop_attack', 'menu_workshop_utility',
                                  'menu_workshop_utility_restocked'])
def test_early_workshop_without_quantity_control_keeps_single_buy_behavior(name: str) -> None:
    import workshop_multiplier
    image, boxes = capture(name)
    assert workshop_multiplier.read(image, vision.TemplateCache(config.TEMPLATE_DIR), boxes).mode == 'absent'


def test_unreadable_heading_cannot_authorize_a_quantity_change() -> None:
    import workshop_multiplier
    image, _ = capture('menu_workshop_attack_late')
    result = workshop_multiplier.read(image, vision.TemplateCache(config.TEMPLATE_DIR), ())
    assert result.mode == 'unreadable'
    assert result.point is None


def test_stat_multiplier_text_is_not_the_buy_quantity() -> None:
    import workshop_multiplier
    image, boxes = capture('menu_workshop_attack')
    assert any('x1.20' in b.text for b in boxes)
    assert workshop_multiplier.read(image, vision.TemplateCache(config.TEMPLATE_DIR), boxes).mode == 'absent'


def test_expanded_picker_selects_x1_without_category_heading_or_x1_ocr() -> None:
    import workshop_multiplier
    image, boxes = capture('expanded')
    # Live x1 OCR has only .889 confidence; its button pixels are unambiguous.
    boxes = tuple(b for b in boxes if b.text != 'x1')
    result = workshop_multiplier.read(image, vision.TemplateCache(config.TEMPLATE_DIR), boxes)
    assert result.mode == 'select_single'
    assert result.point == (829, 281)


def test_expanded_picker_requires_workshop_context() -> None:
    import workshop_multiplier
    image, boxes = capture('expanded')
    result = workshop_multiplier.read(image, vision.TemplateCache(config.TEMPLATE_DIR),
                                     tuple(b for b in boxes if b.text != 'WORKSHOP'))
    assert result.mode == 'unreadable'
    assert result.point is None
