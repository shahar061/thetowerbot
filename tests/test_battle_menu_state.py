from battle_menu import Badge, IconReading
from battle_menu_state import BattleMenuState

RED, BLUE = Badge("red"), Badge("blue")


def menu(**badges):
    icons = ("cart", "missions", "cards", "labs", "event")
    return {i: IconReading(i, (0, 0), badges.get(i)) for i in icons}


def test_new_badge_is_due():
    state = BattleMenuState(None)
    assert state.due(menu(event=BLUE), now=0) == ["event"]


def test_handled_badge_waits_for_cooldown_even_if_unchanged():
    state = BattleMenuState(None)
    state.handled("event", BLUE, now=0)
    assert state.due(menu(event=BLUE), now=1799) == []
    assert state.due(menu(event=BLUE), now=1800) == ["event"]


def test_changed_badge_is_due_immediately():
    state = BattleMenuState(None)
    state.handled("cart", RED, now=0)
    assert state.due(menu(cart=BLUE), now=10) == ["cart"]


def test_unbadged_icon_never_due():
    assert BattleMenuState(None).due(menu(), now=0) == []


def test_failures_back_off_and_cap():
    state = BattleMenuState(None)
    for _ in range(10):
        state.failed("labs", RED, now=0)
    assert state.due(menu(labs=RED), now=7199) == []
    assert state.due(menu(labs=RED), now=7200) == ["labs"]


def test_session_gap():
    state = BattleMenuState(None)
    assert state.session_allowed(0)
    state.session_started(0)
    assert not state.session_allowed(179)
    assert state.session_allowed(180)


def test_worth_opening_learns_once_then_respects_cooldowns():
    state = BattleMenuState(None)
    assert state.worth_opening(0)                 # nothing known yet
    state.remember(menu(event=BLUE))
    state.handled("event", BLUE, now=0)
    assert not state.worth_opening(100)           # only a cooled-down badge
    assert state.worth_opening(1800)


def test_settings_only_is_not_worth_opening():
    state = BattleMenuState(None)
    state.remember(menu())                        # menu seen, no non-settings badge
    assert not state.worth_opening(10_000)


def test_persists_across_restart(tmp_path):
    path = tmp_path / "battle-menu-state.json"
    first = BattleMenuState(path)
    first.remember(menu(event=BLUE))
    first.handled("event", BLUE, now=100)
    second = BattleMenuState(path)
    assert second.due(menu(event=BLUE), now=200) == []
    assert not second.worth_opening(200)


def test_corrupt_file_starts_fresh(tmp_path):
    path = tmp_path / "battle-menu-state.json"
    path.write_text("{not json")
    assert BattleMenuState(path).due(menu(cart=RED), now=0) == ["cart"]
