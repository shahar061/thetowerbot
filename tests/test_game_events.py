"""The in-game Events page and the tier arrow, against a live account's captures.

menu_main_events_badge_tier_next: Tier 1 at best wave 126, the right arrow lit
and the Events dot on. menu_main_events_badge_tier2_top: one tap later, Tier 2
with the right arrow dim. menu_events_*: the Events page itself, with nothing
claimable - no claimable card has been recorded, so the claim path is driven
by a `Claim` label added to the recorded boxes.
"""
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

import config
import events
import events_badge
import events_claim
import events_screen
import ocr
import tier_select
from tests.test_missions_claim import FakeBus, FakeDevice, FakePanel, FakeTemplates, image

FIXTURES = Path(__file__).parent / 'fixtures'


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / 'ocr' / f'{name}.json').read_text()))


def test_lit_tier_arrow_is_available_and_dim_one_is_not() -> None:
    templates = FakeTemplates()
    lit = tier_select.read_next(image('menu_main_events_badge_tier_next'), templates)
    assert lit is not None and lit.available and lit.point == (689, 1320)
    for name in ('menu_main_events_badge_tier2_top', 'menu_main', 'main_menu_resume',
                 'menu_main_bluestacks_1920'):
        arrow = tier_select.read_next(image(name), templates)
        assert arrow is not None and not arrow.available, name


def test_tier_arrow_off_the_main_menu_is_unknown() -> None:
    assert tier_select.read_next(image('menu_events_missions'), FakeTemplates()) is None


def test_events_dot_is_read_beside_its_own_icon() -> None:
    templates = FakeTemplates()
    assert events_badge.badge_visible(image('menu_main_events_badge_tier_next'), templates) is True
    assert events_badge.badge_visible(image('menu_main_events_badge_tier2_top'), templates) is True
    # A padlocked or undotted icon, including the 1920-tall layout.
    for name in ('menu_main', 'main_menu_resume', 'menu_main_bluestacks_1920',
                 'menu_main_labs_unlocked'):
        assert events_badge.badge_visible(image(name), templates) is False, name
    assert events_badge.badge_visible(image('menu_events_missions'), templates) is None


def test_reader_names_the_info_modal_list_and_shop() -> None:
    info = events_screen.parse(image('menu_events_info'), recorded('menu_events_info'))
    assert info.visible and info.info_close is not None
    assert info.info_close.point == (910, 492)
    missions = events_screen.parse(image('menu_events_missions'), recorded('menu_events_missions'))
    assert missions.visible and missions.missions and missions.info_close is None
    assert missions.counters == ('1', '1', '1', '1') and missions.claims == ()
    assert missions.back is not None and missions.back.point[1] > 2250
    shop = events_screen.parse(image('menu_events_shop'), recorded('menu_events_shop'))
    assert shop.visible and not shop.missions and shop.missions_tab is not None
    menu = events_screen.parse(image('menu_main_events_badge_tier_next'),
                               recorded('menu_main_events_badge_tier_next'))
    assert not menu.visible


def claimable(counter: str = '1/3', claim: bool = True) -> tuple[ocr.TextBox, ...]:
    boxes = []
    for box in recorded('menu_events_missions'):
        if box.text == 'Login for 7 days':
            boxes.append(box)
            if claim:
                boxes.append(ocr.TextBox('CLAIM', .99, config.Rect(460, 1415, 160, 50)))
        elif box.text == 'New!1/3' and box.rect.y == 1282:
            boxes.append(replace(box, text=counter))
        elif box.text != '1/7':
            boxes.append(box)
    return tuple(boxes)


class Walk:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.walk = events_claim.EventsClaim(frame_budget=1)
        self.bus, self.device = FakeBus(), FakeDevice()
        self.pages: list[events_screen.EventsReading] = []
        monkeypatch.setattr(events_claim.events_screen, 'scan', lambda _: self.pages.pop(0))
        assert self.walk.request(now=0)

    def step(self, name: str, page: events_screen.EventsReading | None = None,
             state: str = 'MAIN_MENU') -> Any:
        if page is not None:
            self.pages.append(page)
        return self.walk.advance(screen=image(name), device=self.device,
                                 templates=FakeTemplates(), readings=FakePanel(),
                                 state=state, bus=self.bus, now=1)


def parsed(name: str, boxes: tuple[ocr.TextBox, ...] | None = None) -> events_screen.EventsReading:
    return events_screen.parse(image(name), recorded(name) if boxes is None else boxes)


def test_walk_opens_closes_info_claims_proves_and_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Walk(monkeypatch)
    home = events_screen.EventsReading()
    assert run.step('menu_main_events_badge_tier_next').name == 'events_open'
    assert run.step('menu_events_info', parsed('menu_events_info')).name == 'events_info_close'
    page = parsed('menu_events_missions', claimable())
    assert run.step('menu_events_missions', page).name == 'events_claim'
    assert run.device.taps[-1] == (540, 1440)
    after = parsed('menu_events_missions', claimable(counter='2/3', claim=False))
    assert run.step('menu_events_missions', after).name == 'events_scroll'
    # The list did not move: the end of it. Leave by the footer.
    assert run.step('menu_events_missions', after) is None
    assert run.step('menu_events_missions', after).name == 'events_return'
    assert run.step('menu_main_events_badge_tier_next', home) is None
    assert not run.walk.active
    assert run.walk.snapshot()['result']['status'] == 'completed'
    assert [e.confirmation for e in run.bus.of(events.EventMissionClaimed)] == ['claim_label_gone']
    assert run.bus.of(events.ClaimEnded)[0].claimed == 1


def test_unproven_claim_is_reported_uncertain_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Walk(monkeypatch)
    run.step('menu_main_events_badge_tier_next')
    page = parsed('menu_events_missions', claimable())
    run.step('menu_events_missions', page)
    assert run.step('menu_events_missions', page) is None
    assert run.step('menu_events_missions', page).name == 'events_return'
    assert len(run.bus.of(events.ClaimUncertain)) == 1
    assert run.bus.of(events.EventMissionClaimed) == []
    assert [t for t in run.device.taps if t == (540, 1440)] == [(540, 1440)]


def test_nothing_claimable_scrolls_then_returns_as_an_ordinary_outcome(
        monkeypatch: pytest.MonkeyPatch) -> None:
    run = Walk(monkeypatch)
    run.step('menu_main_events_badge_tier_next')
    page = parsed('menu_events_missions')
    assert run.step('menu_events_missions', page).name == 'events_scroll'
    assert run.device.swipes == [(540, 2000, 540, 1300, .35)]
    run.step('menu_events_missions', page)
    assert run.step('menu_events_missions', page).name == 'events_return'
    run.step('menu_main_events_badge_tier_next', events_screen.EventsReading())
    assert run.walk.snapshot()['result']['reason'] == 'no_claimable_event_mission'
    assert run.bus.of(events.ClaimSkipped)[0].target == 'events'


def test_walk_never_leaves_an_unconfirmed_home(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Walk(monkeypatch)
    assert run.step('menu_main_events_badge_tier_next', state='UNKNOWN') is None
    assert run.device.taps == []
