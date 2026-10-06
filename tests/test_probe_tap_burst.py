"""The tap burst probe never touches a device it must not drive."""
from __future__ import annotations

import pytest

import config
from fleet.runtime import reserve_endpoint
from tools import probe_tap_burst

ENDPOINT = "127.0.0.1:65011"  # no emulator listens here


@pytest.fixture
def connects(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(probe_tap_burst, "connect_device", lambda **kw: calls.append(kw))
    return calls


def test_the_probe_refuses_an_endpoint_a_worker_holds(
        connects: list[tuple], capsys: pytest.CaptureFixture[str]) -> None:
    with reserve_endpoint(ENDPOINT):
        assert probe_tap_burst.main([ENDPOINT, "attack_speed"]) == 1
    assert connects == []
    assert "stop the worker" in capsys.readouterr().err.lower()


def test_the_probe_caps_taps_at_the_burst_maximum(connects: list[tuple]) -> None:
    with pytest.raises(SystemExit) as raised:
        probe_tap_burst.main([ENDPOINT, "attack_speed", "--taps", str(config.BATTLE_BURST_MAX + 1)])
    assert raised.value.code == 2 and connects == []


def test_the_probe_names_an_unknown_upgrade(
        connects: list[tuple], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        probe_tap_burst.main([ENDPOINT, "atack_speed"])
    assert raised.value.code == 2 and connects == []
    assert "atack_speed" in capsys.readouterr().err
