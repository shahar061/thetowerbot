"""Read text off the screen.

    frame  ->  engine  ->  drop low confidence  ->  TextBox

Every entry point returns empty or None rather than raising. A bad read must
degrade the bot, never stop the scan loop - the same rule digits.py follows.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import numpy as np

import config
from config import Rect
from device import Image

logger = logging.getLogger("tower_bot.ocr")

# Sentinel for "construction was tried and failed", distinct from None's
# "not tried yet". Without it a broken wheel is retried on every scan, and
# every scan writes a full traceback into the same stderr the Phase 1 A/B
# evidence goes to - a couple of screens a second of it. There is no startup
# gate until Phase 2, so this is what makes the failure survivable.
_FAILED = object()

_engine: Any | None = None
# One lock guarding construction AND inference. The bot and the web server
# share a process, and the library makes no thread-safety promise worth
# betting a purchase on.
_lock = threading.Lock()

# Both caches key on exact pixel bytes, so a hit is the engine's own answer
# for that input - never a guess from a similar-looking one. Guarded by _lock.
#
# Frames: one tick hands the same frame to several full-frame readers (the
# supervisor, the shared missions/milestones read, the autopilot), each ~1s.
# Each entry holds the engine that produced it, so a swapped engine misses.
_frame_results: OrderedDict[bytes, tuple[Any, Any]] = OrderedDict()
# Crops: recognition is ~60% of a read, and mid-run 65% of the text boxes on
# a frame are byte-identical to ones read 2s earlier (labels, static values).
_crop_results: OrderedDict[bytes, Any] = OrderedDict()
# Regions: padded crops and bands, kept apart so a scan's crop reads cannot
# evict the full frame (spec P0c). Same (engine, result) entries as frames.
_region_results: OrderedDict[bytes, tuple[Any, Any]] = OrderedDict()


def _results_for(image: Image) -> tuple[OrderedDict[bytes, tuple[Any, Any]], int]:
    """The result cache, and its size limit, an image of this size belongs in."""
    height, width = image.shape[:2]
    if height * width >= config.OCR_FRAME_MIN_PIXELS:
        return _frame_results, config.OCR_FRAME_CACHE
    return _region_results, config.OCR_REGION_CACHE


def _digest(image: Image) -> bytes:
    array = np.ascontiguousarray(image)
    return hashlib.blake2b(
        memoryview(array).cast("B"), digest_size=16,
        salt=repr((array.shape, array.dtype.str)).encode()[:16],
    ).digest()


def _remember(cache: OrderedDict[bytes, Any], key: bytes, value: Any, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    if len(cache) > limit:
        cache.popitem(last=False)


def _set_det_mode(engine: Any, upscale: bool) -> bytes:
    """Point the detector at this read's resize rule; return it for the cache key.

    RapidOCR's TextDetector re-reads `limit_type` on every call (its
    get_preprocess), and every read runs under _lock, so this applies to
    exactly one read. "min" is the library default: enlarge any input whose
    short side is under 736 px. "max" only ever shrinks.
    """
    mode = "min" if upscale or config.OCR_DET_UPSCALE else "max"
    detector = getattr(engine, "text_det", None)
    if detector is not None:
        detector.limit_type = mode
    return mode.encode()


def _cache_recognition(engine: Any) -> None:
    """Wrap the engine's recognizer so crops it has read before are not re-run.

    RapidOCR batches crops sorted by width, so an uncached crop's result can
    shift slightly with its batch-mates; a cached one keeps its first answer.
    """
    recognize = getattr(engine, "text_rec", None)
    if recognize is None:
        return

    def cached(crops: list[Image], return_word_box: bool = False) -> tuple[list[Any], float]:
        if return_word_box:
            return recognize(crops, return_word_box)
        keys = [_digest(crop) for crop in crops]
        known = {key: _crop_results[key] for key in keys if key in _crop_results}
        misses = [i for i, key in enumerate(keys) if key not in known]
        elapsed = 0.0
        if misses:
            fresh, elapsed = recognize([crops[i] for i in misses], return_word_box)
            known.update((keys[i], result) for i, result in zip(misses, fresh))
        for key, result in known.items():
            _remember(_crop_results, key, result, config.OCR_CROP_CACHE)
        return [known[key] for key in keys], elapsed

    engine.text_rec = cached


@dataclass(frozen=True)
class TextBox:
    text: str
    confidence: float
    rect: Rect


def _engine_or_none() -> Any | None:
    """The engine, built once, or None if building it failed.

    Failure is remembered: construction is attempted at most once per
    process, and the traceback is logged once rather than once per scan.
    """
    global _engine
    if _engine is _FAILED:
        return None
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR

            # use_cls off: the angle classifier looks for upside-down text,
            # which game UI never has. Measured on a 2400x1080 frame: same
            # 24 boxes, 918ms -> 698ms.
            _engine = RapidOCR(intra_op_num_threads=config.OCR_THREADS,
                               inter_op_num_threads=1, use_cls=False)
            _cache_recognition(_engine)
        except Exception:
            _engine = _FAILED
            logger.exception("could not build the OCR engine; not trying again")
            return None
    return _engine


def read(screen: Image | None, *, strict: bool = False,
         min_confidence: float | None = None, upscale: bool = True) -> tuple[TextBox, ...]:
    """Read boxes, optionally surfacing errors or retaining guard candidates.

    Existing callers retain the configured confidence floor and empty-on-error
    behavior. A passive modal guard can retain uncertain titles without ever
    accepting their values as observations.

    `upscale=False` lets the detector skip enlarging a small input (see
    config.OCR_DET_UPSCALE). Only readers whose parity was measured without
    it opt out.
    """
    if screen is None:
        return ()
    started = time.perf_counter()
    with _lock:
        waited = time.perf_counter() - started
        engine = _engine_or_none()
        if engine is None:
            if strict:
                raise RuntimeError('OCR engine unavailable')
            return ()
        try:
            key = _digest(screen) + _set_det_mode(engine, upscale)
            results, limit = _results_for(screen)
            cached_engine, result = results.get(key, (None, None))
            if cached_engine is engine:
                results.move_to_end(key)
            else:
                result, _elapsed = engine(screen)
                _remember(results, key, (engine, result), limit)
        except Exception:
            logger.exception("OCR failed on a %s frame", getattr(screen, "shape", "?"))
            if strict:
                raise RuntimeError('OCR inference failed') from None
            return ()
        finally:
            elapsed = time.perf_counter() - started
            if elapsed > config.OCR_SLOW_SECONDS:
                logger.warning("slow OCR: %.2fs on a %s frame (%.2fs waiting for the engine)",
                               elapsed, getattr(screen, "shape", "?"), waited)

    boxes: list[TextBox] = []
    try:
        for box, text, confidence in result or ():
            if confidence < (config.OCR_CONFIDENCE_FLOOR if min_confidence is None else min_confidence):
                logger.debug("dropped %r at confidence %.3f", text, confidence)
                continue
            xs = [int(point[0]) for point in box]
            ys = [int(point[1]) for point in box]
            boxes.append(
                TextBox(
                    text=text.strip(),
                    confidence=float(confidence),
                    rect=Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)),
                )
            )
    except Exception:
        # Same rule as the engine call above: a malformed result shape must
        # degrade to "nothing read", never raise out of the scan loop - but
        # a caught exception that leaves no trace is its own defect.
        logger.exception("could not turn the OCR result into boxes")
        if strict:
            raise RuntimeError('OCR output invalid') from None
        return ()
    return tuple(boxes)


# fullmatch() is itself the anchor - a leading/trailing ^/$ in the pattern
# would be a no-op. A partial match on "0.00/sec" or "x1.20" would report a
# stat value as a price; refusing is always safe, while a wrong number is not.
_NUMBER = re.compile(r"\$?(\d+(?:\.\d+)?)([KMBT])?")
_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000,
             "T": 1_000_000_000_000}


def parse_number(text: str) -> int | None:
    """A price or balance, or None if this is not one."""
    match = _NUMBER.fullmatch(text.strip())
    if match is None:
        return None
    digits, suffix = match.groups()
    # round(), not int(): float(digits) * multiplier is not always exact
    # ("2.01" * 1000 lands a hair under 2010), and int() truncates toward
    # zero instead of correcting for it. This module's contract is to refuse
    # rather than guess - a rounding-induced off-by-one is exactly the kind
    # of silently wrong number that contract exists to prevent.
    return round(float(digits) * _SUFFIXES.get(suffix or "", 1))


# Padding put around a cropped region before reading it. The engine's
# DETECTION stage, not its recognition, is what needs this: a short isolated
# number in a full frame can produce no box at all - a lone "0" in the gems
# header returns nothing at any confidence floor, including 0.0 - while the
# same pixels cropped and padded read at 0.92. Quiet margin around the
# glyphs is what buys the detection.
CROP_PADDING: int = 20


def read_region(
    screen: Image | None,
    rect: Rect,
    *,
    padding: int = CROP_PADDING,
    min_confidence: float | None = None,
) -> tuple[TextBox, ...]:
    """Read one region off its own padded crop, in frame coordinates.

    Preferred over filtering a whole-frame read down to `rect` whenever the
    region holds one short number. It is not only more accurate but cheaper:
    measured on a menu frame, two padded header crops cost ~139ms against
    ~222ms for a single whole-frame read, because the detection stage scales
    with the pixels it is handed.

    Boxes come back in CROP coordinates, offset for the padding, so a caller
    that only wants the text can ignore geometry entirely. Returns empty for
    a rect that lands off-screen rather than raising - same rule as read().
    """
    if screen is None:
        return ()
    height, width = screen.shape[:2]
    x0, y0 = max(rect.x, 0), max(rect.y, 0)
    x1, y1 = min(rect.x + rect.w, width), min(rect.y + rect.h, height)
    if x1 <= x0 or y1 <= y0:
        return ()

    crop = screen[y0:y1, x0:x1]
    import cv2  # local: keeps the import cost off callers that never crop

    padded = cv2.copyMakeBorder(
        crop, padding, padding, padding, padding, cv2.BORDER_CONSTANT
    )
    return read(padded, min_confidence=min_confidence)


def available() -> bool:
    """Whether the engine will load - the startup gate of spec §10.

    Building it here rather than on the first scan is the point: a missing
    or broken wheel becomes a loud, once-only reason at startup instead of
    silent inaction discovered a visit later.
    """
    return _engine_or_none() is not None


def number_in(boxes: tuple[TextBox, ...], rect: Rect) -> int | None:
    """The one number read inside `rect`, or None.

    Membership is by box CENTRE, not by containment: OCR boxes sit a few
    pixels proud of the glyphs they bound, and a region measured on the
    glyphs would reject its own number if the whole box had to fit.

    Two numbers inside one region is ambiguity, and ambiguity refuses. The
    coin and gem balances share a header row, so "pick the first" would pick
    a balance by luck - and this module's contract, everywhere else too, is
    that a refused read is safe and a wrong number is not.
    """
    numbers = [
        number
        for box in boxes
        if _centre_in(rect, box.rect)
        and (number := parse_number(box.text)) is not None
    ]
    if len(numbers) != 1:
        if numbers:
            logger.warning("refusing %s: %d numbers inside it", rect, len(numbers))
        return None
    return numbers[0]


def _centre_in(rect: Rect, box: Rect) -> bool:
    cx, cy = box.x + box.w // 2, box.y + box.h // 2
    return rect.x <= cx < rect.x + rect.w and rect.y <= cy < rect.y + rect.h


class FrameReads:
    """One scan's OCR of one frame, shared by every reader in run_once.

    `full()` reads the whole frame at most once per scan and remembers the
    result - or the exception, which it re-raises to every later caller, so
    a strict reader still sees the failure (spec invariant 3). It calls this
    module's `read`, so a test that replaces ocr.read replaces it here too.

    `digest` is the frame's SHA-256, computed once: the supervisor,
    perception.parse_frame and the autopilot each used to hash it again.
    """

    def __init__(self, screen: Image) -> None:
        self.screen = screen
        self._digest: str | None = None
        self._full: tuple[TextBox, ...] | None = None
        self._full_error: Exception | None = None

    @property
    def digest(self) -> str:
        if self._digest is None:
            self._digest = hashlib.sha256(self.screen.tobytes()).hexdigest()
        return self._digest

    def full(self) -> tuple[TextBox, ...]:
        if self._full_error is not None:
            raise self._full_error
        if self._full is None:
            try:
                self._full = read(self.screen, strict=True)
            except Exception as error:
                self._full_error = error
                raise
        return self._full
