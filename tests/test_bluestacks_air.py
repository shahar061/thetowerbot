from __future__ import annotations

from dataclasses import replace
import base64
import os
from pathlib import Path
import re
import sys
from typing import Callable

import numpy as np
import pytest
import cv2

from bluestacks import HostCapabilityError, HostInstance
from config import Rect
from fleet.bluestacks_air import (
    BlueStacksAirDriver,
    BlueStacksAirInventory,
    MacOSMultiInstanceManager,
    ManagerFrame,
    ManagerRowObservation,
    ModalFrame,
    ModalFrameVerifier,
    _SwiftManagerBridge,
)
from fleet.clone_qualification import QualificationScope
from ocr import TextBox


EXECUTABLE = Path("/Applications/BlueStacks.app/Contents/MacOS/BlueStacks")
MANAGER_EXECUTABLE = Path(
    "/Applications/BlueStacks Air multi-instance manager.app/Contents/MacOS/"
    "BlueStacks Air multi-instance manager"
)


@pytest.fixture(autouse=True)
def supported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")


class Manager:
    def __init__(self, game_version: str = "29.0.2") -> None:
        self.game_version = game_version
        self.source_version = "image-1"


class AttestedManager(Manager):
    profile_version = "BlueStacks-Air-2"

    def capture(self) -> ManagerFrame:
        return ManagerFrame(41, np.zeros((10, 10, 3), dtype=np.uint8))


@pytest.mark.skipif(not os.environ.get("BLUESTACKS_AIR_LIVE"), reason="real host only")
def test_live_manager_observes_named_clone_controls() -> None:
    """Observe the expected manager row without pressing any host control."""
    observation = ManagerRowObservation(MacOSMultiInstanceManager())

    frame = observation.capture()

    assert isinstance(frame, ManagerFrame)
    assert observation.row_control("Tiramisu64_2", action="Start") is not None


def test_capabilities_require_current_scope_profile_manager_and_adb_evidence(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    scope = replace(scope_for(inventory), instance_config={
        "source_lease": inventory.lease("Air_2"), "source_instance": "Air_2",
        "source_endpoint": "127.0.0.1:5595",
    })
    manager = AttestedManager()
    driver = BlueStacksAirDriver(scope, inventory, manager, lineage_path=tmp_path / "lineage.json",
                                 endpoint_present=lambda endpoint: endpoint == "127.0.0.1:5595")

    assert driver.qualification_scope() == scope
    assert driver.supports_lifecycle is True
    assert driver.supports_clone_staging is True
    assert driver.supports_m05_live_qualification is True

    manager.profile_version = "BlueStacks-Air-3"
    assert driver.supports_lifecycle is False
    assert driver.supports_clone_staging is False
    assert driver.supports_m05_live_qualification is False


@pytest.mark.parametrize("evidence", ["adb", "manager", "host"])
def test_capabilities_fail_closed_when_fresh_host_evidence_is_missing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str) -> None:
    inventory = configured_inventory(tmp_path)
    scope = scope_for(inventory)
    manager = AttestedManager()
    endpoint_present = lambda endpoint: endpoint == "127.0.0.1:5595"
    if evidence == "adb":
        endpoint_present = lambda _: False
    elif evidence == "manager":
        manager.capture = lambda: (_ for _ in ()).throw(HostCapabilityError("Accessibility unavailable"))
    elif evidence == "host":
        monkeypatch.setattr(sys, "platform", "linux")
    driver = BlueStacksAirDriver(scope, inventory, manager, endpoint_present=endpoint_present)

    assert driver.supports_lifecycle is False
    assert driver.supports_clone_staging is False
    assert driver.supports_m05_live_qualification is False


def test_capabilities_require_current_source_app_version(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    scope = replace(scope_for(inventory), instance_config={
        "source_lease": inventory.lease("Air_2"), "source_instance": "Air_2",
        "source_endpoint": "127.0.0.1:5595",
    })
    manager = AttestedManager()
    driver = BlueStacksAirDriver(scope, inventory, manager, lineage_path=tmp_path / "lineage.json",
                                 endpoint_present=lambda endpoint: endpoint == "127.0.0.1:5595")

    assert driver.supports_m05_live_qualification is True
    manager.source_version = "image-2"
    assert driver.qualification_scope() is None
    assert driver.supports_lifecycle is False
    assert driver.supports_clone_staging is False
    assert driver.supports_m05_live_qualification is False


def test_live_capability_requires_stable_source_scope_and_lineage_record(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    manager = AttestedManager()
    endpoint = lambda value: value == "127.0.0.1:5595"
    legacy = BlueStacksAirDriver(scope_for(inventory), inventory, manager,
                                 endpoint_present=endpoint)
    assert legacy.supports_clone_staging is True
    assert legacy.supports_m05_live_qualification is False

    stable_scope = replace(scope_for(inventory), instance_config={
        "source_lease": inventory.lease("Air_2"), "source_instance": "Air_2",
        "source_endpoint": "127.0.0.1:5595",
    })
    no_lineage = BlueStacksAirDriver(stable_scope, inventory, manager,
                                     endpoint_present=endpoint)
    assert no_lineage.supports_m05_live_qualification is False


class FakeManagerBridge:
    def __init__(self, *, title: str, owner_path: str = str(EXECUTABLE),
                 moved: bool = False) -> None:
        self.title = title
        self.owner_path = owner_path
        self.moved = moved
        self.inspect_calls = 0
        self.capture_calls = 0
        self.press_calls = 0

    def inspect(self) -> dict[str, int | float | str]:
        self.inspect_calls += 1
        return {"id": 42 if self.moved and self.inspect_calls > 1 else 41,
                "title": self.title, "owner_path": self.owner_path,
                "pid": 900,
                "x": 100., "y": 200., "width": 800., "height": 600.}

    def capture(self, metadata: dict[str, int | float | str]) -> np.ndarray:
        self.capture_calls += 1
        return np.zeros((10, 10, 3), dtype=np.uint8)

    def press(self, metadata: dict[str, int | float | str], point: tuple[int, int],
              expected_label: str) -> None:
        self.press_calls += 1


class FakeModalBridge(FakeManagerBridge):
    def __init__(self, *, modal_rows: list[dict[str, int | float | str]],
                 main_owner_path: str = str(MANAGER_EXECUTABLE),
                 main_title: str = "BlueStacks Air Manager") -> None:
        super().__init__(title=main_title, owner_path=main_owner_path)
        self.modal_rows = modal_rows
        self.modal_capture_calls = 0
        self.modal_clone_calls: list[tuple[dict[str, int | float | str],
                                           dict[str, int | float | str], tuple[float, float]]] = []
        self.modal_fresh_calls: list[tuple[float, float]] = []
        self.modal_create_calls: list[tuple[float, float]] = []

    def inspect_modal(self, parent: dict[str, int | float | str]) -> list[dict[str, int | float | str]]:
        return self.modal_rows

    def capture_modal(self, parent: dict[str, int | float | str],
                      modal: dict[str, int | float | str]) -> np.ndarray:
        self.modal_capture_calls += 1
        return np.zeros((400, 400, 3), dtype=np.uint8)

    def press_modal_clone(self, parent: dict[str, int | float | str],
                          modal: dict[str, int | float | str],
                          point: tuple[float, float]) -> None:
        self.modal_clone_calls.append((dict(parent), dict(modal), point))

    def press_modal_fresh(self, parent: dict[str, int | float | str],
                          modal: dict[str, int | float | str],
                          point: tuple[float, float]) -> None:
        self.modal_fresh_calls.append(point)

    def press_modal_create(self, parent: dict[str, int | float | str],
                           modal: dict[str, int | float | str],
                           point: tuple[float, float]) -> None:
        self.modal_create_calls.append(point)


class RevalidatingModalBridge(FakeModalBridge):
    def __init__(self, *, fresh_parent_pid: int = 900, fresh_modal_pid: int = 900) -> None:
        super().__init__(modal_rows=[modal_row()])
        self.fresh_parent_pid = fresh_parent_pid
        self.fresh_modal_pid = fresh_modal_pid

    def capture_modal(self, parent: dict[str, int | float | str],
                      modal: dict[str, int | float | str]) -> np.ndarray:
        if parent["pid"] != self.fresh_parent_pid or modal["pid"] != self.fresh_modal_pid:
            raise HostCapabilityError("manager modal changed")
        return super().capture_modal(parent, modal)


class MovingParentModalBridge(FakeModalBridge):
    def __init__(self, *, perpetual: bool) -> None:
        super().__init__(modal_rows=[])
        self.perpetual = perpetual
        self.modal_inspect_calls = 0

    def inspect(self) -> dict[str, int | float | str]:
        self.inspect_calls += 1
        window_id = 42 if self.inspect_calls > 1 else 41
        return {"id": window_id, "title": self.title, "owner_path": self.owner_path,
                "pid": 900, "x": 100., "y": 200., "width": 800., "height": 600.}

    def inspect_modal(self, parent: dict[str, int | float | str]) -> list[dict[str, int | float | str]]:
        self.modal_inspect_calls += 1
        if self.perpetual or self.modal_inspect_calls == 1:
            raise HostCapabilityError("manager window changed")
        return [modal_row(parent_id=int(parent["id"]))]


def modal_row(*, window_id: int = 52, title: str = "", pid: int = 900,
              parent_id: int = 41, x: float = 300., y: float = 300.,
              width: float = 400., height: float = 400.) -> dict[str, int | float | str]:
    return {"id": window_id, "title": title, "pid": pid, "parent_id": parent_id,
            "x": x, "y": y, "width": width, "height": height}


def test_native_modal_capture_forwards_inspected_pids_and_bounds() -> None:
    class RecordingSwiftBridge(_SwiftManagerBridge):
        arguments: tuple[str, ...] = ()

        def _run(self, *args: str) -> str:
            self.arguments = args
            encoded, image = cv2.imencode(".png", np.zeros((2, 2, 3), dtype=np.uint8))
            assert encoded is True
            return base64.b64encode(image.tobytes()).decode("ascii")

    bridge = RecordingSwiftBridge()
    parent = {"id": 41, "pid": 900, "x": 1., "y": 2., "width": 3., "height": 4.}
    modal = {"id": 52, "pid": 900, "title": "",
             "x": 5., "y": 6., "width": 7., "height": 8.}

    bridge.capture_modal(parent, modal)

    assert bridge.arguments == ("modal-capture", "41", "900", "1.0", "2.0", "3.0", "4.0",
                                "52", "900", "5.0", "6.0", "7.0", "8.0")


def test_native_modal_clone_press_forwards_pinned_windows_and_point() -> None:
    class RecordingSwiftBridge(_SwiftManagerBridge):
        arguments: tuple[str, ...] = ()

        def _run(self, *args: str) -> str:
            self.arguments = args
            return "pressed"

    bridge = RecordingSwiftBridge()
    parent = {"id": 41, "pid": 900, "x": 1., "y": 2., "width": 3., "height": 4.}
    modal = {"id": 52, "pid": 900, "title": "",
             "x": 5., "y": 6., "width": 7., "height": 8.}

    bridge.press_modal_clone(parent, modal, (9.5, 10.5))

    assert bridge.arguments == ("modal-press-clone", "41", "900", "1.0", "2.0", "3.0", "4.0",
                                "52", "900", "5.0", "6.0", "7.0", "8.0", "9.5", "10.5")


def test_native_modal_clone_uses_the_hid_event_path() -> None:
    """BlueStacks accepts HID events where its session-level clicks are ignored."""
    source = (Path(__file__).parents[1] / "fleet" / "macos_bluestacks_manager.swift").read_text()

    assert ".cghidEventTap" in source
    assert ".cgSessionEventTap" not in source


def test_native_modal_discovery_binds_focused_manager_modal_above_layer_zero() -> None:
    """The active sheet is selected by AX focus even when old CG windows remain."""
    source = (Path(__file__).parents[1] / "fleet" / "macos_bluestacks_manager.swift").read_text()

    modal_section = source[source.index("func exactModal"):source.index("func verifyParentForModalCapture")]
    assert "kAXFocusedWindowAttribute" in modal_section
    assert "focusedMatches.max" in modal_section
    assert "layer.intValue == 0" not in modal_section


def test_native_stop_binds_its_confirmation_to_the_exact_close_instance_dialog() -> None:
    """The native bridge must not leave BlueStacks' Stop confirmation unhandled."""
    source = (Path(__file__).parents[1] / "fleet" / "macos_bluestacks_manager.swift").read_text()

    assert 'attribute($0, kAXTitleAttribute) as? String == "Close instance"' in source
    assert 'namedPressableButtons(dialogs[0], "Close")' in source
    assert 'if args[9] == "Stop" { confirmStopDialog(current) }' in source


def test_native_upgrade_dismissal_binds_the_popup_to_one_named_player() -> None:
    """A source-instance popup must never be dismissed while targeting a clone."""
    source = (Path(__file__).parents[1] / "fleet" / "macos_bluestacks_manager.swift").read_text()

    assert 'func dismissUpgrade(forPlayerNamed playerTitle: String)' in source
    assert 'row[kCGWindowName as String] as? String == playerTitle' in source
    assert 'row[kCGWindowName as String] as? String == "Upgrade available"' in source
    assert 'player.frame.contains(frame)' in source
    assert 'closeButtonAtTopRight(matches[0], dialogs[0])' in source


@pytest.mark.parametrize("bridge", [
    RevalidatingModalBridge(fresh_parent_pid=901),
    RevalidatingModalBridge(fresh_modal_pid=901),
])
def test_manager_rejects_native_parent_or_modal_pid_drift(
        bridge: RevalidatingModalBridge) -> None:
    with pytest.raises(HostCapabilityError, match="modal"):
        MacOSMultiInstanceManager(bridge).capture_modal()

    assert bridge.modal_capture_calls == 0
    assert bridge.press_calls == 0


def test_manager_captures_one_centered_untitled_same_owner_popup() -> None:
    bridge = FakeModalBridge(modal_rows=[modal_row()])

    frame = MacOSMultiInstanceManager(bridge).capture_modal()

    assert frame.window_id == 52
    assert frame.parent_window_id == 41
    assert frame.title == ""
    assert frame.image.shape == (400, 400, 3)
    assert bridge.modal_capture_calls == 1
    assert bridge.press_calls == 0


def test_manager_retries_one_parent_movement_with_fresh_modal_evidence() -> None:
    bridge = MovingParentModalBridge(perpetual=False)

    frame = MacOSMultiInstanceManager(bridge).capture_modal()

    assert frame.parent_window_id == 42
    assert bridge.modal_inspect_calls == 2
    assert bridge.modal_capture_calls == 1
    assert bridge.press_calls == 0


def test_manager_rejects_perpetual_parent_movement_without_capture() -> None:
    bridge = MovingParentModalBridge(perpetual=True)

    with pytest.raises(HostCapabilityError, match="manager window changed"):
        MacOSMultiInstanceManager(bridge).capture_modal()

    assert bridge.modal_inspect_calls == 3
    assert bridge.modal_capture_calls == 0
    assert bridge.press_calls == 0


@pytest.mark.parametrize("rows", [
    [],
    [modal_row(window_id=52), modal_row(window_id=53)],
    [modal_row(title="New instance")],
    [modal_row(parent_id=99)],
    [modal_row(window_id=41)],
    [modal_row(x=99.)],
    [modal_row(x=301.)],
    [modal_row(width=800.)],
])
def test_manager_rejects_missing_ambiguous_or_unbound_modal(rows: list[dict[str, int | float | str]]) -> None:
    bridge = FakeModalBridge(modal_rows=rows)

    with pytest.raises(HostCapabilityError, match="modal"):
        MacOSMultiInstanceManager(bridge).capture_modal()

    assert bridge.modal_capture_calls == 0
    assert bridge.press_calls == 0


def test_manager_rejects_same_pid_untitled_popup_outside_manager_bounds() -> None:
    bridge = FakeModalBridge(modal_rows=[modal_row(x=99.)])

    with pytest.raises(HostCapabilityError, match="modal"):
        MacOSMultiInstanceManager(bridge).capture_modal()

    assert bridge.modal_capture_calls == 0
    assert bridge.press_calls == 0


def modal_text_boxes(*, clone_y: int = 130, duplicate_x: int = 20, duplicate_y: int = 180,
                     duplicate_text: str = "The chosen instance will be duplicated") -> tuple[TextBox, ...]:
    return (
        TextBox("New instance", 1., Rect(20, 20, 160, 20)),
        TextBox("Fresh instance", 1., Rect(20, 90, 160, 20)),
        TextBox("Clone Instance", 1., Rect(20, clone_y, 160, 20)),
        TextBox(duplicate_text, 1., Rect(duplicate_x, duplicate_y, 300, 20)),
    )


def test_modal_frame_verifier_accepts_exact_rendered_popup_profile() -> None:
    frame = ModalFrame(52, 41, "", np.zeros((11, 12, 3), dtype=np.uint8))

    ModalFrameVerifier(lambda image: modal_text_boxes()).verify(frame)


def test_modal_frame_verifier_accepts_ocr_that_omits_spaces() -> None:
    frame = ModalFrame(52, 41, "", np.zeros((11, 12, 3), dtype=np.uint8))
    boxes = (
        TextBox("Newinstance", 1., Rect(20, 20, 160, 20)),
        TextBox("Fresh instance", 1., Rect(20, 90, 160, 20)),
        TextBox("Clone Instance", 1., Rect(20, 130, 160, 20)),
        TextBox("The choseninstancewill be duplicated", 1., Rect(20, 180, 300, 20)),
    )

    ModalFrameVerifier(lambda image: boxes).verify(frame)


def test_manager_presses_only_clone_row_from_fresh_verified_modal() -> None:
    bridge = FakeModalBridge(modal_rows=[modal_row()])
    manager = MacOSMultiInstanceManager(bridge)

    manager.press_clone_instance(ModalFrameVerifier(lambda image: modal_text_boxes()))

    assert len(bridge.modal_clone_calls) == 1
    parent, modal, point = bridge.modal_clone_calls[0]
    assert parent["id"] == 41
    assert modal["id"] == 52
    assert point == (680., 440.)
    assert bridge.press_calls == 0


def test_manager_presses_only_fresh_row_from_verified_modal() -> None:
    bridge = FakeModalBridge(modal_rows=[modal_row()])
    manager = MacOSMultiInstanceManager(bridge)

    manager.press_fresh_instance(ModalFrameVerifier(lambda image: modal_text_boxes()))

    assert len(bridge.modal_fresh_calls) == 1
    assert bridge.modal_fresh_calls[0][0] == 672.
    assert bridge.modal_clone_calls == []


@pytest.mark.parametrize("boxes", [
    modal_text_boxes(duplicate_text="different copy"),
    modal_text_boxes(clone_y=80),
    modal_text_boxes() + (TextBox("Fresh instance", 1., Rect(20, 120, 160, 20)),),
])
def test_modal_frame_verifier_rejects_missing_ambiguous_or_wrongly_ordered_profile(
        boxes: tuple[TextBox, ...]) -> None:
    frame = ModalFrame(52, 41, "", np.zeros((11, 12, 3), dtype=np.uint8))

    with pytest.raises(HostCapabilityError, match="modal"):
        ModalFrameVerifier(lambda image: boxes).verify(frame)


@pytest.mark.parametrize("boxes", [
    modal_text_boxes(duplicate_y=110),
    modal_text_boxes(duplicate_x=500),
])
def test_modal_frame_verifier_rejects_description_outside_clone_option_layout(
        boxes: tuple[TextBox, ...]) -> None:
    frame = ModalFrame(52, 41, "", np.zeros((11, 12, 3), dtype=np.uint8))

    with pytest.raises(HostCapabilityError, match="modal"):
        ModalFrameVerifier(lambda image: boxes).verify(frame)


def test_manager_rejects_modal_when_parent_is_not_companion_owned() -> None:
    bridge = FakeModalBridge(modal_rows=[modal_row()], main_owner_path=str(EXECUTABLE))

    with pytest.raises(HostCapabilityError, match="modal parent"):
        MacOSMultiInstanceManager(bridge).capture_modal()

    assert bridge.modal_capture_calls == 0
    assert bridge.press_calls == 0


def configured_inventory(tmp_path: Path) -> BlueStacksAirInventory:
    config = tmp_path / "bluestacks.conf"
    config.write_text('bst.installed_images="Air"\n'
                      'bst.instance.Air_2.adb_port="5595"\n'
                      'bst.instance.Air_2.android_id="aabbcc22"\n'
                      'bst.instance.Air_2.display_name="BlueStacks Air 2"\n'
                      'bst.instance.Air_3.adb_port="5605"\n'
                      'bst.instance.Air_3.android_id="aabbcc33"\n'
                      'bst.instance.Air_3.display_name="BlueStacks Air 3"\n')
    return BlueStacksAirInventory(config, executable=EXECUTABLE,
                                  process_rows=lambda: {
                                      "Air_2": True, "Air_3": False,
                                  })


def test_inventory_binds_each_configured_instance_to_one_unique_endpoint(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)

    assert inventory.instances() == [
        HostInstance("Air_2", "127.0.0.1:5595", inventory.lease("Air_2"), "running"),
        HostInstance("Air_3", "127.0.0.1:5605", inventory.lease("Air_3"), "stopped"),
    ]
    assert inventory.display_name("Air_2", digest=inventory.digest()) == "BlueStacks Air 2"
    assert inventory.installed_image_prefix() == "Air_"


def test_inventory_uses_persisted_manager_counter_after_clones_removed(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    inventory.config_path.write_text(inventory.config_path.read_text() +
                                     'bst.next_vm_id="20"\n')
    assert inventory.next_clone_name("Air_2") == "Air_20"


def test_inventory_refuses_missing_or_changed_manager_counter(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    with pytest.raises(HostCapabilityError, match="next VM counter"):
        inventory.next_clone_name("Air_2")
    original_digest = inventory.digest()
    inventory.config_path.write_text(inventory.config_path.read_text() +
                                     'bst.next_vm_id="20"\n')
    with pytest.raises(HostCapabilityError, match="configuration changed"):
        inventory.next_clone_name("Air_2", digest=original_digest)


def test_lease_is_stable_across_other_instance_creation_but_changes_with_own_identity(
        tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    source_lease = inventory.lease("Air_2")
    config = tmp_path / "bluestacks.conf"
    config.write_text(config.read_text() +
                      'bst.instance.Air_4.adb_port="5615"\n'
                      'bst.instance.Air_4.android_id="aabbcc44"\n'
                      'bst.instance.Air_4.display_name="BlueStacks Air 4"\n')
    assert inventory.lease("Air_2") == source_lease
    config.write_text(config.read_text().replace('Air_2.android_id="aabbcc22"',
                                                  'Air_2.android_id="aabbcc99"'))
    assert inventory.lease("Air_2") != source_lease


@pytest.mark.parametrize("contents", [
    'bst.instance.Air_2.adb_port="5595"\nbst.instance.Air_3.adb_port="5595"\n',
    'bst.instance.Air 2.adb_port="5595"\n',
])
def test_inventory_rejects_ambiguous_or_malformed_configured_endpoint(
        tmp_path: Path, contents: str) -> None:
    config = tmp_path / "bluestacks.conf"
    config.write_text(contents)
    inventory = BlueStacksAirInventory(config, executable=EXECUTABLE,
                                       process_rows=lambda: {})

    with pytest.raises(HostCapabilityError, match="inventory"):
        inventory.instances()


def scope_for(inventory: BlueStacksAirInventory) -> QualificationScope:
    return QualificationScope(
        "mac-a", "BlueStacks-Air-2", "seed-v1", "image-1", "29.0.2",
        {"configuration_digest": inventory.digest(), "source_instance": "Air_2",
         "source_endpoint": "127.0.0.1:5595"},
    )


def test_qualification_scope_requires_current_source_digest_and_game_version(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    scope = scope_for(inventory)
    manager = Manager()
    driver = BlueStacksAirDriver(scope, inventory, manager)

    assert driver.inventory() == inventory.instances()
    assert driver.qualification_scope() == scope

    config = tmp_path / "bluestacks.conf"
    config.write_text(config.read_text() + 'bst.instance.Air_3.display_name="Air 3"\n')
    assert driver.qualification_scope() is None

    config.write_text('bst.instance.Air_2.adb_port="5595"\n'
                      'bst.instance.Air_2.display_name="BlueStacks Air 2"\n'
                      'bst.instance.Air_3.adb_port="5605"\n'
                      'bst.instance.Air_3.display_name="BlueStacks Air 3"\n')
    manager.game_version = "29.0.3"
    assert driver.qualification_scope() is None


def test_qualification_scope_with_source_lease_survives_a_new_instance(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    scope = replace(scope_for(inventory), instance_config={
        "source_lease": inventory.lease("Air_2"), "source_instance": "Air_2",
        "source_endpoint": "127.0.0.1:5595",
    })
    driver = BlueStacksAirDriver(scope, inventory, Manager())
    assert driver.qualification_scope() == scope

    config = tmp_path / "bluestacks.conf"
    config.write_text(config.read_text() +
                      'bst.instance.Air_4.adb_port="5615"\n'
                      'bst.instance.Air_4.android_id="aabbcc44"\n'
                      'bst.instance.Air_4.display_name="BlueStacks Air 4"\n')
    assert driver.qualification_scope() == scope

    config.write_text(config.read_text().replace('Air_2.android_id="aabbcc22"',
                                                  'Air_2.android_id="aabbcc99"'))
    assert driver.qualification_scope() is None


def test_clone_lineage_requires_recorded_exact_identity(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    scope = replace(scope_for(inventory), instance_config={
        "source_lease": inventory.lease("Air_2"), "source_instance": "Air_2",
        "source_endpoint": "127.0.0.1:5595",
    })
    path = tmp_path / "lineage.json"
    driver = BlueStacksAirDriver(scope, inventory, Manager(), lineage_path=path)
    rows = {row.name: row for row in driver.inventory()}
    assert rows["Air_2"].source_lineage == scope.source_lineage
    assert rows["Air_3"].source_lineage is None

    recorded = driver._record_clone_lineage(rows["Air_3"])
    assert recorded.source_lineage == scope.source_lineage
    assert {row.name: row for row in driver.inventory()}["Air_3"].source_lineage == scope.source_lineage

    config = tmp_path / "bluestacks.conf"
    config.write_text(config.read_text().replace('Air_3.android_id="aabbcc33"',
                                                  'Air_3.android_id="aabbcc99"'))
    assert {row.name: row for row in driver.inventory()}["Air_3"].source_lineage is None


def test_qualification_scope_rejects_a_missing_or_remapped_source(tmp_path: Path) -> None:
    inventory = configured_inventory(tmp_path)
    scope = scope_for(inventory)
    driver = BlueStacksAirDriver(scope, inventory, Manager())

    assert driver.qualification_scope() == scope

    missing = replace(scope, instance_config={
        "configuration_digest": inventory.digest(), "source_instance": "missing",
        "source_endpoint": "127.0.0.1:5595",
    })
    assert BlueStacksAirDriver(missing, inventory, Manager()).qualification_scope() is None

    remapped = replace(scope, instance_config={
        "configuration_digest": inventory.digest(), "source_instance": "Air_2",
        "source_endpoint": "127.0.0.1:5605",
    })
    assert BlueStacksAirDriver(remapped, inventory, Manager()).qualification_scope() is None


@pytest.mark.parametrize("platform, executable", [
    ("linux", EXECUTABLE),
    ("darwin", Path("/app/other-host")),
])
def test_inventory_rejects_an_unsupported_host_before_configuration_io(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str,
        executable: Path) -> None:
    config = tmp_path / "unreadable.conf"
    inventory = BlueStacksAirInventory(config, executable=executable,
                                       process_rows=lambda: pytest.fail("processes observed"))
    monkeypatch.setattr(sys, "platform", platform)

    with pytest.raises(HostCapabilityError, match="supported macOS BlueStacks"):
        inventory.instances()


class ChangedSnapshotInventory:
    def __init__(self, captured_digest: str) -> None:
        self.captured_digest = captured_digest

    def digest(self) -> str:
        return self.captured_digest

    def instances(self) -> list[HostInstance]:
        return [HostInstance("Air_2", "127.0.0.1:5595", "new-lease", "running")]

    def snapshot(self) -> tuple[str, list[HostInstance]]:
        return ("configuration-changed", self.instances())


def test_qualification_scope_rejects_a_config_change_between_digest_and_inventory() -> None:
    scope = QualificationScope(
        "mac-a", "BlueStacks-Air-2", "seed-v1", "image-1", "29.0.2",
        {"configuration_digest": "configuration-captured", "source_instance": "Air_2",
         "source_endpoint": "127.0.0.1:5595"},
    )
    inventory = ChangedSnapshotInventory("configuration-captured")

    assert BlueStacksAirDriver(scope, inventory, Manager()).qualification_scope() is None


def test_manager_rejects_non_manager_window_before_capture() -> None:
    bridge = FakeManagerBridge(title="BlueStacks Air 4")

    with pytest.raises(HostCapabilityError, match="manager window"):
        MacOSMultiInstanceManager(bridge).capture()

    assert bridge.capture_calls == 0


def test_manager_capture_returns_image_for_the_exact_manager_window() -> None:
    bridge = FakeManagerBridge(title="BlueStacks Air Manager")

    frame = MacOSMultiInstanceManager(bridge).capture()

    assert isinstance(frame, ManagerFrame)
    assert frame.window_id == 41
    assert frame.image.shape == (10, 10, 3)
    assert bridge.capture_calls == 1


def test_manager_accepts_the_exact_companion_manager_executable() -> None:
    bridge = FakeManagerBridge(title="BlueStacks Air Manager",
                               owner_path=str(MANAGER_EXECUTABLE))

    frame = MacOSMultiInstanceManager(bridge).capture()

    assert frame.window_id == 41
    assert bridge.capture_calls == 1


def test_manager_opens_the_exact_companion_when_no_verified_window_exists() -> None:
    """Catch a launcher that skips the post-launch exact-manager validation."""
    class DelayedManagerBridge(FakeManagerBridge):
        available = False

        def inspect(self) -> dict[str, int | float | str]:
            if not self.available:
                raise HostCapabilityError("exact manager window unavailable")
            return super().inspect()

    bridge = DelayedManagerBridge(title="BlueStacks Air Manager",
                                  owner_path=str(MANAGER_EXECUTABLE))
    launches: list[str] = []

    def launch() -> None:
        launches.append("manager")
        bridge.available = True

    frame = MacOSMultiInstanceManager(bridge, launcher=launch).ensure_open()

    assert frame.window_id == 41
    assert bridge.capture_calls == 1
    assert launches == ["manager"]


def test_manager_refuses_an_unverified_window_without_launching() -> None:
    """Catch a launcher that treats a spoofed Manager window as merely absent."""
    bridge = FakeManagerBridge(title="BlueStacks Air Manager",
                               owner_path="/Applications/Spoof.app/Contents/MacOS/Spoof")
    launches: list[str] = []

    with pytest.raises(HostCapabilityError, match="manager owner"):
        MacOSMultiInstanceManager(bridge, launcher=lambda: launches.append("manager")).ensure_open()

    assert launches == []


def test_manager_refuses_a_wrong_title_without_launching() -> None:
    """Catch an absent-window branch that launches over a mismatched title."""
    bridge = FakeManagerBridge(title="BlueStacks Air 5")
    launches: list[str] = []

    with pytest.raises(HostCapabilityError, match="exact manager title"):
        MacOSMultiInstanceManager(bridge, launcher=lambda: launches.append("manager")).ensure_open()

    assert launches == []


def test_manager_press_reobserves_same_window_before_press() -> None:
    bridge = FakeManagerBridge(title="BlueStacks Air Manager", moved=True)
    manager = MacOSMultiInstanceManager(bridge)
    frame = manager.capture()

    with pytest.raises(HostCapabilityError, match="window changed"):
        manager.press(frame.window_id, (300, 400), "Stop")

    assert bridge.inspect_calls == 2
    assert bridge.press_calls == 0


def test_manager_rejects_a_spoofed_window_owner_before_capture() -> None:
    bridge = FakeManagerBridge(title="BlueStacks Air Manager",
                               owner_path="/Applications/Spoof.app/Contents/MacOS/Spoof")

    with pytest.raises(HostCapabilityError, match="manager owner"):
        MacOSMultiInstanceManager(bridge).capture()

    assert bridge.capture_calls == 0


def test_manager_rejects_non_macos_before_an_injected_bridge_is_called(
        monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeManagerBridge(title="BlueStacks Air Manager")
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(HostCapabilityError, match="supported macOS"):
        MacOSMultiInstanceManager(bridge).capture()

    assert bridge.inspect_calls == 0


@pytest.mark.parametrize("label", [
    "Stop all", "Delete", "Cloud Save", "Account", "Password", "Google Profile", "other",
])
def test_manager_rejects_forbidden_or_unspecified_control_labels(label: str) -> None:
    bridge = FakeManagerBridge(title="BlueStacks Air Manager")

    with pytest.raises(HostCapabilityError, match="manager control"):
        MacOSMultiInstanceManager(bridge).press(41, (300, 400), label)

    assert bridge.press_calls == 0


class FakeManager:
    def __init__(self, labels: list[tuple[str, tuple[int, int]]],
                 on_press: Callable[[str], None] | None = None) -> None:
        self.labels = labels
        self.on_press = on_press
        self.game_version = "29.0.2"
        self.source_version = "image"
        self.presses: list[tuple[str, tuple[int, int]]] = []

    def capture(self) -> ManagerFrame:
        return ManagerFrame(41, np.zeros((1, 1, 3), dtype=np.uint8))

    def press(self, window_id: int, point: tuple[int, int], expected_label: str) -> None:
        assert window_id == 41
        self.presses.append((expected_label, point))
        if self.on_press is not None:
            self.on_press(expected_label)


def test_manager_row_observation_converts_retina_pixels_to_manager_points() -> None:
    class RetinaManager:
        def capture(self) -> ManagerFrame:
            return ManagerFrame(41, np.zeros((400, 800, 3), dtype=np.uint8),
                                x=100., y=200., width=400., height=200.)

    observation = ManagerRowObservation(
        RetinaManager(),
        ocr_reader=lambda _image: (
            TextBox("source", 1., Rect(100, 120, 100, 20)),
            TextBox("Stop", 1., Rect(600, 120, 80, 20)),
        ),
    )

    control = observation.row_control("source", action="Stop")

    assert control.window_id == 41
    assert control.point == (420, 265)


def test_manager_row_observation_accepts_joined_status_dot_only() -> None:
    class ManagerFrameSource:
        def capture(self) -> ManagerFrame:
            return ManagerFrame(41, np.zeros((100, 800, 3), dtype=np.uint8))

    def boxes(label: str) -> tuple[TextBox, ...]:
        return (TextBox(label, .99, Rect(100, 20, 180, 20)),
                TextBox("Stop", .99, Rect(600, 20, 80, 20)))

    observed = ManagerRowObservation(ManagerFrameSource(),
                                     ocr_reader=lambda _image: boxes("BlueStacks Air 16○"))
    assert observed.row_control("BlueStacks Air 16", action="Stop").point == (640, 30)

    observed = ManagerRowObservation(ManagerFrameSource(),
                                     ocr_reader=lambda _image: boxes("BlueStacks Air 160"))
    with pytest.raises(HostCapabilityError, match="missing or ambiguous"):
        observed.row_control("BlueStacks Air 16", action="Stop")


class LifecycleInventory:
    def __init__(self, name: str, endpoint: str, *, running: bool) -> None:
        self.name = name
        self.endpoint = endpoint
        self.running = running
        self.snapshots = 0

    def instances(self) -> list[HostInstance]:
        return self.snapshot()[1]

    def snapshot(self) -> tuple[str, list[HostInstance]]:
        self.snapshots += 1
        state = "running" if self.running else "stopped"
        return ("configuration-v1", [HostInstance(self.name, self.endpoint, "lease", state)])


def test_stable_scope_lifecycle_accepts_host_config_write_after_start() -> None:
    class MutableLifecycleInventory(LifecycleInventory):
        digest = "configuration-v1"

        def snapshot(self) -> tuple[str, list[HostInstance]]:
            _, rows = super().snapshot()
            return self.digest, rows

    inventory = MutableLifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([("Tiramisu64_4", (100, 300)), ("Start", (800, 300))])

    def press(action: str) -> None:
        if action == "Start":
            inventory.running = True
            inventory.digest = "configuration-v2"
            manager.labels = [("Tiramisu64_4", (100, 300)), ("Stop", (800, 300))]

    manager.on_press = press
    scope = QualificationScope("mac", "BlueStacks-Air", "source", "image", "29.0.2",
                               {"source_lease": "lease", "source_instance": inventory.name,
                                "source_endpoint": inventory.endpoint})
    driver = BlueStacksAirDriver(scope, inventory, manager,
                                 ocr_reader=lambda _image: lifecycle_boxes(manager),
                                 endpoint_present=lambda _endpoint: inventory.running,
                                 timeout=0., poll_interval=.01)
    driver.start(inventory.name)
    assert inventory.running is True


def lifecycle_boxes(manager: FakeManager) -> tuple[TextBox, ...]:
    return tuple(
        TextBox(label, 1., Rect(x, y, 80, 20))
        for label, (x, y) in manager.labels
    )


def lifecycle_scope(inventory: LifecycleInventory, *, digest: str = "configuration-v1",
                    endpoint: str | None = None, version: str = "29.0.2") -> QualificationScope:
    return QualificationScope(
        "mac", "BlueStacks-Air", "source", "image", version,
        {"configuration_digest": digest, "source_instance": inventory.name,
         "source_endpoint": endpoint or inventory.endpoint},
    )


def lifecycle_driver(manager: FakeManager, inventory: LifecycleInventory,
                     endpoint_present: Callable[[str], bool],
                     scope: QualificationScope | None = None) -> BlueStacksAirDriver:
    return BlueStacksAirDriver(
        scope or lifecycle_scope(inventory), inventory, manager,
        ocr_reader=lambda _image: lifecycle_boxes(manager),
        endpoint_present=endpoint_present, timeout=0., poll_interval=.01,
    )


def test_lifecycle_stop_uses_only_the_exact_named_running_row() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=True)
    manager = FakeManager([
        ("Tiramisu64_4", (100, 300)), ("Stop", (800, 300)),
    ], on_press=lambda label: (
        setattr(inventory, "running", False),
        setattr(manager, "labels", [("Tiramisu64_4", (100, 300)), ("Start", (800, 300))]),
    ) if label == "Stop" else None)
    endpoints: list[str] = []
    driver = lifecycle_driver(
        manager, inventory,
        lambda endpoint: endpoints.append(endpoint) is None and inventory.running,
    )

    driver.stop("Tiramisu64_4")

    assert manager.presses == [("Stop", (840, 310))]
    assert endpoints == ["127.0.0.1:5595", "127.0.0.1:5595", "127.0.0.1:5595"]


def test_lifecycle_stop_refuses_duplicate_name_or_stop_all() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=True)
    manager = FakeManager([
        ("Tiramisu64_4", (100, 300)), ("Tiramisu64_4", (100, 400)),
        ("Stop all", (800, 700)),
    ])

    with pytest.raises(HostCapabilityError):
        lifecycle_driver(manager, inventory, lambda _endpoint: False).stop("Tiramisu64_4")

    assert manager.presses == []


def test_lifecycle_start_uses_only_the_exact_named_stopped_row() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([
        ("Tiramisu64_4", (100, 300)), ("Start", (800, 300)),
    ], on_press=lambda label: (
        setattr(inventory, "running", True),
        setattr(manager, "labels", [("Tiramisu64_4", (100, 300)), ("Stop", (800, 300))]),
    ) if label == "Start" else None)

    lifecycle_driver(manager, inventory, lambda _endpoint: inventory.running).start(
        "Tiramisu64_4")

    assert manager.presses == [("Start", (840, 310))]


def test_lifecycle_start_reobserves_row_when_manager_window_changed_before_press() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    rejected: list[str] = []

    class MovingManager(FakeManager):
        def press(self, window_id: int, point: tuple[int, int], expected_label: str) -> None:
            if not rejected:
                rejected.append(expected_label)
                raise HostCapabilityError("manager window changed")
            super().press(window_id, point, expected_label)

    manager = MovingManager([
        ("Tiramisu64_4", (100, 300)), ("Start", (800, 300)),
    ], on_press=lambda label: (
        setattr(inventory, "running", True),
        setattr(manager, "labels", [("Tiramisu64_4", (100, 300)), ("Stop", (800, 300))]),
    ) if label == "Start" else None)

    lifecycle_driver(manager, inventory, lambda _endpoint: inventory.running).start(
        "Tiramisu64_4")

    assert rejected == ["Start"]
    assert manager.presses == [("Start", (840, 310))]


def test_lifecycle_start_reobserves_when_manager_window_changes_during_capture() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    moves: list[int] = []

    class MovingCaptureManager(FakeManager):
        def capture(self) -> ManagerFrame:
            if not moves:
                moves.append(1)
                raise HostCapabilityError("manager window changed: id,x,y,w,h 41,0,0,9,9 -> 41,0,0,9,10")
            return super().capture()

    manager = MovingCaptureManager([
        ("Tiramisu64_4", (100, 300)), ("Start", (800, 300)),
    ], on_press=lambda label: (
        setattr(inventory, "running", True),
        setattr(manager, "labels", [("Tiramisu64_4", (100, 300)), ("Stop", (800, 300))]),
    ) if label == "Start" else None)

    lifecycle_driver(manager, inventory, lambda _endpoint: inventory.running).start(
        "Tiramisu64_4")

    assert moves == [1]
    assert manager.presses == [("Start", (840, 310))]


def test_swift_bridge_keeps_the_observed_window_change(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "swift", stderr=(
            "BlueStacks manager unavailable: manager window changed: "
            "id,x,y,w,h 41,0.0,38.0,900.0,600.0 -> 41,0.0,38.0,900.0,640.0\n"))

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", refuse)

    with pytest.raises(HostCapabilityError) as raised:
        _SwiftManagerBridge()._run("capture")

    assert str(raised.value) == ("manager window changed: "
                                 "id,x,y,w,h 41,0.0,38.0,900.0,600.0 -> 41,0.0,38.0,900.0,640.0")


def test_lifecycle_start_gives_up_when_manager_window_keeps_changing() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    attempts: list[str] = []

    class UnsettledManager(FakeManager):
        def press(self, window_id: int, point: tuple[int, int], expected_label: str) -> None:
            attempts.append(expected_label)
            raise HostCapabilityError("manager window changed")

    manager = UnsettledManager([("Tiramisu64_4", (100, 300)), ("Start", (800, 300))])

    with pytest.raises(HostCapabilityError, match="manager window changed"):
        lifecycle_driver(manager, inventory, lambda _endpoint: inventory.running).start(
            "Tiramisu64_4")

    assert attempts == ["Start"] * (BlueStacksAirDriver._PRESS_RETRIES + 1)


def test_lifecycle_refuses_an_unchanged_process_state_after_press() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([
        ("Tiramisu64_4", (100, 300)), ("Start", (800, 300)),
    ])

    with pytest.raises(HostCapabilityError, match="postcondition"):
        lifecycle_driver(manager, inventory, lambda _endpoint: inventory.running).start("Tiramisu64_4")

    assert manager.presses == [("Start", (840, 310))]


def test_lifecycle_refuses_an_attached_endpoint_before_start_press() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([
        ("Tiramisu64_4", (100, 300)), ("Start", (800, 300)),
    ])

    with pytest.raises(HostCapabilityError, match="endpoint"):
        lifecycle_driver(manager, inventory, lambda _endpoint: True).start("Tiramisu64_4")

    assert manager.presses == []


@pytest.mark.parametrize(("scope", "version"), [
    (QualificationScope("", "BlueStacks-Air", "source", "image", "29.0.2",
                        {"configuration_digest": "configuration-v1", "source_instance": "Tiramisu64_4",
                         "source_endpoint": "127.0.0.1:5595"}), "29.0.2"),
    (QualificationScope("mac", "BlueStacks-Air", "source", "image", "29.0.2",
                        {"configuration_digest": "configuration-v0", "source_instance": "Tiramisu64_4",
                         "source_endpoint": "127.0.0.1:5595"}), "29.0.2"),
    (QualificationScope("mac", "BlueStacks-Air", "source", "image", "29.0.2",
                        {"configuration_digest": "configuration-v1", "source_instance": "Tiramisu64_4",
                         "source_endpoint": "127.0.0.1:5605"}), "29.0.2"),
    (QualificationScope("mac", "BlueStacks-Air", "source", "image", "29.0.1",
                        {"configuration_digest": "configuration-v1", "source_instance": "Tiramisu64_4",
                         "source_endpoint": "127.0.0.1:5595"}), "29.0.2"),
], ids=("invalid", "configuration_changed", "source_remapped", "manager_version_changed"))
def test_lifecycle_refuses_every_stale_qualification_scope_before_press(
        scope: QualificationScope, version: str) -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([("Tiramisu64_4", (100, 300)), ("Start", (800, 300))])
    manager.game_version = version

    with pytest.raises(HostCapabilityError, match="qualification"):
        lifecycle_driver(manager, inventory, lambda _endpoint: False, scope).start("Tiramisu64_4")

    assert manager.presses == []


class ChangingGenerationInventory(LifecycleInventory):
    def snapshot(self) -> tuple[str, list[HostInstance]]:
        self.snapshots += 1
        state = "running" if self.running else "stopped"
        digest = "configuration-v1" if self.snapshots == 1 else "configuration-v2"
        return (digest, [HostInstance(self.name, self.endpoint, "lease", state)])


def test_lifecycle_refuses_generation_changed_during_prepress_observations() -> None:
    inventory = ChangingGenerationInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([("Tiramisu64_4", (100, 300)), ("Start", (800, 300))])

    with pytest.raises(HostCapabilityError, match="configuration changed"):
        lifecycle_driver(manager, inventory, lambda _endpoint: False).start("Tiramisu64_4")

    assert manager.presses == []


class MutableGenerationInventory(LifecycleInventory):
    def __init__(self, name: str, endpoint: str, *, running: bool) -> None:
        super().__init__(name, endpoint, running=running)
        self.digest = "configuration-v1"

    def snapshot(self) -> tuple[str, list[HostInstance]]:
        self.snapshots += 1
        state = "running" if self.running else "stopped"
        return (self.digest, [HostInstance(self.name, self.endpoint, "lease", state)])


def test_lifecycle_refuses_process_drift_after_initial_prepress_snapshot() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([("Tiramisu64_4", (100, 300)), ("Start", (800, 300))])
    observations = 0

    def endpoint_present(_endpoint: str) -> bool:
        nonlocal observations
        observations += 1
        if observations == 1:
            inventory.running = True
            return False
        return True

    with pytest.raises(HostCapabilityError):
        lifecycle_driver(manager, inventory, endpoint_present).start("Tiramisu64_4")

    assert observations == 1
    assert manager.presses == []


def test_lifecycle_refuses_configuration_drift_after_initial_prepress_snapshot() -> None:
    inventory = MutableGenerationInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([("Tiramisu64_4", (100, 300)), ("Start", (800, 300))])
    observations = 0

    def endpoint_present(_endpoint: str) -> bool:
        nonlocal observations
        observations += 1
        if observations == 1:
            inventory.digest = "configuration-v2"
        return False

    with pytest.raises(HostCapabilityError):
        lifecycle_driver(manager, inventory, endpoint_present).start("Tiramisu64_4")

    assert observations == 1
    assert manager.presses == []


def test_lifecycle_refuses_manager_version_drift_after_initial_prepress_snapshot() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([("Tiramisu64_4", (100, 300)), ("Start", (800, 300))])
    observations = 0

    def endpoint_present(_endpoint: str) -> bool:
        nonlocal observations
        observations += 1
        if observations == 1:
            manager.game_version = "29.0.3"
        return False

    with pytest.raises(HostCapabilityError):
        lifecycle_driver(manager, inventory, endpoint_present).start("Tiramisu64_4")

    assert observations == 1
    assert manager.presses == []


def test_lifecycle_refuses_endpoint_drift_after_initial_prepress_snapshot() -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([("Tiramisu64_4", (100, 300)), ("Start", (800, 300))])
    observations = 0

    def endpoint_present(_endpoint: str) -> bool:
        nonlocal observations
        observations += 1
        return observations > 1

    with pytest.raises(HostCapabilityError, match="endpoint"):
        lifecycle_driver(manager, inventory, endpoint_present).start("Tiramisu64_4")

    assert observations == 2
    assert manager.presses == []


def test_lifecycle_default_endpoint_evidence_requires_the_exact_adb_transport(
        monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = LifecycleInventory("Tiramisu64_4", "127.0.0.1:5595", running=False)
    manager = FakeManager([
        ("Tiramisu64_4", (100, 300)), ("Start", (800, 300)),
    ], on_press=lambda _label: (
        setattr(inventory, "running", True),
        setattr(manager, "labels", [("Tiramisu64_4", (100, 300)), ("Stop", (800, 300))]),
    ))
    monkeypatch.setattr("fleet.bluestacks_air._adb_endpoint_rows",
                        lambda: ("127.0.0.1:5595",) if inventory.running else (),
                        raising=False)
    driver = BlueStacksAirDriver(
        QualificationScope("mac", "BlueStacks-Air", "source", "image", "29.0.2",
                           {"configuration_digest": "configuration-v1", "source_instance": inventory.name,
                            "source_endpoint": inventory.endpoint}), inventory, manager,
        ocr_reader=lambda _image: lifecycle_boxes(manager), timeout=0., poll_interval=.01,
    )

    driver.start("Tiramisu64_4")

    assert manager.presses == [("Start", (840, 310))]


class CloneInventory:
    """Mutable fake configuration/process inventory; no host resources are used."""

    def __init__(self, names: set[str], *, create: str | None = None,
                 extra_created: bool = False, target_running: bool = False) -> None:
        self.names = set(names)
        self.create = create
        self.extra_created = extra_created
        self.target_running = target_running
        self.source_running = True
        self.created = False
        self.snapshots = 0

    def _instances(self) -> list[HostInstance]:
        names = sorted(self.names | ({self.create} if self.created and self.create else set()))
        if self.created and self.extra_created:
            names.append("unexpected_9")
        return [HostInstance(
            name, f"127.0.0.1:{5571 + index * 4}", f"lease-{name}",
            "running" if (name.endswith("_2") and self.source_running)
            or (self.created and name == self.create and self.target_running) else "stopped")
                for index, name in enumerate(names)]

    def instances(self) -> list[HostInstance]:
        return self.snapshot()[1]

    def next_clone_name(self, source: str, *, digest: str | None = None) -> str:
        prefix = (match.group(1) if (match := re.fullmatch(r"(.+_)[1-9][0-9]*", source))
                  else f"{source}_")
        number = getattr(self, "next_id", max((int(name[len(prefix):])
                                                for name in self.names
                                                if name.startswith(prefix) and name[len(prefix):].isdigit()),
                                               default=0) + 1)
        return f"{prefix}{number}"

    def snapshot(self) -> tuple[str, list[HostInstance]]:
        self.snapshots += 1
        return ("configuration-v2" if self.created else "configuration-v1", self._instances())


class CloneManager(FakeManager):
    def __init__(self, frames: list[list[tuple[str, tuple[int, int]]]],
                 inventory: CloneInventory, *, create_record: bool = True,
                 drift_source_after_instance: bool = False,
                 drift_source_running_after_instance: bool = False) -> None:
        super().__init__(frames[0])
        self.frames = frames
        self.inventory = inventory
        self.create_record = create_record
        self.drift_source_after_instance = drift_source_after_instance
        self.drift_source_running_after_instance = drift_source_running_after_instance
        self.frame_index = 0

    def capture(self) -> ManagerFrame:
        self.labels = self.frames[min(self.frame_index, len(self.frames) - 1)]
        return super().capture()

    def press(self, window_id: int, point: tuple[int, int], expected_label: str) -> None:
        super().press(window_id, point, expected_label)
        if expected_label == "Stop":
            self.inventory.source_running = False
        if expected_label == "Start":
            self.inventory.source_running = True
        if expected_label == "Instance" and self.drift_source_after_instance:
            self.inventory.source_running = False
        if expected_label == "Instance" and self.drift_source_running_after_instance:
            self.inventory.source_running = True
        if expected_label == "Create" and self.create_record:
            self.inventory.created = True
        self.frame_index += 1


class FreshManager(FakeManager):
    def __init__(self, inventory: CloneInventory, *, valid_form: bool = True,
                 create_record: bool = True) -> None:
        super().__init__([("Instance", (300, 700))])
        self.inventory = inventory
        self.valid_form = valid_form
        self.create_record = create_record
        self.form_open = False

    def capture(self) -> ManagerFrame:
        self.labels = ([("Tiramisu64_3", (100, 300)), ("Start", (800, 300))]
                       if self.inventory.created else [("Instance", (300, 700))])
        return super().capture()

    def capture_modal(self) -> ModalFrame:
        if not self.form_open:
            raise HostCapabilityError("fresh form missing")
        return ModalFrame(52, 41, "", np.zeros((500, 900, 3), dtype=np.uint8),
                          x=100., y=200., width=450., height=250.)

    def press_fresh_instance(self) -> None:
        self.form_open = True

    def press_fresh_create(self, window_id: int, point: tuple[int, int]) -> None:
        assert window_id == 52
        assert point == (505, 430)
        if self.create_record:
            self.inventory.created = True
        self.form_open = False


def fresh_boxes(manager: FreshManager) -> tuple[TextBox, ...]:
    if not manager.form_open:
        return lifecycle_boxes(manager)
    fields = ["Fresh instance - Android 13", "CPU cores", "Memory allocation",
              "Resolution", "Performance mode", "DPI", "Instance count", "1", "Create"]
    if not manager.valid_form:
        fields.remove("Instance count")
    return tuple(TextBox(label, 1., Rect(790 if label == "Create" else 480
                                          if label == "1" else 20,
                                          450 if label in {"Create", "1", "Instance count"}
                                          else index * 35, 40, 20))
                 for index, label in enumerate(fields))


def fresh_driver(manager: FreshManager, inventory: CloneInventory) -> BlueStacksAirDriver:
    return BlueStacksAirDriver(
        QualificationScope("mac", "BlueStacks-Air", "source", "image", "29.0.2", {
            "configuration_digest": "configuration-v1", "source_instance": "Tiramisu64_2",
            "source_endpoint": "127.0.0.1:5571"}),
        inventory, manager, ocr_reader=lambda _image: fresh_boxes(manager),
        endpoint_present=lambda endpoint: endpoint == "127.0.0.1:5571",
        timeout=0., poll_interval=.01,
    )


def test_fresh_create_requires_one_form_and_exact_new_manager_row() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = FreshManager(inventory)
    driver = fresh_driver(manager, inventory)

    assert driver.supports_fresh_provision is True
    assert driver.required_fresh_prefix == "Tiramisu64_"
    created = driver.create_fresh("Tiramisu64_3")

    assert created.name == "Tiramisu64_3"
    assert created.state == "stopped"
    assert manager.presses == [("Instance", (340, 710))]


def test_fresh_create_refuses_ambiguous_form_without_creating() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = FreshManager(inventory, valid_form=False)
    driver = fresh_driver(manager, inventory)

    with pytest.raises(HostCapabilityError, match="fresh form"):
        driver.create_fresh("Tiramisu64_3")
    assert inventory.created is False


def test_fresh_create_refuses_nonunit_accessibility_count() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = FreshManager(inventory)
    manager.fresh_instance_count = lambda _window_id: "2"  # type: ignore[attr-defined]
    driver = fresh_driver(manager, inventory)

    with pytest.raises(HostCapabilityError, match="instance count"):
        driver.create_fresh("Tiramisu64_3")
    assert inventory.created is False


def test_fresh_capability_does_not_require_clone_qualification() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = FreshManager(inventory)
    driver = BlueStacksAirDriver(None, inventory, manager,
                                 fresh_prefix="Tiramisu64_",
                                 ocr_reader=lambda _image: fresh_boxes(manager),
                                 endpoint_present=lambda _endpoint: False,
                                 timeout=0.)

    assert driver.qualification_scope() is None
    assert driver.supports_clone_staging is False
    assert driver.supports_fresh_provision is True


def clone_frames(source: str, *, create_source: str | None = None,
                 conflicting_source: str | None = None) -> list[list[tuple[str, tuple[int, int]]]]:
    return [
        [(source, (100, 300)), ("Stop", (800, 300))],
        [(source, (100, 300)), ("Start", (800, 300)), ("Instance", (800, 300))],
        [("Clone instance", (600, 220))],
        [("Source", (20, 300)), (create_source or source, (140, 300)),
         *([(conflicting_source, (140, 340))] if conflicting_source else []),
         ("1", (430, 400)), ("Create", (700, 600))],
        [(source, (100, 300)), ("Start", (800, 300))],
        [(source, (100, 300)), ("Stop", (800, 300))],
    ]


def clone_driver(manager: CloneManager, inventory: CloneInventory,
                 endpoint_observations: list[str] | None = None) -> BlueStacksAirDriver:
    source = "Tiramisu64_2"
    source_instance = next(item for item in inventory.instances() if item.name == source)
    return BlueStacksAirDriver(
        QualificationScope("mac", "BlueStacks-Air", "source", "image", "29.0.2", {
            "configuration_digest": "configuration-v1", "source_instance": source,
            "source_endpoint": source_instance.endpoint,
        }), inventory, manager, ocr_reader=lambda _image: lifecycle_boxes(manager),
        endpoint_present=lambda endpoint: (
            (endpoint_observations.append(endpoint) if endpoint_observations is not None else None)
            is None and (endpoint == source_instance.endpoint and inventory.source_running
                         or (inventory.created and inventory.target_running
                             and endpoint == "127.0.0.1:5575"))),
        timeout=0., poll_interval=.01,
    )


def test_clone_requires_the_next_deterministic_name_and_exact_source() -> None:
    inventory = CloneInventory({"Tiramisu64_2", "Tiramisu64_4", "Tiramisu64_5"}, create="Tiramisu64_6")
    manager = CloneManager(clone_frames("Tiramisu64_2"), inventory)

    with pytest.raises(HostCapabilityError, match="next clone name"):
        clone_driver(manager, inventory).stage_clone("Tiramisu64_7", "Tiramisu64_2")

    assert manager.presses == []


def test_clone_uses_manager_counter_when_removed_clones_leave_a_gap() -> None:
    inventory = CloneInventory({"Tiramisu64_2", "Tiramisu64_6"}, create="Tiramisu64_20")
    inventory.next_id = 20
    manager = CloneManager(clone_frames("Tiramisu64_2"), inventory)

    created = clone_driver(manager, inventory).stage_clone("Tiramisu64_20", "Tiramisu64_2")

    assert created.name == "Tiramisu64_20"


def test_clone_uses_the_single_manager_instance_control_not_a_row_action() -> None:
    """The Manager's Instance button is in its footer, below every source row."""
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    frames = clone_frames("Tiramisu64_2")
    frames[1] = [("Tiramisu64_2", (100, 300)), ("Start", (800, 300)),
                 ("Instance", (300, 700))]
    manager = CloneManager(frames, inventory)

    created = clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert created.name == "Tiramisu64_3"
    assert manager.presses[1] == ("Instance", (340, 710))


def test_clone_allows_a_base_template_and_derives_the_next_numbered_clone() -> None:
    """BlueStacks names the first template without a numeric suffix."""
    inventory = CloneInventory({"Tiramisu64", "Tiramisu64_6"}, create="Tiramisu64_7")
    frames = clone_frames("Tiramisu64")[1:]
    manager = CloneManager(frames, inventory)
    driver = BlueStacksAirDriver(
        QualificationScope("mac", "BlueStacks-Air", "source", "image", "29.0.2", {
            "configuration_digest": "configuration-v1", "source_instance": "Tiramisu64",
            "source_endpoint": next(item.endpoint for item in inventory.instances()
                                    if item.name == "Tiramisu64"),
        }), inventory, manager, ocr_reader=lambda _image: lifecycle_boxes(manager),
        endpoint_present=lambda _endpoint: False,
        timeout=0., poll_interval=.01,
    )

    created = driver.stage_clone("Tiramisu64_7", "Tiramisu64")

    assert created.name == "Tiramisu64_7"


def test_clone_requires_the_exact_scoped_source_before_press() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = CloneManager(clone_frames("Tiramisu64_2"), inventory)

    with pytest.raises(HostCapabilityError, match="clone source"):
        clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_4")

    assert manager.presses == []


def test_clone_creates_one_exact_configured_target() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = CloneManager(clone_frames("Tiramisu64_2"), inventory)

    created = clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert created.name == "Tiramisu64_3"
    assert created.endpoint == "127.0.0.1:5575"
    assert created.state == "stopped"
    assert manager.presses == [("Stop", (840, 310)), ("Instance", (840, 310)),
                               ("Clone instance", (640, 230)), ("Create", (740, 610)),
                               ("Start", (840, 310))]


def test_clone_stops_and_reproves_a_running_source_before_opening_the_picker() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = CloneManager([
        [("Tiramisu64_2", (100, 300)), ("Stop", (800, 300))],
        [("Tiramisu64_2", (100, 300)), ("Start", (800, 300)), ("Instance", (800, 300))],
        [("Clone instance", (600, 220))],
        [("Source", (20, 300)), ("Tiramisu64_2", (140, 300)),
         ("1", (430, 400)), ("Create", (700, 600))],
        [("Tiramisu64_2", (100, 300)), ("Start", (800, 300))],
        [("Tiramisu64_2", (100, 300)), ("Stop", (800, 300))],
    ], inventory)

    clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert manager.presses == [("Stop", (840, 310)), ("Instance", (840, 310)),
                               ("Clone instance", (640, 230)), ("Create", (740, 610)),
                               ("Start", (840, 310))]


def test_clone_revalidates_the_running_scoped_source_before_clone_instance() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = CloneManager(clone_frames("Tiramisu64_2"), inventory,
                           drift_source_running_after_instance=True)

    with pytest.raises(HostCapabilityError, match="source is missing or not stopped"):
        clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert manager.presses == [("Stop", (840, 310)), ("Instance", (840, 310))]


def test_clone_rejects_a_conflicting_create_form_source_before_create() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = CloneManager(clone_frames("Tiramisu64_2", conflicting_source="Tiramisu64_4"), inventory)

    with pytest.raises(HostCapabilityError, match="create form source"):
        clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert manager.presses == [("Stop", (840, 310)), ("Instance", (840, 310)),
                               ("Clone instance", (640, 230))]


def test_clone_requires_the_exact_adb_endpoint_for_a_running_target() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3", target_running=True)
    manager = CloneManager(clone_frames("Tiramisu64_2"), inventory)
    endpoints: list[str] = []

    created = clone_driver(manager, inventory, endpoints).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert created == HostInstance("Tiramisu64_3", "127.0.0.1:5575", "lease-Tiramisu64_3", "running")
    assert "127.0.0.1:5575" in endpoints
    assert endpoints[-1] == "127.0.0.1:5571"


@pytest.mark.parametrize(("frames", "match"), [
    ([[('Tiramisu64_2', (100, 300))]], "row"),
    ([[('Tiramisu64_2', (100, 300)), ("Stop", (800, 300))],
      [('Tiramisu64_2', (100, 300)), ("Start", (800, 300)), ("Instance", (800, 300))],
      [("unexpected dialog", (600, 220))]], "clone dialog"),
])
def test_clone_refuses_missing_or_unexpected_manager_dialog(
        frames: list[list[tuple[str, tuple[int, int]]]], match: str) -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = CloneManager(frames, inventory)

    with pytest.raises(HostCapabilityError, match=match):
        clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert manager.presses == ([] if match == "row" else [
        ("Stop", (840, 310)), ("Instance", (840, 310)),
    ])


def test_clone_refuses_ambiguous_source_rows_before_press() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = CloneManager([[("Tiramisu64_2", (100, 300)), ("Tiramisu64_2", (100, 400)),
                             ("Instance", (800, 300))]], inventory)

    with pytest.raises(HostCapabilityError, match="row"):
        clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert manager.presses == []


def test_clone_refuses_any_source_mismatch_on_the_create_form() -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3")
    manager = CloneManager(clone_frames("Tiramisu64_2", create_source="Tiramisu64_4"), inventory)

    with pytest.raises(HostCapabilityError, match="create form"):
        clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert manager.presses == [("Stop", (840, 310)), ("Instance", (840, 310)),
                               ("Clone instance", (640, 230))]


@pytest.mark.parametrize("create_record, extra_created", [(False, False), (True, True)],
                         ids=("no_new_config_record", "more_than_one_new_record"))
def test_clone_requires_exactly_one_fresh_config_record(
        create_record: bool, extra_created: bool) -> None:
    inventory = CloneInventory({"Tiramisu64_2"}, create="Tiramisu64_3", extra_created=extra_created)
    manager = CloneManager(clone_frames("Tiramisu64_2"), inventory, create_record=create_record)

    with pytest.raises(HostCapabilityError, match="clone postcondition"):
        clone_driver(manager, inventory).stage_clone("Tiramisu64_3", "Tiramisu64_2")

    assert manager.presses == [("Stop", (840, 310)), ("Instance", (840, 310)),
                               ("Clone instance", (640, 230)), ("Create", (740, 610))]
