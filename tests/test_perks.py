"""First-choice perk picking from a sanitized capture of the actual panel."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import config
import events
from ocr import TextBox

FIXTURES = Path(__file__).parent / 'fixtures' / 'perks'


def choice() -> tuple[np.ndarray, tuple[TextBox, ...]]:
    image = cv2.imread(str(FIXTURES / 'choice.png'))
    rows = json.loads((FIXTURES / 'choice.json').read_text())
    boxes = tuple(TextBox(b['text'], b['confidence'], config.Rect(*b['rect'])) for b in rows)
    return image, boxes


def regen_choice() -> tuple[np.ndarray, tuple[TextBox, ...]]:
    image = cv2.imread(str(FIXTURES / 'regen_choice.png'))
    rows = json.loads((FIXTURES / 'regen_choice.json').read_text())
    return image, tuple(TextBox(b['text'], b['confidence'], config.Rect(*b['rect'])) for b in rows)


def test_live_heading_with_joined_newperk_recognizes_the_first_offer() -> None:
    import perks
    image, boxes = regen_choice()
    assert any(b.text == 'Choose a NewPerk' for b in boxes)
    assert perks.panel_visible(image, boxes)
    result = perks.read_choice(image, boxes)
    assert result is not None
    assert result.first.label == 'x1.75 Health Regen'
    assert result.first.point == (540, 611)


def test_joined_newperk_button_still_opens_the_chooser() -> None:
    import perks
    image = cv2.imread(str(FIXTURES / 'joined_button.png'))
    rows = json.loads((FIXTURES / 'joined_button.json').read_text())
    boxes = tuple(TextBox(b['text'], b['confidence'], config.Rect(*b['rect'])) for b in rows)
    assert boxes[0].text == 'NewPerk'
    point = perks.read_button(image, boxes)
    assert point is not None
    assert 350 < point[0] < 750 and 20 < point[1] < 100


def summary() -> tuple[np.ndarray, tuple[TextBox, ...]]:
    image = cv2.imread(str(FIXTURES / 'summary.png'))
    rows = json.loads((FIXTURES / 'summary.json').read_text())
    return image, tuple(TextBox(b['text'], b['confidence'], config.Rect(*b['rect'])) for b in rows)


def test_after_pick_summary_is_verified_closed_and_returned_to_battle() -> None:
    import perks
    image, boxes = choice()
    after, after_boxes = summary()
    bus, device = Bus(), Device()
    picker = perks.PerkPicker(bus)
    policy = SimpleNamespace(auto_pick_perks=True)
    picker.observe(image, boxes, device, policy, now=0, in_run=False)
    picker.observe(after, after_boxes, device, policy, now=1, in_run=False)
    assert len(device.taps) == 2
    assert 900 < device.taps[-1][0] < 975
    assert 360 < device.taps[-1][1] < 440
    assert len([e for e in bus.published if isinstance(e, events.PerkPicked)]) == 1
    picker.observe(np.zeros_like(image), (), device, policy, now=2, in_run=True)
    assert not picker.active


def test_already_open_summary_closes_without_inventing_a_pick() -> None:
    import perks
    image, boxes = summary()
    bus, device = Bus(), Device()
    picker = perks.PerkPicker(bus)
    picker.observe(image, boxes, device, SimpleNamespace(auto_pick_perks=True), now=0, in_run=False)
    assert len(device.taps) == 1
    assert not any(isinstance(e, events.PerkPicked) for e in bus.published)


def test_selected_caption_split_into_boxes_still_confirms_and_closes() -> None:
    import perks
    image, boxes = choice()
    after, after_boxes = summary()
    split = []
    for box in after_boxes:
        if box.text == 'Black Hole duration +12.0s':
            split.extend((TextBox('Black Hole duration', .99, config.Rect(289, 795, 360, 37)),
                          TextBox('+12.0s', .99, config.Rect(655, 796, 130, 37))))
        else:
            split.append(box)
    bus, device = Bus(), Device()
    picker = perks.PerkPicker(bus)
    policy = SimpleNamespace(auto_pick_perks=True)
    picker.observe(image, boxes, device, policy, now=0, in_run=False)
    picker.observe(after, tuple(reversed(split)), device, policy, now=1, in_run=False)
    assert len(device.taps) == 2
    assert any(isinstance(e, events.PerkPicked) for e in bus.published)


def test_first_choice_is_top_bordered_offer_not_selected_perk() -> None:
    import perks
    image, boxes = choice()
    result = perks.read_choice(image, tuple(reversed(boxes)))
    assert result is not None
    assert result.first.label == 'Black Hole duration +12.0s'
    assert 516 < result.first.point[1] < 704
    assert len(result.offers) == 2


def test_missing_or_low_confidence_title_grants_no_choice() -> None:
    import perks
    image, boxes = choice()
    assert perks.read_choice(image, tuple(b for b in boxes if b.text != 'Choose a New Perk')) is None
    weak = tuple(TextBox(b.text, .3, b.rect) if b.text == 'Perks' else b for b in boxes)
    assert perks.read_choice(image, weak) is None


def test_missing_first_border_does_not_choose_second_offer() -> None:
    import perks
    image, boxes = choice()
    image[512:707, 105:976] = 0
    assert perks.read_choice(image, boxes) is None


def test_button_accepts_split_words_and_rejects_low_confidence() -> None:
    import perks
    image = np.zeros((2400, 1080, 3), np.uint8)
    image[25:100, 350:750] = (245, 45, 165)
    boxes = (TextBox('New', .99, config.Rect(450, 40, 85, 40)),
             TextBox('Perk', .99, config.Rect(540, 40, 95, 40)))
    assert perks.read_button(image, boxes) == (542, 60)
    assert perks.read_button(image, (TextBox('New Perk', .2, config.Rect(450, 40, 185, 40)),)) is None
    assert perks.read_button(np.zeros_like(image), boxes) is None


class Bus:
    def __init__(self) -> None:
        self.published: list[events.Event] = []

    def publish(self, event: events.Event) -> None:
        self.published.append(event)


class Device:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))


def test_picker_selects_once_then_confirms_return_to_battle() -> None:
    import perks
    image, boxes = choice()
    bus, device = Bus(), Device()
    picker = perks.PerkPicker(bus)
    policy = SimpleNamespace(auto_pick_perks=True)
    assert picker.observe(image, boxes, device, policy, now=0, in_run=False)
    assert len(device.taps) == 1
    assert picker.active
    # An unchanged modal is not permission to select twice.
    assert picker.observe(image, boxes, device, policy, now=1, in_run=False)
    assert len(device.taps) == 1
    assert picker.observe(np.zeros_like(image), (), device, policy, now=2, in_run=True)
    assert not picker.active
    assert any(isinstance(e, events.PerkPicked) for e in bus.published)


def test_picker_never_acts_disabled_and_cancel_stops_active_walk() -> None:
    import perks
    image, boxes = choice()
    bus, device = Bus(), Device()
    picker = perks.PerkPicker(bus)
    assert not picker.observe(image, boxes, device, SimpleNamespace(auto_pick_perks=False), now=0, in_run=False)
    assert device.taps == []
    picker.observe(image, boxes, device, SimpleNamespace(auto_pick_perks=True), now=1, in_run=False)
    picker.cancel('paused', 'Paused from the dashboard')
    assert not picker.active


def test_unconfirmed_identical_offer_is_not_tapped_again_after_timeout() -> None:
    import perks
    image, boxes = choice()
    bus, device = Bus(), Device()
    picker = perks.PerkPicker(bus)
    policy = SimpleNamespace(auto_pick_perks=True)
    picker.observe(image, boxes, device, policy, now=0, in_run=False)
    picker.observe(image, boxes, device, policy, now=30, in_run=False)
    picker.observe(image, boxes, device, policy, now=60, in_run=False)
    assert len(device.taps) == 1
    assert not any(isinstance(e, events.PerkPicked) for e in bus.published)


def test_unreadable_first_offer_never_falls_through_to_second() -> None:
    import perks
    image, boxes = choice()
    boxes = tuple(b for b in boxes if b.text != 'Black Hole duration +12.0s')
    assert perks.read_choice(image, boxes) is None


def test_more_earned_perks_select_first_from_each_fresh_offer() -> None:
    import perks
    image, boxes = choice()
    bus, device = Bus(), Device()
    picker = perks.PerkPicker(bus)
    policy = SimpleNamespace(auto_pick_perks=True)
    picker.observe(image, boxes, device, policy, now=0, in_run=False)
    updated = tuple(TextBox('x1.25 damage', b.confidence, b.rect)
                    if b.text == 'Black Hole duration +12.0s' else b for b in boxes)
    updated_image = image.copy()
    updated_image[540:680, 130:950] = (80, 40, 30)
    picker.observe(updated_image, updated, device, policy, now=1, in_run=False)
    assert len(device.taps) == 1  # confirm the previous choice on its own frame
    picker.observe(updated_image, updated, device, policy, now=2, in_run=False)
    picker.observe(updated_image, updated, device, policy, now=3, in_run=False)
    assert len(device.taps) == 2
    assert device.taps[0] == device.taps[1]


def test_ocr_variation_on_same_panel_does_not_confirm_or_reselect() -> None:
    import perks
    image, boxes = choice()
    bus, device = Bus(), Device()
    picker = perks.PerkPicker(bus)
    policy = SimpleNamespace(auto_pick_perks=True)
    picker.observe(image, boxes, device, policy, now=0, in_run=False)
    dropped = tuple(b for b in boxes if b.text != 'Golden tower bonus xl.5')
    dropped = tuple(TextBox(b.text, b.confidence, config.Rect(
        b.rect.x, b.rect.y + 2, b.rect.w, b.rect.h))
        if b.text == 'Choose a New Perk' else b for b in dropped)
    picker.observe(image, dropped, device, policy, now=1, in_run=False)
    picker.observe(image, dropped, device, policy, now=2, in_run=False)
    picker.observe(image, boxes, device, policy, now=3, in_run=False)
    # The cash HUD can classify IN_RUN underneath a panel whose title OCR failed.
    picker.observe(image, (), device, policy, now=4, in_run=True)
    assert len(device.taps) == 1
    assert not any(isinstance(e, events.PerkPicked) for e in bus.published)


def test_auto_pick_perks_roundtrip_opt_in_and_strict_validation() -> None:
    from strategy import ActionRule, ControlError, Strategy
    from strategy_compat import capture_legacy
    strategy = Strategy(name='test', actions=(ActionRule('Damage', 'upgrade_damage.png'),))
    assert strategy.to_dict().get('auto_pick_perks') is False
    enabled = strategy.merged({'auto_pick_perks': True})
    assert enabled.auto_pick_perks is True
    assert Strategy.from_dict(enabled.to_dict()) == enabled
    assert capture_legacy(enabled, 'test')['strategy']['auto_pick_perks'] is True
    with pytest.raises(ControlError):
        strategy.merged({'auto_pick_perks': 'yes'})
