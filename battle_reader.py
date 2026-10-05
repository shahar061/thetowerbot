"""Shared HUD and target crops for modeled battle purchases."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Mapping

import cv2

import battle_tab
import config
import ocr
import tiles
from perception import Observation, contains, parse_frame, stat_number


class BattleReader:
    """Reuse labels only while their exact pixels, tab, layout and scope match.

    Numeric values and MAX are always fresh. A price model never supplies OCR
    boxes. Full screen popup checks remain owned by the loop's backstop.
    """
    def __init__(self) -> None:
        self._scope: object = None
        self._last: Observation | None = None
        self._layout: tuple[config.Rect, ...] = ()
        self._labels: tuple[ocr.TextBox, ...] = ()
        self._label_hash: bytes = b''
        self._numbers: dict[str, tuple[bytes, tuple[ocr.TextBox, ...]]] = {}

    @staticmethod
    def target_regions(rect: config.Rect) -> tuple[config.Rect, config.Rect]:
        # Measured on the supported 1920/2400 fixtures. The parser's .41
        # threshold classifies glyph tops and would cut the value in half.
        split = round(rect.h * .65)
        return (config.Rect(rect.x + rect.w // 2, rect.y, rect.w - rect.w // 2, split),
                config.Rect(rect.x + rect.w // 2, rect.y + round(rect.h * .55),
                            rect.w - rect.w // 2, rect.h - round(rect.h * .55)))

    def _hash_labels(self, screen: Any) -> bytes:
        digest = hashlib.sha256()
        for r in self._layout:
            digest.update(screen[r.y:r.y+r.h, r.x:r.x+r.w//2].tobytes())
        return digest.digest()

    @staticmethod
    def _numeric_region(rect: config.Rect) -> config.Rect:
        return config.Rect(rect.x + rect.w // 2, rect.y, rect.w - rect.w // 2, rect.h)

    @staticmethod
    def _pixels(screen: Any, rect: config.Rect) -> bytes:
        return hashlib.sha256(screen[rect.y:rect.y+rect.h, rect.x:rect.x+rect.w].tobytes()).digest()

    def _remember_numbers(self, screen: Any, boxes: tuple[ocr.TextBox, ...], observation: Observation) -> None:
        for row in observation.rows:
            if row.confidence < .9 or row.upgrade_id.startswith('discovered:'):
                self._numbers.pop(row.upgrade_id, None)
                continue
            region = self._numeric_region(row.rect)
            numeric = tuple(b for b in boxes if contains(row.rect, b.rect) and b.rect.x >= region.x)
            if numeric:
                self._numbers[row.upgrade_id] = (self._pixels(screen, region), numeric)

    @staticmethod
    def _crop(reads: ocr.FrameReads, region: config.Rect) -> tuple[ocr.TextBox, ...]:
        return tuple(replace(b, rect=config.Rect(
            b.rect.x + region.x - ocr.CROP_PADDING, b.rect.y + region.y - ocr.CROP_PADDING,
            b.rect.w, b.rect.h)) for b in ocr.read_region(reads.screen, region, upscale=False))

    @staticmethod
    def _band(reads: ocr.FrameReads, region: config.Rect) -> tuple[ocr.TextBox, ...]:
        crop = reads.screen[region.y:region.y+region.h, region.x:region.x+region.w]
        small = cv2.resize(crop, None, fx=config.BATTLE_OCR_SCALE, fy=config.BATTLE_OCR_SCALE,
                           interpolation=cv2.INTER_AREA)
        return tuple(ocr.band_box_to_frame(b, region, crop.shape[1] / small.shape[1],
                     crop.shape[0] / small.shape[0]) for b in ocr.read(small, strict=True, upscale=False))

    def read(self, reads: ocr.FrameReads, *, target: str | None,
             quote: Mapping[str, Any] | None, scope: object) -> tuple[ocr.TextBox, ...]:
        screen = reads.screen
        bands = config.BATTLE_BANDS.get((screen.shape[1], screen.shape[0]))
        layout = tiles.find_tiles(screen)
        row = next((r for r in self._last.rows if r.upgrade_id == target), None) if self._last else None
        if (bands is not None and self._last is not None and row is not None
                and self._scope == scope and layout == self._layout and self._labels
                and self._last.category == battle_tab.classify_frame(screen)
                and self._hash_labels(screen) == self._label_hash):
            hud = config.Rect(bands.panel.x, bands.panel.y, bands.panel.w,
                              bands.heading.y + bands.heading.h - bands.panel.y)
            boxes = (*self._band(reads, bands.top), *self._band(reads, hud), *self._labels)
            partial = parse_frame(screen, boxes, 'battle', digest=reads.digest,
                                  tab_colour=self._last.category)
            calibrating = not quote or not quote.get('verified') or quote.get('wave') != partial.combat.get('wave')
            # Cached text is evidence for this frame only when the entire
            # numeric region has identical pixels. Read changed targets;
            # unchanged neighbours can authorize the next visible purchase.
            for previous in self._last.rows:
                region = self._numeric_region(previous.rect)
                cached = self._numbers.get(previous.upgrade_id)
                if cached is not None and cached[0] == self._pixels(screen, region):
                    boxes = (*boxes, *cached[1])
                elif previous.upgrade_id == target:
                    boxes = (*boxes, *self._crop(reads, region))
            current = parse_frame(screen, boxes, 'battle', digest=reads.digest,
                                  tab_colour=self._last.category)
            target_row = next((r for r in current.rows if r.upgrade_id == target), None)
            if (current.category == self._last.category and current.cash is not None
                    and current.combat.get('wave') is not None and target_row is not None
                    and target_row.confidence >= .9
                    and (target_row.value is not None or target_row.status in {'maxed', 'locked'})
                    and (not calibrating or target_row.price is not None
                         or target_row.status in {'maxed', 'locked'})):
                reads.battle_targeted = True
                self._remember_numbers(screen, boxes, current)
                return boxes
        boxes = reads._read_battle()
        self._last = parse_frame(screen, boxes, 'battle', digest=reads.digest,
                                 tab_colour=battle_tab.classify_frame(screen))
        self._scope, self._layout = scope, layout
        self._labels = tuple(b for b in boxes if stat_number(b.text) is None and not any(c.isdigit() for c in b.text)
            and b.text.strip().upper() not in {'MAX', 'MAXED', 'LOCKED', 'UNAVAILABLE'}
            and any(contains(r.rect, b.rect) and b.rect.x < r.rect.x + r.rect.w * .5
                    for r in self._last.rows if not r.upgrade_id.startswith('discovered:')))
        self._label_hash = self._hash_labels(screen)
        self._numbers.clear()
        self._remember_numbers(screen, boxes, self._last)
        return boxes
