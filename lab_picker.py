"""Find one lab in the research picker by scrolling, one swipe per frame.

Pure: it reads a PickerPage and says what to do next. The game keeps the
picker's scroll position while the app runs, so the search first swipes up
until the list stops moving (its top) or after MAX_UP_SWIPES, then down. It
ends when the target card is fully inside the list, when the list stops
moving after a down swipe (its end), or after MAX_SWIPES down swipes.
"""

from __future__ import annotations

from dataclasses import dataclass

from lab_screen import PickerPage

MAX_SWIPES = 8
MAX_UP_SWIPES = 8
SWIPE_FRACTION = .6
SWIPE_SECONDS = .35


@dataclass(frozen=True)
class SearchStep:
    kind: str  # found | swipe | not_found | wait
    swipe: tuple[int, int, int, int] | None = None


class PickerSearch:
    def __init__(self, lab_id: str, width: int) -> None:
        self.lab_id, self.width = lab_id, width
        self.swipes = 0  # down swipes
        self.up_swipes = 0
        self.frames_seen = 0
        self._at_top = False
        self._after_swipe: tuple | None = None

    def step(self, page: PickerPage) -> SearchStep:
        if not page.open or page.viewport is None:
            return SearchStep("wait")
        self.frames_seen += 1
        card = page.card(self.lab_id)
        if card is not None and card.fully_visible:
            return SearchStep("found")
        signature = page.signature()
        top, bottom = page.viewport
        distance = int((bottom - top) * SWIPE_FRACTION)
        if not self._at_top:
            if (self._after_swipe is not None and signature == self._after_swipe
                    or self.up_swipes >= MAX_UP_SWIPES):
                self._at_top, self._after_swipe = True, None  # the list did not move: its top
            else:
                # Drag down from the list's top edge: the list scrolls toward its start.
                start = top + int((bottom - top) * .1)
                self.up_swipes += 1
                self._after_swipe = signature
                return SearchStep("swipe", (self.width // 5, start, self.width // 5, start + distance))
        if self._after_swipe is not None and signature == self._after_swipe:
            return SearchStep("not_found")  # the list did not move: its end
        if self.swipes >= MAX_SWIPES:
            return SearchStep("not_found")
        start = bottom - int((bottom - top) * .1)
        self.swipes += 1
        self._after_swipe = signature
        return SearchStep("swipe", (self.width // 5, start, self.width // 5, start - distance))
