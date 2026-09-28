"""The Events page, read from OCR boxes alone.

Recorded on a live Steampunk event (tests/fixtures/menu_events_*.png):

* the title is `EVENT - <NAME>`, above three tabs `Missions`, `Event Shop`
  and `Bots`, and the page is left by `Tap To Return To Game` along the bottom;
* the first visit of an event opens `EVENT INFORMATION` over the page, closed
  by an X right of its title;
* each mission card carries a tier counter, `1/3` (or `New! 1/3`), and that
  counter is what says the Missions list is the one on screen - the shop and
  bots tabs have none.

No claimable card has been recorded. Its control is read as an OCR `Claim`
label inside the list, which is how every other claim control in this game is
drawn, and the walk proves a tap by the page changing (the label gone or a
tier counter moved) - never by the tap alone.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

import ocr
from account_screens import ControlTarget
from device import Image
from geometry import supported_frame
from tiles import normalise

MIN_CONFIDENCE = .9
# Measured on menu_events_info: the X sits 84 px right of the title's box,
# on its centre line.
INFO_CLOSE_DX = 84
_COUNTER = re.compile(r'^(?:new!?\s*)?([0-3])\s*/\s*3$', re.I)
CLAIM_LABELS = {'claim'}


@dataclass(frozen=True)
class EventsReading:
    visible: bool = False
    error: str | None = None
    info_close: ControlTarget | None = None
    missions: bool = False
    # Every tier counter on the list, top to bottom: a claim that landed
    # moves one of them.
    counters: tuple[str, ...] = ()
    claims: tuple[ControlTarget, ...] = ()
    back: ControlTarget | None = None
    missions_tab: ControlTarget | None = None
    # The list's text, so a scroll that moved nothing can be told apart.
    fingerprint: tuple[str, ...] = ()


def _target(name: str, box: ocr.TextBox) -> ControlTarget:
    r = box.rect
    return ControlTarget(name, (r.x + r.w // 2, r.y + r.h // 2), 'located',
                         box.confidence, (r.x, r.y, r.w, r.h))


def parse(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> EventsReading:
    height, width = screen.shape[:2]
    if not supported_frame(width, height):
        return EventsReading(error='events_unsupported_frame')
    sure = [b for b in boxes if b.confidence >= MIN_CONFIDENCE]
    title = [b for b in sure if b.rect.y < 200 and normalise(b.text).startswith('event')
             and len(normalise(b.text)) > len('event')]
    tabs = {normalise(b.text) for b in sure if 200 <= b.rect.y < 400}
    if not title or not {'missions', 'eventshop', 'bots'} <= tabs:
        return EventsReading()
    returns = [b for b in sure if normalise(b.text) == 'taptoreturntogame']
    back = _target('events_return', returns[0]) if len(returns) == 1 else None
    tab = [b for b in sure if 200 <= b.rect.y < 400 and normalise(b.text) == 'missions']
    missions_tab = _target('events_missions_tab', tab[0]) if len(tab) == 1 else None
    list_top = max(b.rect.y + b.rect.h for b in tab)
    list_bottom = returns[0].rect.y if returns else height
    info = [b for b in sure if normalise(b.text) == 'eventinformation']
    if info:
        r = info[0].rect
        close = ControlTarget('events_info_close', (r.x + r.w + INFO_CLOSE_DX, r.y + r.h // 2),
                              'located', info[0].confidence, None) if len(info) == 1 else None
        return EventsReading(visible=True, info_close=close, back=back)
    listed = sorted((b for b in sure if list_top < b.rect.y < list_bottom),
                    key=lambda b: (b.rect.y, b.rect.x))
    counters = tuple(m.group(1) for b in listed if (m := _COUNTER.match(b.text.strip())))
    claims = tuple(_target('events_claim', b) for b in listed
                   if normalise(b.text) in CLAIM_LABELS)
    return EventsReading(visible=True, missions=bool(counters), counters=counters,
                         claims=claims, back=back, missions_tab=missions_tab,
                         fingerprint=tuple(normalise(b.text) for b in listed))


def scan(screen: Image) -> EventsReading:
    try:
        return parse(screen, ocr.read(screen, strict=True))
    except Exception:
        return EventsReading(error='events_unreadable')
