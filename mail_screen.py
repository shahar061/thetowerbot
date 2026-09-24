"""Conservative semantic mail reader; no mailbox-row geometry is assumed.

Only explicit MAIL/INBOX headings and outlined CLAIM/CLAIM ALL controls are
supported. Current tests describe synthetic UI contracts, not live captures.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

import cv2
import numpy as np

import ocr
from account_screens import ControlTarget
from device import Image
from geometry import supported_frame
from tiles import normalise
from milestones_badge import red_pixels


@dataclass(frozen=True)
class NewsEntry:
    title: str
    control: ControlTarget


@dataclass(frozen=True)
class MailReading:
    visible: bool = False
    error: str | None = None
    claim: ControlTarget | None = None
    back: ControlTarget | None = None
    unclaimed: int | None = None
    confirmed: bool = False
    coins: int | None = None
    gems: int | None = None
    news_tab: ControlTarget | None = None
    news_badge: bool = False
    news: tuple[NewsEntry, ...] = ()
    news_detail_title: str | None = None
    mail: tuple[NewsEntry, ...] = ()
    selected_tab: str | None = None


def _button(screen: Image, boxes: tuple[ocr.TextBox, ...],
            labels: set[str], name: str) -> ControlTarget | None:
    matches = [b for b in boxes if b.confidence >= .95 and normalise(b.text) in labels]
    if len(matches) != 1:
        return None
    label = matches[0]
    x, y, w, h = label.rect.x, label.rect.y, label.rect.w, label.rect.h
    edges = cv2.Canny(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY), 80, 180)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    enclosed = []
    for contour in contours:
        bx, by, bw, bh = cv2.boundingRect(contour)
        if (80 <= bw <= 700 and 45 <= bh <= 180
                and bx + 5 < x and by + 5 < y
                and x + w < bx + bw - 5 and y + h < by + bh - 5):
            other = [b for b in boxes if b is not label
                     and bx < b.rect.x + b.rect.w / 2 < bx + bw
                     and by < b.rect.y + b.rect.h / 2 < by + bh]
            if not other:
                enclosed.append((bx, by, bw, bh))
    if not enclosed:
        return None
    # Inner/outer border contours enclose the same label. Tap the label's
    # own centre, never an enclosing card or inferred message-row centre.
    return ControlTarget(name, (x + w // 2, y + h // 2), 'located',
                         label.confidence, (x, y, w, h))


def parse(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> MailReading:
    height, width = screen.shape[:2]
    if not supported_frame(width, height):
        return MailReading(error='unsupported_geometry')
    titles = [b for b in boxes if normalise(b.text) == 'inbox'
              and b.confidence >= .95 and b.rect.y < height * .4]
    if not titles:
        titles = [b for b in boxes if normalise(b.text) == 'mail'
                  and b.confidence >= .95 and b.rect.y < height * .4]
    if len(titles) != 1:
        return MailReading()
    back = _button(screen, boxes, {'return', 'back', 'close'}, 'mail_return')
    # Measured on the captured native inbox: a full-width footer, not an
    # outlined button. Anchor to its exact caption on this frame only.
    footers = [b for b in boxes if b.confidence >= .95
               and normalise(b.text) == 'taptoreturntogame'
               and b.rect.y > height * .85]
    if len(footers) == 1:
        r = footers[0].rect
        back = ControlTarget('mail_return', (r.x + r.w // 2, r.y + r.h // 2),
                             'located', footers[0].confidence, (r.x, r.y, r.w, r.h))
    blocked = any(re.search(r'\$|watch\s*(?:an?\s*)?ad|purchase|buy\s+now', b.text, re.I)
                  for b in boxes)
    claim = None if blocked else _button(screen, boxes, {'claim', 'claimall'}, 'mail_claim')
    claim_all = claim is not None and any(normalise(b.text) == 'claimall' for b in boxes)
    counts = [int(m[1]) for b in boxes if b.confidence >= .95
              and (m := re.fullmatch(r'Unclaimed\s*:?\s*(\d+)', b.text, re.I))]
    amounts: dict[str, list[int]] = {'coins': [], 'gems': []}
    reward_headings = [b for b in boxes if normalise(b.text) in {'reward', 'rewards'}
                       and b.confidence >= .95]
    for box in boxes:
        match = re.fullmatch(r'(\d+)\s+(coins|gems)', box.text.strip(), re.I)
        if (match and box.confidence >= .95 and len(reward_headings) == 1
                and 0 < box.rect.y - reward_headings[0].rect.y < 240):
            amounts[match[2].lower()].append(int(match[1]))
    confirmed = any(normalise(b.text) in {'rewardclaimed', 'rewardsclaimed', 'allrewardsclaimed'}
                    and b.confidence >= .95 for b in boxes)
    news_tab, news_badge = None, False
    tabs = [b for b in boxes if b.confidence >= .95 and normalise(b.text) == 'news'
            and b.rect.y < height * .2]
    mail_tabs = [b for b in boxes if b.confidence >= .95 and normalise(b.text) == 'mail'
                 and b.rect.y < height * .2]
    news: list[NewsEntry] = []
    mail: list[NewsEntry] = []
    selected_tab = None
    detail_title = None
    if len(tabs) == 1 and len(mail_tabs) == 1 and back is not None:
        r = tabs[0].rect
        news_tab = ControlTarget('news_tab', (r.x + r.w // 2, r.y + r.h // 2),
                                 'located', tabs[0].confidence, (r.x, r.y, r.w, r.h))
        patch = screen[max(0, r.y - 35):r.y + r.h, r.x:width]
        news_badge = red_pixels(patch) > 30
    if len(mail_tabs) == 1 and back is not None:
        # Captured INBOX uses two equal-width tab panes; selected pane is
        # brighter purple. Read the background below the observed caption,
        # avoiding glyphs/cursor. This is state evidence, never a tap point.
        r = mail_tabs[0].rect
        # The cursor can obscure the News caption, as in our captured list.
        # Its notification still lies in the measured right-hand tab pane.
        news_badge = red_pixels(screen[max(0, r.y - 35):r.y + r.h, width // 2:width]) > 30
        y = r.y + r.h + 7
        left = screen[y:y + 5, int(width * .1):int(width * .4)]
        right = screen[y:y + 5, int(width * .6):int(width * .9)]
        if left.size and right.size:
            difference = float(np.median(right.max(axis=2)) - np.median(left.max(axis=2)))
            selected_tab = 'news' if difference > 25 else 'mail' if difference < -25 else None
        edges = cv2.Canny(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY), 80, 180)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if not (width * .75 < w < width and 80 < h < 240
                    and r.y + r.h < y < height * .8):
                continue
            content = [b for b in boxes if b.confidence >= .9
                       and x < b.rect.x + b.rect.w / 2 < x + w
                       and y < b.rect.y + b.rect.h / 2 < y + h
                       and not re.fullmatch(r'\d+(?:day|week|month|hour|minute)s?ago', normalise(b.text))
                       and normalise(b.text) != 'unread']
            if not content:
                continue
            content.sort(key=lambda b: (b.rect.y, b.rect.x))
            title = ' '.join(b.text for b in content)
            if re.search(r'https?://|www\.', title, re.I):
                continue
            first = content[0].rect
            entry = NewsEntry(title, ControlTarget(
                'inbox_item', (first.x + first.w // 2, first.y + first.h // 2),
                'located', min(b.confidence for b in content), (x, y, w, h)))
            if selected_tab == 'news':
                news.append(entry)
            elif selected_tab == 'mail':
                unread = any(normalise(b.text) == 'unread' and b.confidence >= .95
                             and y < b.rect.y < y + h for b in boxes)
                marker = red_pixels(screen[y:y + h, x:x + min(100, w)]) > 30
                if unread or marker:
                    mail.append(entry)
        news.sort(key=lambda item: item.control.rect[1])
        mail.sort(key=lambda item: item.control.rect[1])
    elif back is not None and not mail_tabs:
        # Captured News detail has the item's one-line title immediately
        # below INBOX, with its date to the right. Never click that text.
        heading = titles[0].rect
        candidates = [b for b in boxes if b.confidence >= .9
                      and heading.y + heading.h < b.rect.y < heading.y + heading.h + 130
                      and b.rect.x < width * .7
                      and not re.fullmatch(r'[\d/.-]+', b.text)]
        if len(candidates) == 1:
            detail_title = candidates[0].text
    return MailReading(True, claim=claim, back=back,
                       unclaimed=counts[0] if len(counts) == 1 else None,
                       confirmed=confirmed,
                       # Claim All can include hidden/unknown rewards. A
                       # single visible amount is not proof of its total.
                       coins=amounts['coins'][0] if not claim_all and len(amounts['coins']) == 1 else None,
                       gems=amounts['gems'][0] if not claim_all and len(amounts['gems']) == 1 else None,
                       news_tab=news_tab, news_badge=news_badge,
                       news=tuple(news), news_detail_title=detail_title,
                       mail=tuple(mail), selected_tab=selected_tab)


def scan(screen: Image) -> MailReading:
    try:
        return parse(screen, ocr.read(screen, strict=True))
    except Exception:
        return MailReading(error='mail_unreadable')


def news_scroll_points(screen: Image, page: MailReading) -> tuple[int, int, int, int] | None:
    """Swipe only within the current recognized News-card stack.

    The coordinates are derived afresh from outlined rows, clear of tabs and
    the return footer. A detail page or an uncertain tab has no scroll target.
    """
    height, width = screen.shape[:2]
    if (not supported_frame(width, height) or page.error or not page.visible
            or page.selected_tab != 'news' or not page.news or page.back is None):
        return None
    rects = [entry.control.rect for entry in page.news]
    if any(rect is None for rect in rects):
        return None
    left = max(rect[0] for rect in rects)
    right = min(rect[0] + rect[2] for rect in rects)
    top = min(rect[1] for rect in rects) + 20
    bottom = max(rect[1] + rect[3] for rect in rects) - 20
    if page.back.rect is not None:
        bottom = min(bottom, page.back.rect[1] - 30)
    if not (0 <= left < right <= width and 0 <= top < bottom < height
            and bottom - top >= 100):
        return None
    x = left + (right - left) // 5
    return x, bottom, x, top
