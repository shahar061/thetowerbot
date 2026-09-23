"""Serialized OCR battle navigation with observable, acknowledged purchases."""
from __future__ import annotations

from account_state import AccountState

import copy
import hashlib
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

import cv2

import config
from geometry import supported_frame
import events
import ocr
import upgrades
from device import Image, tap
from combat_context import CombatContext, RunIdentity
from perception import Observation, ObservedUpgrade, observe_frame
from policy import AutopilotPolicy, UpgradeRule, choose


class AutopilotState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, str], dict] = {}
        # The run and build each cached row was read under, kept beside the
        # rows rather than inside them: the rows are deep-copied into the
        # dashboard payload, and identity is bookkeeping, not an observation
        # anybody reads off the screen.
        self._origins: dict[tuple[str, str], RunIdentity] = {}
        self._view: dict[str, Any] = dict(
            phase="idle", reason="Autopilot is off", next_upgrade_id=None,
            category=None, combat={}, updated_at=None, verified_purchases=0, last_purchase=None,
        )

    def observe(self, observation: Observation, identity: RunIdentity = RunIdentity()) -> None:
        with self._lock:
            for row in observation.rows:
                self._rows[(row.context, row.upgrade_id)] = row.payload()
                self._origins[(row.context, row.upgrade_id)] = identity
            self._view.update(category=observation.category, updated_at=observation.observed_at)
            if not observation.rows or any(r.context == "battle" for r in observation.rows):
                self._view["combat"] = dict(observation.combat)

    def rows(self, context: str, now: float,
             identity: RunIdentity | None = None) -> dict[str, dict]:
        """Cached rows for `context`, with anything stale reduced to unknown.

        Two independent expiries, and the second is why identity is tracked at
        all: age alone cannot notice that the run restarted or the build
        changed a second ago, and a price cached under the previous run buys
        the wrong thing at full confidence. Expiry blanks the value and the
        price rather than zeroing them - an expired row is a row nobody has
        read yet, not a free upgrade.
        """
        with self._lock:
            result = {key[1]: dict(row) for key, row in self._rows.items() if key[0] == context}
            origins = {key[1]: self._origins.get(key, RunIdentity())
                       for key in self._rows if key[0] == context}
        for upgrade_id, row in result.items():
            changed_run = identity is not None and origins[upgrade_id].differs_from(identity)
            if changed_run or now - row["observed_at"] > 60:
                row.update(status="unknown", value=None, price=None)
        return result

    def unknown(self, entry: upgrades.Upgrade, context: str, now: float) -> None:
        with self._lock:
            self._rows[(context, entry.id)] = dict(
                upgrade_id=entry.id, name=entry.name, category=entry.category,
                context=context, status="unknown", value=None, price=None, observed_at=now,
            )

    def decision(self, phase: str, reason: str, target: str | None = None) -> None:
        with self._lock:
            self._view.update(phase=phase, reason=reason, next_upgrade_id=target)

    def verified(self, row: ObservedUpgrade) -> None:
        with self._lock:
            self._view["verified_purchases"] += 1
            self._view["last_purchase"] = row.payload()

    def clear_battle(self) -> None:
        with self._lock:
            self._rows = {k: v for k, v in self._rows.items() if k[0] != "battle"}
            self._origins = {k: v for k, v in self._origins.items() if k[0] != "battle"}
            self._view.update(combat={}, category=None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy({**self._view, "observations": list(self._rows.values())})


def battle_tab_point(screen: Image, category: str) -> tuple[int, int] | None:
    """Three icon tabs, calibrated on both measured portrait heights.

    Verify the three cell borders before using their centers. A fourth tab or
    changed layout fails closed. Arrival is checked from OCR on the next frame.
    """
    h, w = screen.shape[:2]
    if not supported_frame(w, h) or category not in ("ATTACK", "DEFENSE", "UTILITY"):
        return None
    grey = cv2.cvtColor(screen[h-80:h-20], cv2.COLOR_BGR2GRAY)
    for x in (0, w//3, 2*w//3, w-1):
        strip = grey[:, max(0, x-14):min(w, x+15)]
        if not (strip > 100).mean(axis=0).max() > .65:
            return None
    index = ("ATTACK", "DEFENSE", "UTILITY").index(category)
    return (int(w * (index + .5) / 3), h - 50)


def scroll_panel(device: Any, screen: Image, heading_y: int, *, down: bool) -> None:
    h, w = screen.shape[:2]
    # Swipe inside the label column, clear of the purchase buttons and tab bar.
    top, bottom = heading_y + 90, h - 160
    if bottom - top < 100:
        return
    start, end = (bottom, top) if down else (top, bottom)
    device.swipe(w // 5, start, w // 5, end, .35)


@dataclass
class Search:
    target: str
    direction: str = "up"
    scrolls: int = 0
    fingerprint: tuple[str, ...] | None = None
    tab_attempts: int = 0


# How long an already-published decision stays quiet if the autopilot comes
# back to it: long enough to absorb two states alternating every scan.
DECISION_REPEAT_S = 30.


class BattleAutopilot:
    def __init__(self, state: AutopilotState | None = None, bus: Any | None = None,
                 account_state: AccountState | None = None) -> None:
        self.state = state or AutopilotState()
        # Every battle fact this planner decides from, observed separately and
        # bound to the run and build it came from. Purchases read their cash
        # through it, which is what stops a dropped wallet read from becoming
        # a zero balance or a stale one from becoming a purchase.
        self.context = CombatContext()
        self.account_state = account_state
        self.bus = bus
        self.pending: tuple[ObservedUpgrade, float] | None = None
        self.search: Search | None = None
        # The last frame's rows as overlay boxes, in the schema
        # frames.set_boxes() takes. Kept here rather than returned from
        # step() because the loop needs them on every pass, including the
        # ones that decide to buy nothing - a blank overlay and a bot that
        # is not looking at anything are different things, and the device
        # view is where you tell them apart.
        self.boxes: list[dict[str, Any]] = []
        self._policy: AutopilotPolicy | None = None
        self._blocked: dict[str, float] = {}
        self._last_action = float("-inf")
        self._command_lock = threading.Lock()
        self._queued: dict | None = None
        self._manual: dict | None = None
        self._decided: tuple[str, str, str | None] | None = None
        self._decided_at: dict[tuple[str, str, str | None], float] = {}

    @property
    def has_work(self) -> bool:
        with self._command_lock:
            return bool(self._queued or self._manual or self.pending)

    def submit(self, command: dict, *, now: float | None = None) -> None:
        with self._command_lock:
            if self._queued is not None:
                raise ValueError("A manual command is already queued")
            action = command.get("action")
            if action not in ("buy", "category", "scan"):
                raise ValueError("Unknown manual action")
            if action == "buy":
                entry = upgrades.by_id(command.get("upgrade_id", ""))
                if entry is None or entry.unlock:
                    raise ValueError("Choose a standard battle upgrade")
            if action == "category" and command.get("category") not in ("ATTACK", "DEFENSE", "UTILITY"):
                raise ValueError("Unknown category")
            self._queued = {**command, "expires": (time.time() if now is None else now) +
                            (180 if action == "scan" else 20)}

    def suspend(self, reason: str, *, clear_battle: bool = False) -> None:
        self.search = None
        self._manual = None
        with self._command_lock:
            self._queued = None
        self._decide("idle", reason)
        if clear_battle:
            self.pending = None
            self.state.clear_battle()
            self.context.clear()
            self._blocked.clear()

    def _emit(self, event: events.Event) -> None:
        if self.bus is not None:
            self.bus.publish(event)

    def _decide(self, phase: str, reason: str, target: str | None = None) -> None:
        """Show the decision and publish it when it changes.

        A decision seen in the last DECISION_REPEAT_S is not published again,
        so two states alternating every scan add two rows, not one per scan.
        """
        self.state.decision(phase, reason, target)
        key = (phase, reason, target)
        now = time.time()
        if key != self._decided and now - self._decided_at.get(key, float("-inf")) >= DECISION_REPEAT_S:
            self._emit(events.AutopilotDecided(phase=phase, reason=reason, upgrade_id=target))
            self._decided_at[key] = now
        self._decided = key

    def _seek(self, target: str, observation: Observation, screen: Image,
              device: Any, policy: AutopilotPolicy) -> bool:
        entry = upgrades.by_id(target)
        if self.search is None or self.search.target != target:
            self.search = Search(target)
        search = self.search
        if observation.category != entry.category:
            if search.tab_attempts >= 2:
                self._blocked[target] = observation.observed_at + 60
                self._decide("blocked", "Could not confirm category; waiting before retry", target)
                self.search = None
                return False
            point = battle_tab_point(screen, entry.category)
            if point is None:
                self._decide("blocked", "Category navigation layout is not recognized", target)
                self._blocked[target] = observation.observed_at + 60
                return False
            tap(device, *point)
            search.tab_attempts += 1
            self._decide("navigating", f"Opening {entry.category.title()}", target)
            return True
        fingerprint = tuple(r.upgrade_id for r in observation.rows)
        at_end = fingerprint == search.fingerprint or search.scrolls >= policy.max_scrolls
        if at_end:
            if search.direction == "up":
                search.direction, search.scrolls = "down", 0
            else:
                if target not in {r.upgrade_id for r in observation.rows}:
                    self.state.unknown(entry, "battle", observation.observed_at)
                    self._blocked[target] = observation.observed_at + 60
                self.search = None
                self._decide("discovering", f"{entry.name} was not found; availability is unknown", target)
                return False
        search.fingerprint = fingerprint
        if observation.heading_y is None or not observation.rows:
            self._decide("blocked", "Upgrade panel is unreadable", target)
            return False
        scroll_panel(device, screen, observation.heading_y, down=search.direction == "down")
        search.scrolls += 1
        self._decide("discovering", f"Scanning {entry.category.title()} for {entry.name}", target)
        return True

    def _draw(self, observation: Observation) -> None:
        """Record this frame's rows for the device view.

        Every row, not only the one about to be bought: the legacy matcher
        recorded a box "whether or not the tap happens, because matched but
        rejected is exactly what you open the device view to see", and that
        is even truer here, where a row can be passed over for a target
        already met, an unreadable value or a price above the reserve.

        `tapped` is left False for all of them; `_mark_tapped` sets it on
        the one row a purchase actually goes to, after the decision.
        """
        self.boxes = [
            {
                "name": row.name,
                "x": int(row.rect.x),
                "y": int(row.rect.y),
                "w": int(row.rect.w),
                "h": int(row.rect.h),
                "tap_x": int(row.tap[0]) if row.tap else int(row.rect.x + row.rect.w // 2),
                "tap_y": int(row.tap[1]) if row.tap else int(row.rect.y + row.rect.h // 2),
                "score": float(row.confidence),
                "tapped": False,
            }
            for row in observation.rows
        ]

    def _mark_tapped(self, row: ObservedUpgrade) -> None:
        for box in self.boxes:
            if box["name"] == row.name:
                box["tapped"] = True

    def step(self, screen: Image, device: Any, policy: AutopilotPolicy, *,
             cash: int | None = None, observation: Observation | None = None,
             run_id: int | None = None, cooldown: float = .75,
             identity: RunIdentity = RunIdentity(), elapsed: float | None = None,
             reads: ocr.FrameReads | None = None) -> bool:
        # The scan's shared OCR and digest, when they belong to this screen.
        if reads is not None and reads.screen is not screen:
            reads = None
        observation = observation or observe_frame(screen, "battle", reads=reads)
        changed_identity = self.context.rebind(identity)
        if changed_identity:
            # A confirmation or search started under another run/build cannot
            # be finished using the new one's rows, even if its price matches.
            self.pending = None
            self.search = None
            self._blocked.clear()
            self._last_action = float("-inf")
            self.state.clear_battle()
            with self._command_lock:
                self._manual = None
                self._queued = None
        digest = reads.digest if reads is not None else hashlib.sha256(screen.tobytes()).hexdigest()
        if (observation.frame_digest != digest
                or (observation.frame_width, observation.frame_height)
                != (screen.shape[1], screen.shape[0])):
            self.boxes = []
            self._decide("blocked", "Observation does not match the current screen")
            return False
        self._draw(observation)
        if self.account_state is not None:
            self.account_state.observe_run(observation, run_id)
        now = observation.observed_at
        # Before any branch below can return: a frame that arrives under a
        # different run or build expires what the previous one left behind,
        # whether or not this step gets as far as deciding anything.
        self.context.observe(observation, identity=identity, elapsed=elapsed,
                             cash=cash if cash is not None else observation.cash, now=now)
        if changed_identity:
            self._decide("blocked", "Run or build changed; waiting for another frame")
            return False
        with self._command_lock:
            if self._manual is None and not self.pending and self._queued:
                self._manual, self._queued = self._queued, None
                self.search = None
        if self._manual and self._manual["expires"] < now:
            self._manual = None
            self.search = None
            self._decide("idle", "Manual command expired")
            return False
        if self._manual and self._manual["action"] == "buy":
            policy = replace(policy, enabled=True, preset="manual",
                             rules=(UpgradeRule(self._manual["upgrade_id"]),))
        if not policy.enabled and not self._manual and not self.pending:
            self.suspend("Autopilot is off")
            return False
        self.state.observe(observation, identity)
        if self._policy != policy:
            self.search = None
            self._policy = policy
        visible = {r.upgrade_id: r for r in observation.rows}
        if self.pending:
            before, sent_at = self.pending
            after = visible.get(before.upgrade_id)
            if now <= sent_at:
                return False
            confirmed = after and (
                after.status == "maxed" or
                (after.price is not None and before.price is not None and after.price > before.price) or
                (after.value is not None and before.value is not None and after.value != before.value)
            )
            # A manual buy is one purchase; the policy above is still its
            # single-rule stand-in, so falling through would buy it again.
            was_manual = self._manual is not None
            if confirmed:
                self.state.verified(after)
                self._emit(events.BattlePurchased(item=after.name, upgrade_id=after.upgrade_id,
                                                  price=before.price, value=after.value))
                self._decide("verified", f"Verified {after.name} upgrade", after.upgrade_id)
                self.pending = None
                self._manual = None
                # No return: the frame that proves the last purchase already
                # shows the new prices and cash, so it can pick the next one.
                # Stopping here spent a whole scan per purchase doing nothing.
            elif now - sent_at >= 8:
                self._blocked[before.upgrade_id] = now + 60
                self._decide("blocked", f"{before.name} purchase was not confirmed", before.upgrade_id)
                self.pending = None
                self._manual = None
            else:
                self._decide("verifying", f"Checking {before.name} purchase", before.upgrade_id)
            if not confirmed or was_manual:
                return False
        if not observation.category or not observation.rows:
            self._decide("blocked", "Waiting for a readable upgrade panel")
            return False
        if now - self._last_action < max(.75, cooldown):
            return False
        if self._manual and self._manual["action"] in ("category", "scan"):
            command = self._manual
            categories = command.setdefault("categories", ["ATTACK", "DEFENSE", "UTILITY"])
            category = command.get("category") if command["action"] == "category" else categories[0]
            if command["action"] == "category" and observation.category == category:
                self._manual = None
                self._decide("manual", f"Showing {category.title()}")
                return False
            target = next(e.id for e in upgrades.CATALOG if e.category == category and not e.unlock)
            moved = self._seek(target, observation, screen, device, policy)
            if moved:
                self._last_action = now
            elif self.search is None:
                if command["action"] == "scan":
                    categories.pop(0)
                if command["action"] == "category" or not categories:
                    self._manual = None
                    self._decide("manual", "Scan complete; unseen upgrades remain unknown")
            return moved
        cached = self.state.rows("battle", now, identity)
        enabled = [r for r in policy.effective_rules() if r.enabled and self._blocked.get(r.upgrade_id, 0) <= now]
        if not enabled:
            self._decide("waiting", "No eligible rules; enable upgrades or wait for a fresh scan")
            return False
        # Fresh facts only, each aged on its own clock. A fact that is
        # unknown, unreadable, expired or unsupported is simply absent here,
        # and the guides refuse to decide without the keys they need - which
        # is the whole reason none of them is ever filled in with a zero.
        combat = self.context.combat(now)
        wallet = self.context.reading("cash", now)
        actual_cash = int(wallet.value) if wallet.known and isinstance(wallet.value, (int, float)) else None
        decision = choose(replace(policy, rules=tuple(enabled)), cached, combat)
        # Discover the configured inventory even when a guide must wait for stats.
        missing = next((r.upgrade_id for r in enabled if r.upgrade_id not in cached
                        or cached[r.upgrade_id]["status"] == "unknown"), None)
        if missing and missing not in visible and decision.phase != "survival":
            moved = self._seek(missing, observation, screen, device, policy)
            if moved:
                self._last_action = now
            return moved
        target = decision.upgrade_id
        self._decide(decision.phase, decision.reason, target)
        if not target:
            return False
        if target not in visible:
            moved = self._seek(target, observation, screen, device, policy)
            if moved:
                self._last_action = now
            return moved
        row = visible[target]
        self.search = None
        if row.status != "available" or row.price is None or row.tap is None:
            return False
        refusal = self.context.refuse("This purchase", ("cash",), now=now)
        if refusal is not None or actual_cash is None:
            # The wallet is decision-critical and has no safe default: no
            # reading means no purchase, however affordable the price looks.
            self._decide("blocked", refusal or "This purchase is held: cash is unreadable", target)
            return False
        if row.price > actual_cash - policy.cash_reserve:
            self._decide("saving", f"Saving cash for {row.name}; reserve protected", target)
            return False
        tap(device, *row.tap)
        self._mark_tapped(row)
        self.pending = (row, now)
        self._last_action = now
        self._decide("verifying", f"Checking {row.name} purchase", target)
        self._emit(events.Tapped(action=row.name, x=row.tap[0], y=row.tap[1], score=1,
                                 price=row.price, wallet=actual_cash))
        return True
