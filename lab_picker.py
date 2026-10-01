"""Find one lab in the research picker by scrolling, one swipe per frame.

Pure: it reads a PickerPage and says what to do next. The picker opens at the
top on every visit, so there is no scroll position to restore. The search
ends when the target card is fully inside the list, when the list stops
moving after a swipe (its end), or after MAX_SWIPES swipes.
"""

from __future__ import annotations

from dataclasses import dataclass

from lab_screen import PickerPage

MAX_SWIPES = 8
SWIPE_FRACTION = .6
SWIPE_SECONDS = .35


@dataclass(frozen=True)
class SearchStep:
    kind: str  # found | swipe | not_found | wait
    swipe: tuple[int, int, int, int] | None = None


class PickerSearch:
    def __init__(self, lab_id: str, width: int) -> None:
        self.lab_id, self.width = lab_id, width
        self.swipes = 0
        self.frames_seen = 0
        self._after_swipe: tuple | None = None

    def step(self, page: PickerPage) -> SearchStep:
        if not page.open or page.viewport is None:
            return SearchStep("wait")
        self.frames_seen += 1
        card = page.card(self.lab_id)
        if card is not None and card.fully_visible:
            return SearchStep("found")
        signature = page.signature()
        if self._after_swipe is not None and signature == self._after_swipe:
            return SearchStep("not_found")  # the list did not move: its end
        if self.swipes >= MAX_SWIPES:
            return SearchStep("not_found")
        top, bottom = page.viewport
        distance = int((bottom - top) * SWIPE_FRACTION)
        start = bottom - int((bottom - top) * .1)
        self.swipes += 1
        self._after_swipe = signature
        return SearchStep("swipe", (self.width // 5, start, self.width // 5, start - distance))
