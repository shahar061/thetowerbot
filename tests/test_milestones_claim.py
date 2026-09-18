"""The MILESTONES claim walk, driven one fake frame at a time.

No OCR: `MilestonesReadings.claim_evidence()` is stood in for directly, so
every case here is about what the transaction DOES with that evidence rather
than about reading the page. The three device-touching taps it can still
make - opening Milestones from the main menu, tapping `Claim All`'s reader-
provided rect, tapping the modal's CLAIM, and returning - go through the real
`account_collection.locate_control` against real captures and a real
`vision.TemplateCache`, not a synthetic image, for the identical reason
tests/test_missions_claim.py gives: a constant screen matched against a
constant template is a degenerate normalized cross-correlation that reports
every position tied at the top score, which `locate_control` then correctly
calls ambiguous rather than located - so a synthetic frame here would make
`_tap` refuse before the walk ever reached a real step, and every assertion
below would hold for the wrong reason.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2

import config
import events
import account_collection
import milestones_claim
import vision

FIXTURES = Path(__file__).parent / 'fixtures'

# Measured (see milestones_claim's own header comments and the task's spec):
# the main menu's MILESTONES control on menu_milestones_entry.png, the
# ladder's own return control on both ladder captures, the modal's CLAIM on
# the reward modal capture, and the Claim All rect this reader actually
# produces from menu_milestones_claimable.png's real OCR fixture (its centre
# is what the tap-arithmetic test below pins).
MILESTONES_CONTROL = (540, 833)
RETURN_CONTROL = (540, 2293)
CLAIM_MODAL_CONTROL = (540, 1866)
CLAIM_ALL_RECT = (420, 255, 246, 53)
CLAIM_ALL_TAP = (543, 281)


class FakeBus:
    def __init__(self) -> None:
        self.published: list[events.Event] = []

    def publish(self, event: events.Event) -> events.Event:
        self.published.append(event)
        return event

    def of(self, kind: type) -> list[events.Event]:
        return [e for e in self.published if isinstance(e, kind)]


class FakeReadings:
    """Stands in for MilestonesReadings, replaying scripted frames."""

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
    return {'screen_id': None, 'error': None, 'scanned': True, 'tier': None,
            'claim_all': None, 'reward_text': None, 'currency': None, 'amount': None}


def ladder(tier: int | None, claim_all: tuple[int, int, int, int] | None, *,
          error: str | None = None,
          screen_id: str | None = milestones_claim.LADDER_SCREEN) -> dict[str, Any]:
    """A ladder-page frame. `error`/`screen_id` default to a clean read; a
    caller exercising `ladder_unreadable` or `ladder_not_reached`
    overrides one of them."""
    return {'screen_id': screen_id, 'error': error, 'scanned': True, 'tier': tier,
            'claim_all': claim_all, 'reward_text': None, 'currency': None, 'amount': None}


def modal(reward_text: str | None, currency: str | None, amount: int | None, *,
         error: str | None = None,
         screen_id: str | None = milestones_claim.MODAL_SCREEN,
         action: str = 'claim', index: int | None = None,
         total: int | None = None,
         control: tuple[int, int, int, int] | None = None) -> dict[str, Any]:
    """A reward-modal frame. `error`/`screen_id` default to a clean read."""
    return {'screen_id': screen_id, 'error': error, 'scanned': True, 'tier': None,
            'claim_all': None, 'reward_text': reward_text, 'currency': currency,
            'amount': amount, 'modal_action': action,
            'modal_index': index, 'modal_total': total,
            'modal_control': control}


def image(name: str) -> Any:
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    assert frame is not None, f'missing fixture: {name}.png'
    return frame


def screen(step: str) -> Any:
    """A real 2400x1080 capture, chosen for whichever tap this step might make.

    OPEN_MILESTONES taps the MILESTONES control, which is only on the main
    menu - menu_milestones_entry.png, matching nav/milestones.png at 1.0000.
    LADDER's own template tap (the return control, made once `Claim All` is
    absent) is located on menu_milestones_claimable.png, matching
    nav/missions_return.png at 0.9999986 - the same file and coordinate on
    both recorded ladder captures. LADDER's OTHER tap - `Claim All` - is
    never matched by template (it is tapped from the reader's own rect, see
    `milestones_claim._tap_claim_all`), so which frame stands in for it is
    not load-bearing. MODAL taps CLAIM, located on
    menu_milestones_reward_modal.png, matching nav/claim_reward.png at
    0.9999955. CONFIRM_HOME issues no tap of its own.
    """
    if step == 'open_milestones':
        return image('menu_milestones_entry')
    if step == 'modal':
        return image('menu_milestones_reward_modal')
    return image('menu_milestones_claimable')


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

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))


def drive(frames: list[dict[str, Any]], *,
         steps: int = 20) -> tuple[milestones_claim.MilestonesClaim, FakeBus, FakeDevice]:
    """Run a walk to completion against scripted frames."""
    claim = milestones_claim.MilestonesClaim()
    claim.request(now=0.)
    bus, readings = FakeBus(), FakeReadings(frames)
    templates, device = FakeTemplates(), FakeDevice()
    for _ in range(steps):
        if not claim.active:
            break
        claim.advance(screen=screen(claim.snapshot()['step']), device=device,
                      templates=templates, readings=FakePanel(), milestones=readings,
                      bus=bus, state='MAIN_MENU', now=0.)
        readings.step()
    return claim, bus, device


def test_a_walk_with_nothing_claimable_returns_home_without_tapping() -> None:
    """Nothing claimable means the walk still has to tap its own way home -
    checked against the walk actually completing and actually returning, not
    just against the absence of a claim (which an unrelated early failure,
    e.g. the MILESTONES control refusing, would satisfy just as well)."""
    claim, bus, device = drive([home(), ladder(1, None), home()])
    assert not bus.of(events.MilestoneClaimed)
    ended = bus.of(events.ClaimEnded)
    assert ended and ended[0].claimed == 0 and ended[0].aborted is False
    result = claim.snapshot()['result']
    assert result['status'] == 'completed' and result['reason'] == 'claimed'
    assert RETURN_CONTROL in device.taps


def test_the_claim_all_button_is_tapped_at_the_rects_centre() -> None:
    """The one line deciding where Claim All is actually tapped: for the
    measured rect (420, 255, 246, 53), the centre is
    (420 + 246 // 2, 255 + 53 // 2) = (543, 281). Mutating this to the
    rect's top-left, or swapping x and y, must fail here."""
    claim, _, device = drive([home(), ladder(1, CLAIM_ALL_RECT),
                              modal('25 COINS', 'coins', 25), ladder(2, None), home()])
    assert (543, 281) in device.taps


def test_the_modal_claim_button_is_tapped_at_its_template_match() -> None:
    claim, _, device = drive([home(), ladder(1, CLAIM_ALL_RECT),
                              modal('25 COINS', 'coins', 25), ladder(2, None), home()])
    assert CLAIM_MODAL_CONTROL in device.taps


def test_skip_is_never_tapped() -> None:
    """SKIP discards the reward the modal is offering, so tapping it would
    spend a claimable milestone for nothing and leave no ledger line.

    Named here rather than left to the tap-list assertions, because those pin
    the happy path's four taps by equality and would stop covering this the
    moment anyone relaxed one to a membership check. `nav/skip.png` matches
    the reward modal at 1.0000 - as well as CLAIM does - so its real
    coordinate is located with the real matcher and asserted ABSENT, rather
    than trusted to be unreachable because no code mentions it.
    """
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    frame = image('menu_milestones_reward_modal')
    found = account_collection.locate_control(frame, cache.get('nav/skip.png'), 'nav/skip.png')
    assert found.status == 'located' and found.score >= .99, (found.status, found.score)
    assert found.point is not None
    skip_point = (int(found.point[0]), int(found.point[1]))

    _, _, device = drive([home(), ladder(1, CLAIM_ALL_RECT),
                          modal('25 COINS', 'coins', 25), ladder(2, None), home()])
    assert CLAIM_MODAL_CONTROL in device.taps, 'the walk did not reach the modal at all'
    assert skip_point not in device.taps, skip_point
    # And the two really are different points, or the assertion above is vacuous.
    assert skip_point != CLAIM_MODAL_CONTROL


def test_a_claim_is_only_recorded_once_the_ladder_reappears() -> None:
    """The success test IS the modal->ladder transition. One full round trip:
    Claim All tapped, the modal names '25 COINS' at tier 1, CLAIM tapped, and
    only once the ladder reappears - one frame later, not the frame the tap
    was made on - is MilestoneClaimed published with that exact evidence."""
    claim, bus, device = drive([home(), ladder(1, CLAIM_ALL_RECT),
                                modal('25 COINS', 'coins', 25), ladder(2, None), home()])
    claimed = bus.of(events.MilestoneClaimed)
    assert len(claimed) == 1
    assert (claimed[0].reward_text, claimed[0].currency, claimed[0].amount, claimed[0].tier) == (
        '25 COINS', 'coins', 25, 1)
    result = claim.snapshot()['result']
    assert result['status'] == 'completed' and result['reason'] == 'claimed'
    assert device.taps == [MILESTONES_CONTROL, CLAIM_ALL_TAP, CLAIM_MODAL_CONTROL, RETURN_CONTROL]


def test_claim_all_walks_next_then_claim_and_records_both_rewards() -> None:
    claim, bus, device = drive([
        home(), ladder(1, CLAIM_ALL_RECT),
        modal('25 COINS', 'coins', 25, action='next', index=1, total=2,
              control=(444, 1860, 191, 63)),
        modal('10 GEMS', 'gems', 10, action='claim', index=2, total=2),
        ladder(1, None), home(),
    ])
    assert claim.snapshot()['result']['status'] == 'completed'
    assert [(e.reward_text, e.amount, e.tier) for e in bus.of(events.MilestoneClaimed)] == [
        ('25 COINS', 25, 1), ('10 GEMS', 10, 1)]
    assert bus.of(events.ClaimEnded)[0].claimed == 2
    assert device.taps == [MILESTONES_CONTROL, CLAIM_ALL_TAP,
                           (539, 1891), CLAIM_MODAL_CONTROL, RETURN_CONTROL]


def test_next_without_confirmed_following_reward_reports_uncertainty() -> None:
    claim, bus, _ = drive([
        home(), ladder(1, CLAIM_ALL_RECT),
        modal('25 COINS', 'coins', 25, action='next', index=1, total=2,
              control=(444, 1860, 191, 63)),
        modal(None, None, None, error='unreadable', action='claim', index=2, total=2),
    ])
    assert claim.snapshot()['result']['status'] == 'failed'
    assert bus.of(events.ClaimUncertain)
    assert not bus.of(events.ClaimSkipped)


def test_a_claim_with_no_currency_is_recorded_as_a_reward_that_moved_nothing() -> None:
    """`Unlock Lab` is a real reward on the recorded ladder: the modal names
    it but it moves no currency. Distinct from an unreadable modal - this is
    a successful read that simply found no coins/gems in the reward line -
    and the ledger's own MilestoneClaimed handling turns exactly this shape
    (reward_text present, currency None) into delta=0, never delta=None."""
    claim, bus, _ = drive([home(), ladder(1, CLAIM_ALL_RECT),
                           modal('Unlock Lab', None, None), ladder(2, None), home()])
    claimed = bus.of(events.MilestoneClaimed)
    assert len(claimed) == 1
    assert (claimed[0].reward_text, claimed[0].currency, claimed[0].amount) == (
        'Unlock Lab', None, None)


def test_a_claim_walk_taps_through_multiple_rounds_when_more_than_one_tier_is_completed() -> None:
    """`Claim All` reappearing after a confirmed claim means another tier is
    still completed and unclaimed - the walk must loop, not stop at one."""
    frames = [home(), ladder(1, CLAIM_ALL_RECT), modal('25 COINS', 'coins', 25),
              ladder(2, CLAIM_ALL_RECT), modal('15GEMS', 'gems', 15),
              ladder(3, None), home()]
    claim, bus, device = drive(frames, steps=30)
    claimed = bus.of(events.MilestoneClaimed)
    assert len(claimed) == 2
    assert [(c.currency, c.amount, c.tier) for c in claimed] == [
        ('coins', 25, 1), ('gems', 15, 2)]
    assert device.taps.count(CLAIM_ALL_TAP) == 2
    assert device.taps.count(CLAIM_MODAL_CONTROL) == 2
    assert RETURN_CONTROL in device.taps


def test_a_claim_walk_stops_at_its_bound_even_if_claim_all_keeps_offering() -> None:
    """`MAX_CLAIMS_PER_WALK` is a runaway backstop, not a measured capacity -
    see its own comment. Proved with the constructor override rather than by
    scripting twenty round trips: a ladder that ALWAYS offers `Claim All`
    (never runs out on its own) must still stop at exactly `max_claims`
    claims, and stop by tapping the return control and completing - not by
    failing, and not merely by giving up early or overshooting.

    The ladder frame is read off `claim.snapshot()['step']` rather than off a
    fixed frame list: a truly page-driven `Claim All` would offer forever, so
    only a walk that reads its OWN step can ever show `home()` again once the
    return control has actually been tapped.
    """
    max_claims = 3
    claim = milestones_claim.MilestonesClaim(max_claims=max_claims)
    claim.request(now=0.)
    bus = FakeBus()
    templates, device = FakeTemplates(), FakeDevice()

    class AlwaysClaimable:
        def claim_evidence(self) -> dict[str, Any]:
            step = claim.snapshot()['step']
            if step == 'modal':
                return modal('25 COINS', 'coins', 25)
            if step in ('open_milestones', 'confirm_home'):
                return home()
            return ladder(1, CLAIM_ALL_RECT)  # 'ladder', every single time

    readings = AlwaysClaimable()
    for _ in range(40):
        if not claim.active:
            break
        claim.advance(screen=screen(claim.snapshot()['step']), device=device,
                      templates=templates, readings=FakePanel(), milestones=readings,
                      bus=bus, state='MAIN_MENU', now=0.)

    claimed = bus.of(events.MilestoneClaimed)
    assert len(claimed) == max_claims
    result = claim.snapshot()['result']
    assert result['status'] == 'completed' and result['reason'] == 'claim_bound_reached'
    ended = bus.of(events.ClaimEnded)
    assert ended and ended[-1].claimed == max_claims and ended[-1].aborted is False
    assert RETURN_CONTROL in device.taps
    assert device.taps.count(CLAIM_ALL_TAP) == max_claims
    assert device.taps.count(CLAIM_MODAL_CONTROL) == max_claims


def test_a_ladder_that_never_reappears_after_claim_ends_the_walk_as_uncertain() -> None:
    """A tap that changed nothing observable is reported, not repeated.

    CLAIM was tapped in the modal, so the reward may well have been taken -
    the reflow or OCR could simply have lagged. ClaimEnded alone only carries
    a count; the ClaimUncertain this walk publishes on the way out is what
    names which reward ('25 COINS') was tapped, and it is deliberately not
    ClaimSkipped: ClaimSkipped means the walk provably moved nothing.
    """
    modal_frame = modal('25 COINS', 'coins', 25)
    frames = [home(), ladder(1, CLAIM_ALL_RECT), modal_frame] + [modal_frame] * 10
    claim, bus, _ = drive(frames, steps=20)
    result = claim.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'claim_not_confirmed'
    assert not bus.of(events.MilestoneClaimed)
    uncertain = bus.of(events.ClaimUncertain)
    assert uncertain and uncertain[-1].reason == 'claim_not_confirmed'
    assert '25 COINS' in uncertain[-1].detail
    assert not bus.of(events.ClaimSkipped)


def test_an_unreadable_ladder_is_refused_not_claimed() -> None:
    """`ladder_unreadable`: the ladder was reached but a reader error is
    reported. No `Claim All` is even considered, so no further tap is made.

    The reason is the LADDER's own, not one shared with the modal: while
    both refusals published `milestones_unreadable`, deleting this guard
    entirely still satisfied the assertion below, because the modal's
    refusal one step downstream produced the same string. Only the tap-list
    assertion had teeth. Now the reason names the site too."""
    claim, bus, device = drive([home(), ladder(1, CLAIM_ALL_RECT, error='ocr failed')])
    result = claim.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'ladder_unreadable'
    assert not bus.of(events.MilestoneClaimed)
    assert bus.of(events.ClaimSkipped)
    assert device.taps == [MILESTONES_CONTROL]


def test_an_unreadable_reward_modal_is_refused_not_claimed() -> None:
    """`modal_unreadable`: Claim All was already tapped -
    it opens the ceremony, nothing more - but CLAIM itself is never tapped
    against evidence the reader could not read, so nothing was yet granted.
    This is a refusal, not an uncertain outcome, because the ambiguous step
    (CLAIM) never fired."""
    claim, bus, device = drive([home(), ladder(1, CLAIM_ALL_RECT),
                                modal(None, None, None, error='ocr failed')])
    result = claim.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'modal_unreadable'
    assert not bus.of(events.MilestoneClaimed)
    assert bus.of(events.ClaimSkipped)
    assert not bus.of(events.ClaimUncertain)
    assert device.taps == [MILESTONES_CONTROL, CLAIM_ALL_TAP]


def test_the_open_step_never_taps_before_home_is_confirmed() -> None:
    """`home_not_confirmed`: the OPEN_MILESTONES tap must not fire from a
    frame that is not actually the main menu. Driven directly (not through
    `drive()`, which always starts every script with a genuine `home()`
    frame) so this guard is exercised rather than skipped."""
    claim = milestones_claim.MilestonesClaim()
    claim.request(now=0.)
    device = FakeDevice()
    for _ in range(8):
        claim.advance(screen=screen('ladder'), device=device, templates=FakeTemplates(),
                      readings=FakePanel(), milestones=FakeReadings([ladder(1, CLAIM_ALL_RECT)]),
                      bus=FakeBus(), state='MAIN_MENU', now=0.)
    result = claim.snapshot()['result']
    assert result['reason'] == 'home_not_confirmed'
    assert device.taps == []


def test_the_ladder_step_waits_for_its_own_screen_id_before_reading_it() -> None:
    """`ladder_not_reached`: the tap landed, but the very next frames do not
    yet show the ladder's own screen id."""
    frames = [home()] + [ladder(1, CLAIM_ALL_RECT, screen_id=None)] * 8
    claim, bus, device = drive(frames)
    result = claim.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'ladder_not_reached'
    assert not bus.of(events.MilestoneClaimed)
    assert device.taps == [MILESTONES_CONTROL]


def test_the_modal_step_waits_for_its_own_screen_id_before_tapping_claim() -> None:
    """`modal_not_reached`: Claim All was tapped, but the very next frames
    never show the reward ceremony's own screen id."""
    frames = [home(), ladder(1, CLAIM_ALL_RECT)] + [ladder(1, CLAIM_ALL_RECT)] * 8
    claim, bus, device = drive(frames)
    result = claim.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'modal_not_reached'
    assert not bus.of(events.MilestoneClaimed)
    assert device.taps == [MILESTONES_CONTROL, CLAIM_ALL_TAP]


def test_the_walk_does_not_report_completed_before_home_is_restored() -> None:
    """`home_not_restored`: once the return control is tapped, the walk must
    not report `completed` while the ladder is still evidently up."""
    claim = milestones_claim.MilestonesClaim()
    claim.request(now=0.)
    bus = FakeBus()
    readings = FakeReadings([home(), ladder(1, None)])  # never returns to home()
    templates, device = FakeTemplates(), FakeDevice()

    def step() -> None:
        claim.advance(screen=screen(claim.snapshot()['step']), device=device,
                      templates=templates, readings=FakePanel(), milestones=readings,
                      bus=bus, state='MAIN_MENU', now=0.)
        readings.step()

    for _ in range(9):
        step()
    result = claim.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'home_not_restored'
    assert RETURN_CONTROL in device.taps


def test_pausing_mid_walk_cancels_it() -> None:
    claim = milestones_claim.MilestonesClaim()
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
    claim = milestones_claim.MilestonesClaim()
    claim.request(now=0.)
    bus = FakeBus()
    readings = FakeReadings([home(), ladder(1, CLAIM_ALL_RECT),
                             modal('25 COINS', 'coins', 25), ladder(2, None)])
    templates, device = FakeTemplates(), FakeDevice()

    def step() -> None:
        claim.advance(screen=screen(claim.snapshot()['step']), device=device,
                      templates=templates, readings=FakePanel(), milestones=readings,
                      bus=bus, state='MAIN_MENU', now=0.)
        readings.step()

    step()  # taps the MILESTONES control; step becomes LADDER
    step()  # taps Claim All at (543, 281); step becomes MODAL
    step()  # taps the modal's CLAIM; `_pending` is now set; step becomes LADDER

    mid_walk = claim.snapshot()
    assert mid_walk['claimed'] == 0  # tapped, not yet confirmed by the ladder reappearing
    text = repr(mid_walk)
    assert '543' not in text and '281' not in text and 'rect' not in text

    step()  # the ladder reappears; the claim is confirmed and `_pending` clears
    text_after = repr(claim.snapshot())
    assert '543' not in text_after and '281' not in text_after and 'rect' not in text_after


def test_a_claim_walk_is_armed_at_most_once() -> None:
    """`request()` is idempotent per object: a second call while a walk is
    already active must arm nothing and report False, rather than restarting
    or stacking a second walk on the same instance."""
    claim = milestones_claim.MilestonesClaim()
    assert claim.request() is True
    assert claim.active
    assert claim.request() is False


# --- the passive guard, and the predicate all three walks share -----------

def recorded(name: str) -> tuple[Any, ...]:
    import json

    import ocr
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads(
                     (FIXTURES / 'ocr' / f'{name}.json').read_text()))


def test_the_milestones_screens_hold_actions_with_no_walk_armed(
        bot_on_main_menu: Any, monkeypatch: Any) -> None:
    """The reward modal is a full-screen overlay carrying a tappable CLAIM, and
    config.NAV_DISMISS - which shopping.py's OPEN_CARDS step walks to clear
    first-visit popups - holds nav/claim_reward.png and nav/skip.png, BOTH
    measured at 1.0000 on it. Without the guard that walk could claim a
    milestone reward with no ledger line, or tap SKIP and discard it.

    Asserted with NOTHING armed, because that is the case the guard exists for:
    a transaction-only guard would leave the modal unprotected exactly when no
    milestones walk is running.
    """
    import ocr
    from strategy import Shopping
    bot = bot_on_main_menu(Shopping(enabled=False))
    boxes = recorded('menu_milestones_reward_modal')
    bot._screen = image('menu_milestones_reward_modal')
    monkeypatch.setattr(ocr, 'read', lambda *a, **k: boxes)
    assert not bot.milestones_claim.active
    bot.run_once()
    skipped = [e for e in bot.bus.published if isinstance(e, events.Skipped)]
    assert any(e.reason == 'milestones_screen_guard' for e in skipped)
    assert bot.device.taps == []


def test_stranded_milestones_ladder_returns_to_game_after_guard_budget(
        bot_on_main_menu: Any, monkeypatch: Any) -> None:
    """After a worker restart loses its claim walk, the ladder needs an exit."""
    import ocr
    from strategy import Shopping

    bot = bot_on_main_menu(Shopping(enabled=False))
    bot.controls.apply({'tap_jitter_px': 0, 'tap_delay': 0})
    bot._screen = image('menu_milestones_claimable')
    boxes = recorded('menu_milestones_claimable')
    monkeypatch.setattr(ocr, 'read', lambda *a, **k: boxes)
    for _ in range(config.HELD_PAGE_SCAN_LIMIT + 1):
        bot.run_once()
    assert bot.device.taps == [RETURN_CONTROL]


def test_an_ordinary_menu_frame_is_not_held_by_the_milestones_guard(
        bot_on_main_menu: Any, monkeypatch: Any) -> None:
    """The guard has to be the milestones screens and not "any menu", or it
    would quietly stop the bot everywhere - the same check the missions guard
    carries, for the same reason."""
    import ocr
    from strategy import Shopping
    bot = bot_on_main_menu(Shopping(enabled=False))
    bot._screen = image('menu_main')
    monkeypatch.setattr(ocr, 'read', lambda *a, **k: ())
    bot.run_once()
    assert not [e for e in bot.bus.published
                if isinstance(e, events.Skipped)
                and e.reason == 'milestones_screen_guard']


def test_supervised_claim_walk_reaches_claim_all_and_reward_modal(
        bot_on_main_menu: Any, monkeypatch: Any) -> None:
    """Recovery must recognize the ladder before the claim reader can tap."""
    import ocr
    from strategy import Shopping
    from supervisor import RecoveryState

    class Supervisor:
        current_account = 'account-a'

        def __init__(self) -> None:
            self.seen: list[str] = []

        def observe(self, **evidence: Any) -> RecoveryState:
            self.seen.append(evidence['screen'])
            return (RecoveryState.BLOCKED if evidence['screen'] == 'UNKNOWN'
                    else RecoveryState.READY)

    bot = bot_on_main_menu(Shopping(enabled=False))
    bot.controls.apply({'tap_jitter_px': 0, 'tap_delay': 0})
    supervisor = Supervisor()
    bot.supervisor = supervisor
    entry = image('menu_milestones_entry')
    ladder_frame = image('menu_milestones_claimable')
    modal_frame = image('menu_milestones_reward_modal')
    bot._screen = entry
    def boxes_for_frame(*args: Any, **kwargs: Any) -> Any:
        fixture = ('menu_milestones_entry' if bot._screen is entry else
                   'menu_milestones_claimable' if bot._screen is ladder_frame else
                   'menu_milestones_reward_modal')
        return recorded(fixture)

    monkeypatch.setattr(ocr, 'read', boxes_for_frame)
    assert bot.milestones_claim.request()
    bot.run_once()
    assert MILESTONES_CONTROL in bot.device.taps

    bot._screen = ladder_frame
    bot.run_once()
    assert supervisor.seen[-1] == milestones_claim.LADDER_SCREEN
    assert CLAIM_ALL_TAP in bot.device.taps

    bot._screen = modal_frame
    bot.run_once()
    assert supervisor.seen[-1] == milestones_claim.MODAL_SCREEN
    assert CLAIM_MODAL_CONTROL in bot.device.taps


def test_native_1920_milestones_controls_are_located_on_that_frame() -> None:
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    cases = (
        ('menu_milestones_claimable_1920', 'MILESTONES_RETURN', (540, 1813)),
        ('menu_milestones_reward_claim_1920', None, (540, 1626)),
    )
    for name, target, point in cases:
        template = (config.NAV_TARGETS[target] if target is not None
                    else milestones_claim.CLAIM_TEMPLATE)
        found = account_collection.locate_control(image(name), cache.get(template), template)
        assert found.status == 'located', name
        assert found.point == point


def test_supervised_1920_claim_walk_uses_live_next_and_claim_positions(
        bot_on_main_menu: Any, monkeypatch: Any) -> None:
    import ocr
    from strategy import Shopping
    from supervisor import RecoveryState

    class Supervisor:
        current_account = 'account-a'

        def observe(self, **evidence: Any) -> RecoveryState:
            return (RecoveryState.BLOCKED if evidence['screen'] == 'UNKNOWN'
                    else RecoveryState.READY)

    bot = bot_on_main_menu(Shopping(enabled=False))
    bot.controls.apply({'tap_jitter_px': 0, 'tap_delay': 0})
    bot.supervisor = Supervisor()
    frames = {
        name: image(name) for name in (
            'menu_milestones_entry', 'menu_milestones_claimable_1920',
            'menu_milestones_reward_next_1920', 'menu_milestones_reward_claim_1920')
    }
    monkeypatch.setattr(ocr, 'read', lambda *a, **k: recorded(
        next(name for name, frame in frames.items() if bot._screen is frame)))
    assert bot.milestones_claim.request()
    for name in frames:
        bot._screen = frames[name]
        bot.run_once()
    assert bot.device.taps == [MILESTONES_CONTROL, (543, 281),
                               (540, 1651), (540, 1626)]


def test_a_pause_cancels_a_half_walked_milestones_claim(
        bot_on_main_menu: Any, monkeypatch: Any) -> None:
    """The third cancel site, in tower_bot's guard block - NOT in the runner,
    which drives a FakeBot with no run_once() and so cannot reach this code at
    all. Pause is the operator's stop-touching-my-device control, so it has to
    reach a walk already walking, not merely refuse to arm a new one.
    """
    import ocr
    from strategy import Shopping
    bot = bot_on_main_menu(Shopping(enabled=False))
    bot.controls.apply({'tap_jitter_px': 0, 'tap_delay': 0})
    assert bot.milestones_claim.request(now=0.) is True
    bot.controls.apply({'paused': True})
    bot._screen = image('menu_main')
    monkeypatch.setattr(ocr, 'read', lambda *a, **k: ())
    bot.run_once()
    ended = bot.milestones_claim.snapshot()
    assert ended['status'] == 'failed'
    assert ended['result']['reason'] == 'paused'


def test_the_dismiss_templates_really_do_match_this_modal() -> None:
    """The measurement the guard's whole justification rests on. If this ever
    stops being true the guard is still correct but its stated reason is not,
    and a future author would read the comment as stale rather than as
    describing a closed hazard."""
    import account_collection
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    frame = image('menu_milestones_reward_modal')
    for name in ('nav/claim_reward.png', 'nav/skip.png'):
        assert name in config.NAV_DISMISS, name
        found = account_collection.locate_control(frame, cache.get(name), name)
        assert found.status == 'located', name
        assert found.score is not None and found.score >= .99, (name, found.score)


def test_at_page_home_refuses_home_while_the_milestones_page_is_up() -> None:
    """`at_page_home` is an AND of two independent facts, and each half has to
    be load-bearing on its own.

    Without the `at_home` conjunct the predicate would answer "home" from a
    clear page reader alone - so a walk could tap the main menu while a RUN is
    live. Without the page half it would answer "home" while a milestones
    screen is still up. Both directions are asserted here because deleting
    either one is a silent change every other test on this branch survives.
    """
    import account_collection
    clear = {'scanned': True, 'screen_id': None, 'error': None}
    up = {'scanned': True, 'screen_id': 'milestones.ladder', 'error': None}
    unread = {'scanned': True, 'screen_id': None, 'error': 'ocr failed'}
    unscanned = {'scanned': False, 'screen_id': None, 'error': None}
    # `at_home`'s own shape: examined-and-clear, per its docstring.
    account = {'scanned': True, 'screen_id': None, 'error': None}

    assert account_collection.at_page_home('MAIN_MENU', account, clear) is True
    # The `at_home` half: a clear page reader cannot make a live run into home.
    assert account_collection.at_page_home('IN_RUN', account, clear) is False
    # The page half, three ways it can fail to say "nothing is up".
    assert account_collection.at_page_home('MAIN_MENU', account, up) is False
    assert account_collection.at_page_home('MAIN_MENU', account, unread) is False
    assert account_collection.at_page_home('MAIN_MENU', account, unscanned) is False


def test_at_missions_home_still_behaves_exactly_as_before() -> None:
    """The extraction has to be behaviour-preserving for the two walks that
    already shipped against it, not merely for the new one."""
    import account_collection
    clear = {'scanned': True, 'screen_id': None, 'error': None}
    up = {'scanned': True, 'screen_id': 'missions.daily', 'error': None}
    account = {'scanned': True, 'screen_id': None, 'error': None}
    assert account_collection.at_missions_home('MAIN_MENU', account, clear) is True
    assert account_collection.at_missions_home('IN_RUN', account, clear) is False
    assert account_collection.at_missions_home('MAIN_MENU', account, up) is False
