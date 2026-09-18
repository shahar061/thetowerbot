"""Reverify one registered Tower account before a reroll worker resumes taps."""

from __future__ import annotations

import math
import time
from typing import Any, Callable

from fleet.account_creation import AccountFrame
from supervisor import DeviceSupervisor, RecoveryBlocked, RecoveryState


_CONTROLS = {
    "home": "settings",
    "settings": "account",
    "game_over": "home_from_game_over",
    "google_play_profile": "dismiss_google_play_profile",
    "workshop_tutorial_claim": "claim",
    "workshop": "battle_tab",
}
_NEXT_SCREEN = {
    "home": "settings",
    "settings": "account",
    "game_over": "home",
    "google_play_profile": "home",
    "workshop_tutorial_claim": "workshop",
    "workshop": "home",
}


def verify_restart_account(
    *, device: Any, observe: Callable[[Any], AccountFrame],
    supervisor: DeviceSupervisor, expected_account: str,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Use only observed controls to reach Account and prove the live ID.

    The worker is still blocked for normal bot actions. This bounded identity
    walk is the only permitted input until the supervisor accepts a fresh
    account observation from its current connection.
    """
    if not expected_account:
        raise RecoveryBlocked("registered account identity unavailable")
    account: AccountFrame | None = None
    awaiting: tuple[str, str] | None = None
    battle_deadline = clock() + 180
    navigation_steps = 0
    while navigation_steps < 12:
        frame = observe(device)
        if frame.conflict_dialog:
            raise RecoveryBlocked("session conflict during account verification")
        if frame.screen == "battle" and awaiting is None:
            if clock() >= battle_deadline:
                raise RecoveryBlocked("battle did not finish during account verification")
            sleep(1.)
            continue
        navigation_steps += 1
        if awaiting is not None:
            previous, destination = awaiting
            if frame.screen in {previous, "unknown"}:
                sleep(.5)
                continue
            if frame.screen != destination:
                raise RecoveryBlocked("account navigation transition unavailable")
            awaiting = None
        if frame.screen == "account":
            account = frame
            break
        control = _CONTROLS.get(frame.screen)
        if control is not None:
            if (set(frame.controls) not in ({control}, {control, "close"}) or not frame.digest
                    or not frame.evidence_ref or not math.isfinite(frame.observed_at)
                    or frame.observed_at > clock() or clock() - frame.observed_at > 5):
                raise RecoveryBlocked("account navigation evidence unavailable")
            device.click(*frame.controls[control])
            awaiting = (frame.screen, _NEXT_SCREEN[frame.screen])
        elif frame.screen != "unknown":
            raise RecoveryBlocked("account navigation screen unavailable")
        sleep(.5)
    if (account is None or account.popup_title != "ACCOUNT" or account.id_label != "ID:"
            or not account.account_id or not account.digest or not account.evidence_ref
            or not math.isfinite(account.observed_at) or account.observed_at > clock()
            or clock() - account.observed_at > 5):
        raise RecoveryBlocked("fresh account popup evidence unavailable")
    supervisor.verify_account(account.account_id, observed_at=account.observed_at)
    if (account.account_id != expected_account
            or supervisor.observe(frame_digest=account.digest,
                                  observed_at=account.observed_at, screen="account",
                                  account_id=account.account_id) is not RecoveryState.READY):
        raise RecoveryBlocked("account recovery evidence blocked")

    # Resolve each close control from the observed frame. The nested dialogs
    # move with the emulator height, so the 2400-height points are not safe.
    if set(account.controls) not in ({"close"}, {"new_account", "close"}):
        raise RecoveryBlocked("account close control unavailable")
    device.click(*account.controls["close"])
    for _ in range(6):
        frame = observe(device)
        if frame.conflict_dialog:
            raise RecoveryBlocked("session conflict after account verification")
        if frame.screen == "settings":
            break
        if frame.screen not in {"account", "unknown"}:
            raise RecoveryBlocked("account dialog close was not verified")
        sleep(.5)
    else:
        raise RecoveryBlocked("account dialog close timed out")
    if set(frame.controls) not in ({"close"}, {"account", "close"}):
        raise RecoveryBlocked("settings close control unavailable")
    device.click(*frame.controls["close"])
    for _ in range(6):
        frame = observe(device)
        if frame.conflict_dialog:
            raise RecoveryBlocked("session conflict after account verification")
        if frame.screen == "home":
            return
        if frame.screen not in {"settings", "unknown"}:
            raise RecoveryBlocked("settings dialog close was not verified")
        sleep(.5)
    raise RecoveryBlocked("settings dialog close timed out")
