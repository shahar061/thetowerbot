"""Synthetic mail UI contracts; these are NOT captured/live layout fixtures."""
from typing import Any

import cv2
import numpy as np
import pytest

import config
import events
import mail_claim
import mail_screen
import ocr
from account_screens import ControlTarget
from tests.test_missions_claim import FakeBus, FakeDevice, FakeTemplates, FakePanel, image


def mail_page(*, label: str = 'CLAIM ALL', count: int = 2, claimed: bool = False,
              duplicate: bool = False) -> tuple[Any, tuple[ocr.TextBox, ...]]:
    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    boxes = [ocr.TextBox('MAIL', .99, config.Rect(420, 230, 140, 40)),
             ocr.TextBox(f'Unclaimed: {count}', .99, config.Rect(300, 350, 200, 40)),
             ocr.TextBox('Rewards', .99, config.Rect(400, 480, 140, 40)),
             ocr.TextBox('50 GEMS', .99, config.Rect(450, 550, 140, 40))]
    if claimed:
        boxes.append(ocr.TextBox('Rewards Claimed', .99, config.Rect(360, 700, 300, 40)))
    else:
        cv2.rectangle(frame, (350, 850), (730, 960), (240, 240, 240), 4)
        boxes.append(ocr.TextBox(label, .99, config.Rect(430, 880, 220, 40)))
        if duplicate:
            cv2.rectangle(frame, (350, 1050), (730, 1160), (240, 240, 240), 4)
            boxes.append(ocr.TextBox(label, .99, config.Rect(430, 1080, 220, 40)))
    cv2.rectangle(frame, (350, 1650), (730, 1760), (240, 240, 240), 4)
    boxes.append(ocr.TextBox('RETURN', .99, config.Rect(430, 1680, 220, 40)))
    return frame, tuple(boxes)


def test_reader_requires_unique_visible_claim_button_and_mail_heading() -> None:
    frame, boxes = mail_page(label='CLAIM')
    result = mail_screen.parse(frame, boxes)
    assert result.claim is not None
    assert result.gems == 50
    assert result.unclaimed == 2
    assert mail_screen.parse(frame, boxes[1:]).claim is None
    frame, boxes = mail_page(duplicate=True)
    assert mail_screen.parse(frame, boxes).claim is None


@pytest.mark.parametrize('label', ['DELETE ALL', 'BUY', 'WATCH AD', 'READ', 'https://example.com'])
def test_reader_never_exposes_unapproved_controls(label: str) -> None:
    frame, boxes = mail_page(label=label)
    assert mail_screen.parse(frame, boxes).claim is None


def test_plain_body_text_claim_is_not_a_button() -> None:
    frame, boxes = mail_page()
    frame[800:1200] = 0
    assert mail_screen.parse(frame, boxes).claim is None


def test_walk_confirms_reward_then_returns_and_journals(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = mail_claim.MailClaim(frame_budget=2)
    bus, device, templates = FakeBus(), FakeDevice(), FakeTemplates()
    assert walk.request(now=0)
    monkeypatch.setattr(mail_claim.menu_badges, 'read_badge', lambda *a, **k: type('Badge', (), {'control': ControlTarget('mail', (1022, 524), 'located', 1, (998, 503, 48, 42))})())
    home = mail_screen.MailReading()
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: home)
    def advance(frame: Any, state: str = 'MAIN_MENU') -> Any:
        return walk.advance(screen=frame, device=device, templates=templates,
                            readings=FakePanel(), state=state, bus=bus, now=1)
    advance(image('menu_main_bluestacks_1920'))
    frame, boxes = mail_page(label='CLAIM')
    reading = mail_screen.parse(frame, boxes)
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: reading)
    advance(frame)
    assert len(device.taps) == 2
    assert not bus.of(events.MailClaimed)
    frame, boxes = mail_page(count=0, claimed=True)
    reading = mail_screen.parse(frame, boxes)
    advance(frame)
    assert len(bus.of(events.MailClaimed)) == 1
    assert bus.of(events.MailClaimed)[0].gems == 50
    assert len(device.taps) == 3
    reading = home
    advance(image('menu_main_bluestacks_1920'))
    assert not walk.active
    assert walk.snapshot()['result']['status'] == 'completed'


def test_unchanged_claim_is_never_retried_and_budget_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = mail_claim.MailClaim(frame_budget=1)
    walk.request(now=0)
    walk._step = mail_claim.Step.READ
    bus, device = FakeBus(), FakeDevice()
    frame, boxes = mail_page()
    reading = mail_screen.parse(frame, boxes)
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: reading)
    for i in range(5):
        walk.advance(screen=frame, device=device, templates=FakeTemplates(),
                     readings=FakePanel(), state='MAIN_MENU', bus=bus, now=i)
    assert len(device.taps) == 2  # one claim, one return; no repeat claim
    assert not bus.of(events.MailClaimed)
    assert len(bus.of(events.ClaimUncertain)) == 1


def test_mail_success_event_is_replayable_in_reward_ledger() -> None:
    import ledger
    event = events.MailClaimed(gems=50, confirmation='unclaimed_count_decreased')
    lines = ledger.classify(event)
    assert len(lines) == 2
    assert lines[1].kind == 'MAIL_CLAIM'
    assert lines[1].delta == 50
    assert ledger._REPLAYABLE['MailClaimed'] is events.MailClaimed


def captured_mail(name: str) -> tuple[Any, tuple[ocr.TextBox, ...]]:
    from pathlib import Path
    source = cv2.imread(str(Path(__file__).parent / 'fixtures' / name))
    assert source is not None
    # Captured CUA window; this known image's game viewport is 47:424,32:869.
    frame = cv2.resize(source[32:869, 47:424], (1080, 2400))
    return frame, ocr.read(frame, strict=True)


def test_captured_empty_inbox_has_safe_exit_and_numbered_badge_is_not_reward_proof() -> None:
    frame, boxes = captured_mail('menu_mail_empty_cua.png')
    result = mail_screen.parse(frame, boxes)
    assert result.visible
    assert result.claim is None
    assert result.back is not None
    assert result.news_tab is not None
    assert result.news_badge


def test_live_full_resolution_empty_inbox_exposes_unread_news() -> None:
    from pathlib import Path
    frame = cv2.imread(str(Path(__file__).parent / 'fixtures' / 'menu_mail_empty_live_39.jpg'))
    assert frame is not None
    result = mail_screen.parse(frame, ocr.read(frame, strict=True))
    assert result.visible and result.back is not None
    assert result.selected_tab == 'mail'
    assert result.news_tab is not None and result.news_badge
    assert result.claim is None


def test_live_empty_mail_opens_news_before_returning_home() -> None:
    from pathlib import Path
    frame = cv2.imread(str(Path(__file__).parent / 'fixtures' / 'menu_mail_empty_live_39.jpg'))
    assert frame is not None
    walk = mail_claim.MailClaim()
    walk.request(now=0)
    walk._step = mail_claim.Step.READ
    device = FakeDevice()
    walk.advance(screen=frame, device=device, templates=FakeTemplates(),
                 readings=FakePanel(), state='UNKNOWN', bus=FakeBus(), now=1)
    assert device.taps == [mail_screen.scan(frame).news_tab.point]
    assert walk._step is mail_claim.Step.NEWS_LIST


def test_captured_news_list_has_four_unique_entries_and_no_reward_claims() -> None:
    frame, boxes = captured_mail('menu_news_list_cua.png')
    result = mail_screen.parse(frame, boxes)
    assert result.visible
    assert result.claim is None
    assert len(result.news) == 4
    assert len({item.title for item in result.news}) == 4
    assert result.news_badge


def test_captured_news_detail_is_information_not_claim_success() -> None:
    frame, boxes = captured_mail('menu_news_detail_cua.png')
    result = mail_screen.parse(frame, boxes)
    assert result.visible
    assert result.claim is None
    assert not result.confirmed
    assert result.news_detail_title is not None
    assert result.back is not None


def test_claim_all_never_calls_one_visible_amount_the_total() -> None:
    frame, boxes = mail_page(label='CLAIM ALL')
    result = mail_screen.parse(frame, boxes)
    assert result.claim is not None
    assert result.gems is None


def test_news_walk_waits_for_tab_then_reads_each_title_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    walk = mail_claim.MailClaim(frame_budget=2)
    walk.request(now=0)
    walk._step = mail_claim.Step.READ
    bus, device = FakeBus(), FakeDevice()
    frame = image('menu_main_bluestacks_1920')
    back = ControlTarget('return', (500, 1800), 'located', 1, (300, 1760, 400, 80))
    tab = ControlTarget('news_tab', (800, 230), 'located', 1, (740, 200, 120, 60))
    rows = tuple(mail_screen.NewsEntry(f'Update {i}', ControlTarget('news_item', (500, 500 + i * 200), 'located', 1, (40, 400 + i * 200, 1000, 180))) for i in range(2))
    inbox = mail_screen.MailReading(visible=True, back=back, news_tab=tab, news_badge=True, selected_tab='mail')
    listing = replace(inbox, selected_tab='news', news=rows)
    current = inbox
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: current)
    monkeypatch.setattr(mail_claim.menu_badges, 'read_badge', lambda *a: type('Badge', (), {'control': tab})())
    def step() -> None:
        walk.advance(screen=frame, device=device, templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=bus, now=1)
    step()  # News tab
    step()  # dropped/lagging tap: still Mail tab
    assert len(device.taps) == 1
    current = listing
    step()  # Update0
    assert len(device.taps) == 2
    current = mail_screen.MailReading(visible=True, back=back, news_detail_title='Update 0')
    step()  # verify read; return
    assert [event.title for event in bus.of(events.NewsRead)] == ['Update 0']
    current = mail_screen.MailReading()
    step()  # confirm home; reopen
    step()  # envelope
    current = inbox
    step()  # News tab
    current = listing
    step()  # only unseen Update1
    assert device.taps[-1] == rows[1].control.point
    current = mail_screen.MailReading(visible=True, back=back, news_detail_title='Update 1')
    step()
    assert [event.title for event in bus.of(events.NewsRead)] == ['Update 0', 'Update 1']
    assert not bus.of(events.MailClaimed)
    assert not bus.of(events.MissionClaimed)


def test_synthetic_unread_mail_row_requires_selected_mail_tab_and_marker() -> None:
    from dataclasses import replace
    frame, boxes = mail_page()
    frame[:] = 0
    frame[180:280, :540] = (170, 80, 90)
    frame[180:280, 540:] = (108, 49, 53)
    cv2.rectangle(frame, (30, 400), (1050, 580), (255, 255, 255), 4)
    cv2.circle(frame, (60, 430), 12, (0, 0, 255), -1)
    boxes = (ocr.TextBox('INBOX', .99, config.Rect(30, 100, 170, 45)),
             ocr.TextBox('Mail', .99, config.Rect(210, 200, 120, 50)),
             ocr.TextBox('News', .99, config.Rect(750, 200, 120, 50)),
             ocr.TextBox('A free gift', .99, config.Rect(130, 450, 400, 45)),
             ocr.TextBox('Tap To Return To Game', .99, config.Rect(250, 1800, 600, 60)))
    result = mail_screen.parse(frame, boxes)
    assert result.selected_tab == 'mail'
    assert len(result.mail) == 1
    assert not result.news
    cv2.circle(frame, (60, 430), 13, (0, 0, 0), -1)
    assert not mail_screen.parse(frame, boxes).mail


def test_synthetic_mail_detail_must_match_selected_title_before_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = mail_claim.MailClaim(frame_budget=1)
    walk.request(now=0)
    walk._step = mail_claim.Step.READ
    bus, device = FakeBus(), FakeDevice()
    frame, boxes = mail_page(label='CLAIM')
    detail = mail_screen.parse(frame, boxes)
    entry = mail_screen.NewsEntry('A gift', ControlTarget('mail_item', (500, 450), 'located', 1, (30, 400, 1000, 180)))
    current = mail_screen.MailReading(visible=True, back=detail.back, selected_tab='mail', mail=(entry,))
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: current)
    def step() -> None:
        walk.advance(screen=frame, device=device, templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=bus, now=1)
    step()
    assert device.taps == [entry.control.point]
    current = detail  # Claim present but no matching detail title
    step()
    assert len(device.taps) == 1
    from dataclasses import replace
    current = replace(detail, news_detail_title='A gift')
    step()
    assert len(device.taps) == 2
    assert not bus.of(events.MailClaimed)


def test_new_visit_can_open_a_new_unread_mail_with_the_same_subject() -> None:
    walk = mail_claim.MailClaim()
    walk.request(now=0)
    walk._seen_mail.add('dailyreward')
    walk._seen_news.add('patchnotes')
    walk.cancel('test', 'End first visit.', now=1)
    assert walk.request(now=3601)
    assert 'dailyreward' not in walk._seen_mail
    assert 'patchnotes' in walk._seen_news


class NewsSwipeDevice(FakeDevice):
    def __init__(self) -> None:
        super().__init__()
        self.swipes: list[tuple[int, int, int, int, float]] = []

    def swipe(self, x: int, y: int, x2: int, y2: int, duration: float) -> None:
        self.swipes.append((x, y, x2, y2, duration))


def pagination_page(*titles: str) -> mail_screen.MailReading:
    rows = tuple(mail_screen.NewsEntry(title, ControlTarget(
        'inbox_item', (400, 500 + index * 200), 'located', 1,
        (30, 410 + index * 200, 1000, 180),
    )) for index, title in enumerate(titles))
    return mail_screen.MailReading(
        visible=True, selected_tab='news', news_badge=True, news=rows,
        back=ControlTarget('mail_return', (500, 1800), 'located', 1, (300, 1760, 400, 80)),
    )


def test_news_scroll_reaches_unread_second_page_and_never_journals_a_reward(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    walk = mail_claim.MailClaim(frame_budget=2)
    walk.request(now=0)
    walk._step = mail_claim.Step.NEWS_LIST
    walk._news_mode = True
    walk._seen_news.update({'first', 'second'})
    current = pagination_page('First', 'Second')
    first_page = current
    tab = ControlTarget('news_tab', (800, 230), 'located', 1, (740, 200, 120, 60))
    inbox = replace(first_page, selected_tab='mail', news=(), news_tab=tab)
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: current)
    monkeypatch.setattr(mail_claim.menu_badges, 'read_badge', lambda *a: type('Badge', (), {'control': tab})())
    bus, device = FakeBus(), NewsSwipeDevice()
    def step() -> None:
        walk.advance(screen=image('menu_main_bluestacks_1920'), device=device,
                     templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=bus, now=1)
    step()
    assert len(device.swipes) == 1
    assert not device.taps
    current = pagination_page('Third', 'Fourth')
    step()
    assert device.taps == [current.news[0].control.point]
    current = mail_screen.MailReading(visible=True, back=current.back, news_detail_title='Third')
    step()
    assert [event.title for event in bus.of(events.NewsRead)] == ['Third']
    current = mail_screen.MailReading()
    step()  # home confirmed, pagination history resets for the next opening
    step()  # reopen inbox
    current = inbox
    step()  # News tab
    current = first_page
    step()  # scroll past the same already-read first page again
    assert len(device.swipes) == 2
    current = pagination_page('Third', 'Fourth')
    step()
    assert device.taps[-1] == current.news[1].control.point
    current = mail_screen.MailReading(visible=True, back=current.back, news_detail_title='Fourth')
    step()
    assert [event.title for event in bus.of(events.NewsRead)] == ['Third', 'Fourth']
    assert not bus.of(events.MailClaimed)
    assert not bus.of(events.MissionClaimed)


def test_unchanged_or_missed_news_swipe_exits_without_repeated_swipes(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = mail_claim.MailClaim(frame_budget=2)
    walk.request(now=0)
    walk._step = mail_claim.Step.NEWS_LIST
    walk._news_mode = True
    walk._seen_news.add('alreadyread')
    current = pagination_page('Already read')
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: current)
    bus, device = FakeBus(), NewsSwipeDevice()
    for i in range(5):
        walk.advance(screen=image('menu_main_bluestacks_1920'), device=device,
                     templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=bus, now=i)
    assert len(device.swipes) == 1
    assert device.taps == [current.back.point]
    assert not walk._news_mode
    assert not bus.of(events.NewsRead)


def test_new_visit_can_scroll_past_already_seen_first_page(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = mail_claim.MailClaim(frame_budget=1)
    walk._seen_news.add('firstpage')
    walk.request(now=3600)
    walk._step = mail_claim.Step.NEWS_LIST
    walk._news_mode = True
    current = pagination_page('First page')
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: current)
    device = NewsSwipeDevice()
    walk.advance(screen=image('menu_main_bluestacks_1920'), device=device,
                 templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=FakeBus(), now=3601)
    assert len(device.swipes) == 1


def test_news_list_on_wrong_tab_never_swipes(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    walk = mail_claim.MailClaim(frame_budget=1)
    walk.request(now=0)
    walk._step = mail_claim.Step.NEWS_LIST
    walk._seen_news.add('seen')
    current = replace(pagination_page('Seen'), selected_tab='mail')
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: current)
    device = NewsSwipeDevice()
    for i in range(3):
        walk.advance(screen=image('menu_main_bluestacks_1920'), device=device,
                     templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=FakeBus(), now=i)
    assert not device.swipes


def test_news_pagination_scroll_budget_returns_even_on_changing_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = mail_claim.MailClaim(frame_budget=1)
    walk.request(now=0)
    walk._step = mail_claim.Step.NEWS_LIST
    walk._news_mode = True
    walk._seen_news.update(f'page{i}' for i in range(mail_claim.MAX_NEWS_SCROLLS + 2))
    current = pagination_page('Page 0')
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: current)
    device, bus = NewsSwipeDevice(), FakeBus()
    for i in range(mail_claim.MAX_NEWS_SCROLLS + 1):
        current = pagination_page(f'Page {i}')
        walk.advance(screen=image('menu_main_bluestacks_1920'), device=device,
                     templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=bus, now=i)
    assert len(device.swipes) == mail_claim.MAX_NEWS_SCROLLS
    assert device.taps == [current.back.point]
    assert not walk._news_mode
    assert walk._reason == 'news_scroll_budget'
    current = mail_screen.MailReading()
    walk.advance(screen=image('menu_main_bluestacks_1920'), device=device,
                 templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=bus, now=20)
    assert not walk.active
    assert not bus.of(events.MailClaimed)


def test_visit_frame_bound_uses_visible_footer_before_finishing(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = mail_claim.MailClaim()
    walk.request(now=0)
    walk._step = mail_claim.Step.NEWS_LIST
    walk._frames = mail_claim.MAX_VISIT_FRAMES
    current = pagination_page('Unread')
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: current)
    device = NewsSwipeDevice()
    walk.advance(screen=image('menu_main_bluestacks_1920'), device=device,
                 templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=FakeBus(), now=1)
    assert device.taps == [current.back.point]
    assert not device.swipes
    assert walk._step is mail_claim.Step.CONFIRM_HOME


def test_unreadable_badge_after_return_never_reports_badge_clearance(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = mail_claim.MailClaim()
    walk.request(now=0)
    walk._news_mode = True
    monkeypatch.setattr(mail_claim.mail_screen, 'scan', lambda *a: mail_screen.MailReading())
    monkeypatch.setattr(mail_claim.menu_badges, 'read_badge', lambda *a: None)
    walk.advance(screen=image('menu_main_bluestacks_1920'), device=NewsSwipeDevice(),
                 templates=FakeTemplates(), readings=FakePanel(), state='MAIN_MENU', bus=FakeBus(), now=1)
    assert not walk.active
    assert walk.snapshot()['result']['reason'] == 'mail_badge_not_readable'
    assert 'not verified' in walk.snapshot()['result']['detail']
