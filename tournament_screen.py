"""Tournament readings qualified against recorded 1080x2400 screens.

Parsers do not perform OCR or touch a device. Unsupported layouts and unknown
modal shapes prohibit entry rather than guessing coordinates.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from account_screens import ControlTarget
from config import Rect
from device import Image
from ocr import TextBox


@dataclass(frozen=True)
class TournamentPage:
    tickets: int | None
    league: str | None
    join_time_left_s: int | None
    tournament_id: str | None
    battle: ControlTarget | None
    return_to_game: ControlTarget | None
    entry_is_ticket: bool = False
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class TournamentStats:
    league: str | None
    wave: int | None
    rank: int | None
    coins: int | None
    ad_coins: int | None
    killed_by: str | None
    ok: ControlTarget | None


@dataclass(frozen=True)
class UsernamePrompt:
    field: ControlTarget | None
    save: ControlTarget | None
    close: ControlTarget | None
    text: str | None


@dataclass(frozen=True)
class TournamentReading:
    menu: ControlTarget | None = None
    page: TournamentPage | None = None
    username: UsernamePrompt | None = None
    profile: ControlTarget | None = None
    buy_ticket: ControlTarget | None = None
    stats: TournamentStats | None = None
    hud_marker: bool = False
    blocked: bool = False


def _normal(text: str) -> str:
    return re.sub(r'\s+', '', text).lower()


def _trusted(boxes: tuple[TextBox, ...]) -> tuple[TextBox, ...]:
    return tuple(b for b in boxes if math.isfinite(b.confidence) and .90 <= b.confidence <= 1
                 and b.rect.w > 0 and b.rect.h > 0)


def _one(boxes: tuple[TextBox, ...], text: str) -> TextBox | None:
    found = [b for b in boxes if _normal(b.text) == _normal(text)]
    return found[0] if len(found) == 1 else None


def _control(box: TextBox | None, name: str) -> ControlTarget | None:
    if box is None:
        return None
    r = box.rect
    return ControlTarget(name, (r.x+r.w//2, r.y+r.h//2), 'located', box.confidence, tuple(r))


@lru_cache(maxsize=3)
def _template(name: str) -> Image | None:
    return cv2.imread(str(Path(__file__).parent / 'templates' / 'tournament' / (name+'.png')))


def _visual(frame: Image, name: str, rect: Rect) -> ControlTarget | None:
    template = _template(name)
    crop = frame[rect.y:rect.y+rect.h, rect.x:rect.x+rect.w]
    if template is None or crop.shape[0] < template.shape[0] or crop.shape[1] < template.shape[1]:
        return None
    result = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(result)
    if score < .94:
        return None
    x, y = rect.x+loc[0], rect.y+loc[1]
    h, w = template.shape[:2]
    # Normalized correlation alone also matches dimmed controls. Require
    # similar intensity so a modal cannot authorize the page behind it.
    matched = frame[y:y+h, x:x+w]
    if abs(float(matched.mean())-float(template.mean())) > 12:
        return None
    return ControlTarget(name, (x+w//2,y+h//2), 'located', score, (x,y,w,h))


def _league(boxes: tuple[TextBox, ...]) -> str | None:
    found = [m.group(1).title() for b in boxes
             if (m := re.fullmatch(r'(Copper|Silver|Gold|Platinum|Champion|Legend|Mythic)\s*League', b.text, re.I))]
    return found[0] if len(found) == 1 else None


def _value(boxes: tuple[TextBox, ...], pattern: str) -> str | None:
    found = [m.group(1) for b in boxes if (m := re.fullmatch(pattern, b.text, re.I))]
    return found[0] if len(found) == 1 else None


def _under(boxes: tuple[TextBox, ...], label: str) -> int | None:
    heading = _one(boxes, label)
    if heading is None:
        return None
    found = [b for b in boxes if b.text.isdigit() and heading.rect.y+heading.rect.h < b.rect.y < heading.rect.y+150
             and abs((b.rect.x+b.rect.w/2)-(heading.rect.x+heading.rect.w/2)) < 140]
    return int(found[0].text) if len(found) == 1 else None


def read_page(frame: Image, boxes: tuple[TextBox, ...]) -> TournamentPage | None:
    return scan(frame, boxes).page


def scan(frame: Image, boxes: tuple[TextBox, ...]) -> TournamentReading:
    if frame.shape[:2] != (2400,1080):
        return TournamentReading(blocked=True)
    boxes = _trusted(boxes)
    stats = _one(boxes, 'TOURNAMENT STATS')
    if stats is not None:
        wave = _value(boxes, r'Wave\s*(\d+)')
        rank = _value(boxes, r'currently\s*at\s*rank:\s*(\d+)')
        reading = TournamentStats(_league(boxes), int(wave) if wave else None,
            int(rank) if rank else None, _under(boxes, 'coins earned'),
            _under(boxes, 'ad coins earned'), _value(boxes, r'Killed\s*By\s*(.+)'),
            _control(_one(boxes,'OK'),'tournament_ok'))
        return TournamentReading(stats=reading, blocked=True)
    if _one(boxes, 'USER NAME') is not None:
        candidates = [b for b in boxes if 1260 < b.rect.y < 1420 and 260 < b.rect.x < 800]
        field = candidates[0] if len(candidates) == 1 else None
        return TournamentReading(username=UsernamePrompt(_control(field,'tournament_name'),
            _control(_one(boxes,'Save'),'tournament_name_save'),
            _visual(frame,'name_close',Rect(810,740,150,160)), field.text if field else None), blocked=True)
    if _one(boxes, 'PLAYER PROFILE') is not None:
        return TournamentReading(profile=_visual(frame,'profile_close',Rect(850,560,180,170)), blocked=True)
    if _one(boxes, 'Buy Ticket') is not None:
        return TournamentReading(buy_ticket=_control(_one(boxes,'Cancel'),'ticket_cancel'), blocked=True)
    # Labels qualified on the page do not include arbitrary confirmation,
    # purchase or error text. Such text always dominates underlying BATTLE.
    if any(re.search(r'\b(buy|purchase|confirm|cancel|error|watch\s*ad|retry)\b', b.text, re.I)
           or re.search(r'\d+\s*gems?\b', b.text, re.I) for b in boxes):
        return TournamentReading(blocked=True)
    tier = [b for b in boxes if re.fullmatch(r'Tier\s*\d+\+',b.text,re.I)
            and 1400 < b.rect.y < 1600 and 500 < b.rect.x < 850]
    wave = [b for b in boxes if re.fullmatch(r'Wave\s*\d+',b.text,re.I)
            and 1450 < b.rect.y < 1650]
    if len(tier) == 1 and len(wave) == 1:
        return TournamentReading(hud_marker=True)
    header = _one(boxes,'TOURNAMENT')
    if header is not None and header.rect.y < 200:
        counter = [b for b in boxes if b.text.isdigit() and b.rect.x > 970 and 95 < b.rect.y < 190]
        ticket = int(counter[0].text) if len(counter) == 1 else None
        timer = _value(boxes, r'Time\s*left\s*to\s*join:\s*(.+)')
        seconds = None
        if timer:
            parts = re.fullmatch(r'(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?',timer)
            if parts and any(parts.groups()):
                seconds = sum(int(v or 0)*factor for v,factor in zip(parts.groups(),(3600,60,1)))
        token = _visual(frame,'battle_ticket',Rect(270,1840,240,290))
        battle = _one(boxes,'BATTLE')
        return TournamentReading(page=TournamentPage(ticket,_league(boxes),seconds,
            _value(boxes,r'Tournament\s*ID:\s*(\S+)'),
            _control(battle,'tournament_battle') if token and battle and battle.rect.y > 1850 else None,
            _control(_one(boxes,'Tap To Return To Game'),'tournament_return'), token is not None))
    opened = _one(boxes,'OPEN')
    if opened and opened.rect.x < 250 and 470 < opened.rect.y < 640 and _one(boxes,'THE TOWER'):
        return TournamentReading(menu=_control(opened,'tournament_open'))
    return TournamentReading()
