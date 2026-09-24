"""The Missions claim walk, driven one fake frame at a time.

No OCR: `MissionsReadings.claim_evidence()` is stood in for directly, so every
case here is about what the transaction DOES with that evidence rather than
about reading the page. The two device-touching taps it can still make -
opening Missions from the main menu and returning from it - go through the
real `account_collection.locate_control` against real captures and a real
`vision.TemplateCache`, not a synthetic image: a constant screen matched
against a constant template is a degenerate normalized cross-correlation (see
tests/test_account_screens.py's own `locate_control(zeros, zeros)` case) that
reports every position tied at the top score, which `locate_control` then
correctly calls ambiguous rather than located - so a synthetic frame here
would make `_tap` refuse before the walk ever reached the CLAIM step, and
every assertion below would hold for that wrong reason instead of the one it
names. Real frames are what test_missions_visit.py uses for the same reason.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2

import config
import events
import missions_claim
import missions_screen
import vision

FIXTURES = Path(__file__).parent / 'fixtures'

# The main menu's MISSIONS control and the missions page's return control,
# measured the same way test_missions_visit.py measures them: template match
# on the same real captures this file drives the walk over.
MISSIONS_CONTROL = (987, 351)
RETURN_CONTROL = (540, 2293)


class FakeBus:
    def __init__(self) -> None:
        self.published: list[events.Event] = []

    def publish(self, event: events.Event) -> events.Event:
        self.published.append(event)
        return event

    def of(self, kind: type) -> list[events.Event]:
        return [e for e in self.published if isinstance(e, kind)]


class FakeReadings:
    """Stands in for MissionsReadings, replaying scripted frames."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._frames = frames
        self._at = 0

    def step(self) -> None:
        self._at = min(self._at + 1, len(self._frames) - 1)

    def claim_evidence(self) -> dict[str, Any]:
        return self._frames[self._at]

    def current_evidence(self) -> dict[str, Any]:
        frame = self._frames[self._at]
        return {k: frame[k] for k in ('screen_id', 'error', 'scanned')}


class FakePanel:
    def current_evidence(self) -> dict[str, Any]:
        return {'screen_id': None, 'error': None, 'scanned': True}


def home() -> dict[str, Any]:
    return {'screen_id': None, 'error': None, 'scanned': True,
            'completed': None, 'claims': ()}


def page(completed: int | None, claims: int, *, error: str | None = None,
         screen_id: str | None = 'missions.daily',
         visible: tuple[str, ...] = ()) -> dict[str, Any]:
    """A missions-page frame. `error`/`screen_id` default to a clean read;
    a caller exercising `missions_unreadable` or `missions_not_reached`
    overrides one of them."""
    targets = tuple(
        missions_screen.ClaimTarget(f'mission_{i}', f'Mission {i}', 25, 3,
                                    (454, 751 + 265 * i, 153, 46))
        for i in range(claims))
    return {'screen_id': screen_id, 'error': error, 'scanned': True,
            'completed': completed, 'claims': targets, 'visible': visible}


def image(name: str) -> Any:
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    assert frame is not None, f'missing fixture: {name}.png'
    return frame


def screen(step: str) -> Any:
    """A real 2400x1080 capture, chosen for whichever tap this step might
    make.

    OPEN_MISSIONS taps the MISSIONS control, which is only on the main menu -
    menu_main.png, matching nav/missions.png at 1.0000, same as
    test_missions_visit.py's MISSIONS_CONTROL. Every other step's own tap -
    the CLAIM step's return-control tap, made once claims run out or the
    bound is hit - is located on the missions page:
    menu_missions_claimable_no_status_bar.png matches nav/missions_return.png
    at 0.9999986, same coordinate test_missions_visit.py's RETURN_CONTROL
    names. No other step calls locate_control at all (a CLAIM button is
    tapped from the reader's own rect, never by template match), so which of
    the two stands in for those frames is not load-bearing.
    """
    return image('menu_main' if step == 'open_missions' else
                 'menu_missions_claimable_no_status_bar')


class FakeTemplates:
    """The real template cache. `_tap` must run genuine `cv2.matchTemplate`
    here, not a stand-in that could report `located` by construction."""

    def __init__(self) -> None:
        self._cache = vision.TemplateCache(config.TEMPLATE_DIR)

    def get(self, name: str) -> Any:
        return self._cache.get(name)


class FakeDevice:
    """Records taps: the coordinate this walk actually reaches for."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, float]] = []

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def swipe(self, x: int, y: int, x2: int, y2: int, duration: float) -> None:
        self.swipes.append((x, y, x2, y2, duration))


def drive(frames: list[dict[str, Any]], *,
          steps: int = 20) -> tuple[missions_claim.MissionsClaim, FakeBus, FakeDevice]:
    """Run a walk to completion against scripted frames."""
    claim = missions_claim.MissionsClaim()
    claim.request(now=0.)
    bus, readings = FakeBus(), FakeReadings(frames)
    templates, device = FakeTemplates(), FakeDevice()
    for _ in range(steps):
        if not claim.active:
            break
        claim.advance(screen=screen(claim.snapshot()['step']), device=device,
                      templates=templates, readings=FakePanel(), missions=readings,
                      bus=bus, state='MAIN_MENU', now=0.)
        readings.step()
    return claim, bus, device


def test_a_walk_refuses_to_tap_when_the_counter_cannot_be_read() -> None:
    """The success test IS the counter. Without it a claim is unverifiable,
    and an unverifiable claim is worse than no claim: nothing afterwards could
    say whether the reward was taken."""
    claim, bus, _ = drive([home(), page(None, 2)])
    assert claim.snapshot()['result']['reason'] == 'counter_unreadable'
    assert not bus.of(events.MissionClaimed)
    assert bus.of(events.ClaimSkipped)


def test_a_claim_is_only_recorded_once_the_counter_moves() -> None:
    claim, bus, _ = drive([home(), page(0, 2), page(1, 1), page(2, 0)])
    claimed = bus.of(events.MissionClaimed)
    assert len(claimed) == 2
    assert (claimed[0].completed_before, claimed[0].completed_after) == (0, 1)


def test_a_counter_that_never_moves_ends_the_walk_rather_than_retapping() -> None:
    """A tap that changed nothing is reported, not repeated.

    The tap that WAS made may or may not have landed - the reflow or OCR
    could simply have lagged - so this is the one ambiguous outcome the
    ledger must still be able to name. ClaimEnded alone only carries a count;
    the ClaimUncertain this walk publishes on the way out is what names which
    mission ("Mission 0") was tapped, and it is deliberately not ClaimSkipped:
    ClaimSkipped means the walk provably moved nothing, but a CLAIM was
    tapped here, so the reward may well have been taken.
    """
    frames = [home()] + [page(0, 2)] * 12
    claim, bus, _ = drive(frames)
    assert claim.snapshot()['result']['status'] == 'failed'
    assert claim.snapshot()['result']['reason'] == 'claim_not_confirmed'
    assert not bus.of(events.MissionClaimed)
    uncertain = bus.of(events.ClaimUncertain)
    assert uncertain and uncertain[-1].reason == 'claim_not_confirmed'
    assert 'Mission 0' in uncertain[-1].detail
    # A CLAIM was tapped, so this is not a refusal: ClaimSkipped would assert
    # the walk moved nothing, and the reward may well have been taken.
    assert not bus.of(events.ClaimSkipped)


def test_a_walk_with_nothing_claimable_returns_home_without_tapping() -> None:
    """Nothing claimable means the walk still has to tap its own way home -
    checked against the walk actually completing and actually returning, not
    just against the absence of a claim (which an unrelated early failure,
    e.g. the MISSIONS control refusing, would satisfy just as well)."""
    claim, bus, device = drive([home(), page(4, 0), home()])
    assert not bus.of(events.MissionClaimed)
    ended = bus.of(events.ClaimEnded)
    assert ended and ended[0].claimed == 0 and ended[0].aborted is False
    result = claim.snapshot()['result']
    assert result['status'] == 'completed' and result['reason'] == 'claimed'
    assert RETURN_CONTROL in device.taps


def test_a_walk_scrolls_to_claim_a_reward_below_the_first_cards() -> None:
    frames = [home(), page(0, 0, visible=('daily-a', 'daily-b')),
              page(0, 1, visible=('daily-c', 'daily-d')),
              page(1, 0), home()]
    claim, bus, device = drive(frames)
    assert len(device.swipes) == 1
    assert len(bus.of(events.MissionClaimed)) == 1
    assert claim.snapshot()['result']['status'] == 'completed'


def test_a_walk_stops_scrolling_when_the_list_does_not_move() -> None:
    frames = [home()] + [page(0, 0, visible=('daily-a', 'daily-b'))] * 8 + [home()]
    claim, bus, device = drive(frames)
    assert len(device.swipes) == 1
    assert not bus.of(events.MissionClaimed)
    assert claim.snapshot()['result']['status'] == 'completed'


def test_a_walk_stops_after_four_distinct_pages_without_a_claim() -> None:
    frames = [home()] + [page(0, 0, visible=(f'page-{i}',))
                         for i in range(5)] + [home()]
    claim, bus, device = drive(frames)
    assert len(device.swipes) == missions_claim.MAX_MISSIONS_SCROLLS
    assert not bus.of(events.MissionClaimed)
    assert claim.snapshot()['result']['status'] == 'completed'


def test_a_walk_stops_at_the_bound_even_if_the_page_keeps_offering() -> None:
    """The page offers at most 8. A page that keeps offering is a misread of
    reflow, and a walk that keeps tapping it is a loop."""
    frames = [home()] + [page(n, 3) for n in range(1, 30)]
    claim, bus, _ = drive(frames, steps=80)
    assert len(bus.of(events.MissionClaimed)) == missions_claim.MAX_CLAIMS_PER_WALK


def test_pausing_mid_walk_cancels_it() -> None:
    claim = missions_claim.MissionsClaim()
    claim.request(now=0.)
    claim.cancel('paused', 'The bot was paused mid-claim.', now=1.)
    assert claim.active is False
    assert claim.snapshot()['result']['reason'] == 'paused'


def test_the_snapshot_carries_no_coordinate() -> None:
    """A tap is located again on the frame it is made from, so a remembered
    point here could only ever be used wrongly - checked on the one frame
    `_pending` actually holds a claim awaiting proof. Checking only after the
    walk ends proves nothing: `_finish` always clears `_pending` to None by
    then, so a snapshot sampled there reads the same whether or not the
    field it is guarding ever held a coordinate mid-walk.
    """
    claim = missions_claim.MissionsClaim()
    claim.request(now=0.)
    bus = FakeBus()
    readings = FakeReadings([home(), page(0, 1), page(1, 0)])
    templates, device = FakeTemplates(), FakeDevice()

    def step() -> None:
        claim.advance(screen=screen(claim.snapshot()['step']), device=device,
                      templates=templates, readings=FakePanel(), missions=readings,
                      bus=bus, state='MAIN_MENU', now=0.)
        readings.step()

    step()  # taps the MISSIONS control; step becomes CLAIM
    step()  # taps the CLAIM button on 'Mission 0'; _pending is now set

    mid_walk = claim.snapshot()
    assert mid_walk['claimed'] == 0  # tapped, not yet confirmed by the counter
    text = repr(mid_walk)
    assert '454' not in text and 'rect' not in text

    step()  # the counter moves; the claim is confirmed and the pending
    text_after = repr(claim.snapshot())
    assert '454' not in text_after and 'rect' not in text_after


# --- guards with no coverage above (added per Ruling 8's fix-round-2
# mutation audit: each of these guards fired zero times in the tests above,
# so disabling it left every existing assertion passing). ------------------

def test_the_open_step_never_taps_before_home_is_confirmed() -> None:
    """`home_not_confirmed`: the OPEN_MISSIONS tap must not fire from a frame
    that is not actually the main menu. Driven directly (not through
    `drive()`, which always starts every script with a genuine `home()`
    frame) so this guard is exercised rather than skipped."""
    claim = missions_claim.MissionsClaim()
    claim.request(now=0.)
    device = FakeDevice()
    for _ in range(8):
        claim.advance(screen=screen('claim'), device=device, templates=FakeTemplates(),
                      readings=FakePanel(), missions=FakeReadings([page(0, 1)]),
                      bus=FakeBus(), state='MAIN_MENU', now=0.)
    result = claim.snapshot()['result']
    assert result['reason'] == 'home_not_confirmed'
    assert device.taps == []


def test_the_walk_does_not_report_completed_before_home_is_restored() -> None:
    """`home_not_restored`: once the return control is tapped, the walk must
    not report `completed` while the missions page is still evidently up."""
    claim = missions_claim.MissionsClaim()
    claim.request(now=0.)
    bus = FakeBus()
    readings = FakeReadings([home(), page(4, 0)])  # never returns to home()
    templates, device = FakeTemplates(), FakeDevice()

    def step() -> None:
        claim.advance(screen=screen(claim.snapshot()['step']), device=device,
                      templates=templates, readings=FakePanel(), missions=readings,
                      bus=bus, state='MAIN_MENU', now=0.)
        readings.step()

    for _ in range(9):
        step()
    result = claim.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'home_not_restored'
    assert RETURN_CONTROL in device.taps


def test_an_unreadable_missions_page_is_refused_not_claimed() -> None:
    """`missions_unreadable`: the page was reached but a reader error is
    reported. No claim button is even considered."""
    claim, bus, device = drive([home(), page(0, 2, error='ocr failed')])
    result = claim.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'missions_unreadable'
    assert not bus.of(events.MissionClaimed)
    assert device.taps == [MISSIONS_CONTROL]


def test_a_claim_step_waits_for_the_missions_screen_id_before_reading_it() -> None:
    """`missions_not_reached`: the tap landed, but the very next frame does
    not yet show the missions page's own screen id."""
    frames = [home()] + [page(0, 2, screen_id=None)] * 8
    claim, bus, device = drive(frames)
    result = claim.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'missions_not_reached'
    assert not bus.of(events.MissionClaimed)
    assert device.taps == [MISSIONS_CONTROL]


def test_the_claim_button_is_tapped_at_the_rects_centre() -> None:
    """The one line deciding where the reward is actually tapped: for the
    plan's measured CLAIM box (454, 751, 153, 46), the centre is
    (454 + 153 // 2, 751 + 46 // 2) = (530, 774). Mutating this to the
    rect's top-left, or swapping x and y, must fail here."""
    claim, _, device = drive([home(), page(0, 1), page(1, 0)])
    assert (530, 774) in device.taps


def test_a_claim_walk_is_armed_at_most_once() -> None:
    """`request()` is idempotent per object: a second call while a walk is
    already active must arm nothing and report False, rather than restarting
    or stacking a second walk on the same instance.

    This does NOT pin mutual exclusion between a claim and a visit - both
    `visit.active` and `claim.active` are True below, because nothing here
    constructs the runner that enforces that rule between the two objects.
    That property is pinned in test_runner.py, by
    test_a_claim_and_the_other_two_transactions_are_never_armed_at_once_in_either_order.
    """
    import missions_visit
    claim = missions_claim.MissionsClaim()
    visit = missions_visit.MissionsVisit()
    assert visit.request() is True
    assert visit.active and not claim.active
    assert claim.request() is True
    assert claim.active
    assert claim.request() is False
