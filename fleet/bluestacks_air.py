"""Read-only BlueStacks Air host inventory and qualification attestation."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from adbutils import AdbClient

from bluestacks import HostCapabilityError, HostInstance
import config
from device import Image, endpoint_matches
from fleet.clone_qualification import QualificationScope
import ocr


_ADB_PORT = re.compile(r'^bst\.instance\.([A-Za-z0-9_]+)\.adb_port="(\d+)"$')
_ADB_PORT_KEY = re.compile(r"^bst\.instance\.[^.]+\.adb_port=")
_DISPLAY_NAME = re.compile(r'^bst\.instance\.([A-Za-z0-9_]+)\.display_name="([^"]+)"$')
_DISPLAY_NAME_KEY = re.compile(r"^bst\.instance\.[^.]+\.display_name=")
_EXECUTABLE = Path("/Applications/BlueStacks.app/Contents/MacOS/BlueStacks")
_MANAGER_EXECUTABLE = Path(
    "/Applications/BlueStacks Air multi-instance manager.app/Contents/MacOS/"
    "BlueStacks Air multi-instance manager"
)
_MANAGER_APP = Path("/Applications/BlueStacks Air multi-instance manager.app")
_MANAGER_OWNER_EXECUTABLES = frozenset({str(_EXECUTABLE), str(_MANAGER_EXECUTABLE)})
_MANAGER_CONTROLS = frozenset({"Start", "Stop", "Instance", "Clone instance", "Create"})


class MultiInstanceManager(Protocol):
    """Narrow boundary for observing and pressing the exact manager window."""

    game_version: str
    source_version: str

    def capture(self) -> ManagerFrame: ...
    def press(self, window_id: int, point: tuple[int, int], expected_label: str) -> None: ...


@dataclass(frozen=True)
class ManagerFrame:
    """One image bound to the exact manager window observed for its capture."""

    window_id: int
    image: Image
    x: float = 0.
    y: float = 0.
    width: float = 0.
    height: float = 0.


@dataclass(frozen=True)
class ModalFrame:
    """One read-only image bound to a verified Manager-owned modal window."""

    window_id: int
    parent_window_id: int
    title: str
    image: Image
    x: float = 0.
    y: float = 0.
    width: float = 0.
    height: float = 0.


@dataclass(frozen=True)
class _ManagerControl:
    """One exact Start or Stop control proven in the current manager frame."""

    window_id: int
    point: tuple[int, int]


class ManagerRowObservation:
    """Read-only capture and exact row-control recognition for one manager."""

    def __init__(self, manager: MultiInstanceManager,
                 ocr_reader: Callable[[Image], tuple[ocr.TextBox, ...]] = ocr.read) -> None:
        self._manager = manager
        self._ocr_reader = ocr_reader

    def capture(self) -> ManagerFrame:
        """Capture the exact manager window without issuing a control press."""
        return self._manager.capture()

    @staticmethod
    def _global_point(frame: ManagerFrame, pixel: tuple[int, int]) -> tuple[int, int]:
        """Map one OCR point through the measured frame scale when it is available."""
        pixel_x, pixel_y = pixel
        image_height, image_width = frame.image.shape[:2]
        if frame.width <= 0 or frame.height <= 0 or image_width <= 0 or image_height <= 0:
            return pixel
        return (round(frame.x + frame.width * pixel_x / image_width),
                round(frame.y + frame.height * pixel_y / image_height))

    def row_control(self, name: str, *, action: str) -> _ManagerControl:
        """Find one exact named row and one exact adjacent action in one fresh frame."""
        try:
            frame = self.capture()
            boxes = self._ocr_reader(frame.image)
        except HostCapabilityError:
            raise
        except Exception as exc:
            raise HostCapabilityError("BlueStacks Air manager layout is unavailable") from exc
        names = [box for box in boxes if box.text == name]
        actions = [box for box in boxes if box.text == action]
        if len(names) != 1 or not actions:
            raise HostCapabilityError("BlueStacks Air manager row is missing or ambiguous")
        row_name = names[0]
        name_centre_y = row_name.rect.y + row_name.rect.h // 2
        candidates = [control for control in actions
                      if (abs(name_centre_y - (control.rect.y + control.rect.h // 2))
                          <= max(row_name.rect.h, control.rect.h)
                          and control.rect.x >= row_name.rect.x + row_name.rect.w)]
        if len(candidates) != 1:
            raise HostCapabilityError("BlueStacks Air manager row layout is not actionable")
        control = candidates[0]
        return _ManagerControl(frame.window_id, self._global_point(
            frame, (control.rect.x + control.rect.w // 2,
                    control.rect.y + control.rect.h // 2)))


class ModalFrameVerifier:
    """Read-only OCR proof for the exact rendered New instance popup profile."""

    _HEADING = "New instance"
    _FRESH = "Fresh instance"
    _CLONE = "Clone Instance"
    _COPY = "The chosen instance will be duplicated"

    def __init__(self, ocr_reader: Callable[[Image], tuple[ocr.TextBox, ...]] = ocr.read) -> None:
        self._ocr_reader = ocr_reader

    @staticmethod
    def _normalized(text: str) -> str:
        """Compare static modal copy despite OCR joining adjacent words."""
        return "".join(text.split()).casefold()

    def _profile(self, frame: ModalFrame) -> tuple[ocr.TextBox, ocr.TextBox, ocr.TextBox, ocr.TextBox]:
        """Return the unique static profile, or fail closed."""
        if not isinstance(frame, ModalFrame):
            raise HostCapabilityError("manager modal profile is unavailable")
        try:
            boxes = self._ocr_reader(frame.image)
        except Exception as exc:
            raise HostCapabilityError("manager modal profile is unavailable") from exc
        required = (self._HEADING, self._FRESH, self._CLONE, self._COPY)
        matches = {
            text: [box for box in boxes if self._normalized(box.text) == self._normalized(text)]
            for text in required
        }
        if any(len(matches[text]) != 1 for text in required):
            raise HostCapabilityError("manager modal profile is unavailable")
        heading, fresh, clone, duplicate = (matches[text][0] for text in required)
        if (fresh.rect.y < heading.rect.y + heading.rect.h
                or clone.rect.y < fresh.rect.y + fresh.rect.h
                or duplicate.rect.y < clone.rect.y + clone.rect.h
                or duplicate.rect.x >= clone.rect.x + clone.rect.w
                or clone.rect.x >= duplicate.rect.x + duplicate.rect.w):
            raise HostCapabilityError("manager modal profile is unavailable")
        return heading, fresh, clone, duplicate

    def verify(self, frame: ModalFrame) -> None:
        """Fail unless one fresh captured popup has the required static text profile."""
        self._profile(frame)

    def clone_point(self, frame: ModalFrame) -> tuple[int, int]:
        """Return the centre of the verified Clone Instance row, in captured pixels."""
        _, _, clone, _ = self._profile(frame)
        height, width = frame.image.shape[:2]
        if width <= 0 or height <= 0 or not 0 <= clone.rect.y + clone.rect.h // 2 < height:
            raise HostCapabilityError("manager modal profile is unavailable")
        # The Manager renders this as a navigation row.  Its chevron is at the
        # right edge; use that stable hit target while OCR supplies the row y.
        return int(width * .95), clone.rect.y + clone.rect.h // 2


def _adb_endpoint_rows() -> tuple[str, ...]:
    """Read the current attached ADB transports without initiating a connection."""
    client = AdbClient(host=config.ADB_HOST, port=config.ADB_PORT)
    return tuple(device.serial for device in client.device_list())


def _adb_endpoint_present(endpoint: str) -> bool:
    """Prove that exactly one currently attached transport matches this endpoint."""
    try:
        return sum(endpoint_matches(endpoint, serial) for serial in _adb_endpoint_rows()) == 1
    except Exception:
        return False


class _ManagerBridge(Protocol):
    def inspect(self) -> Mapping[str, int | float | str]: ...
    def capture(self, metadata: Mapping[str, int | float | str]) -> Image: ...
    def inspect_modal(self, parent: Mapping[str, int | float | str]) -> object: ...
    def capture_modal(self, parent: Mapping[str, int | float | str],
                      modal: Mapping[str, int | float | str]) -> Image: ...
    def press(self, metadata: Mapping[str, int | float | str],
              point: tuple[int, int], expected_label: str) -> None: ...
    def press_modal_clone(self, parent: Mapping[str, int | float | str],
                          modal: Mapping[str, int | float | str],
                          point: tuple[float, float]) -> None: ...


class _SwiftManagerBridge:
    """Mac-native implementation restricted to the manager inspect/capture/press API."""

    _HELPER = Path(__file__).with_name("macos_bluestacks_manager.swift")

    def _run(self, *args: str) -> str:
        if sys.platform != "darwin":
            raise HostCapabilityError("supported macOS manager window is required")
        try:
            result = subprocess.run(["swift", str(self._HELPER), *args], check=True,
                                    text=True, capture_output=True, timeout=20)
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or "").strip()
            if "Accessibility permission required" in message:
                raise HostCapabilityError("macOS Accessibility permission required") from exc
            if "manager window changed" in message:
                raise HostCapabilityError("manager window changed") from exc
            if "no visible exact manager window" in message:
                raise HostCapabilityError("exact manager window unavailable") from exc
            if "multiple visible exact manager windows" in message:
                raise HostCapabilityError("exact manager window is ambiguous") from exc
            raise HostCapabilityError("manager window unavailable") from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise HostCapabilityError("manager window unavailable") from exc
        return result.stdout.strip()

    def inspect(self) -> Mapping[str, int | float | str]:
        try:
            result = json.loads(self._run("inspect"))
        except (TypeError, ValueError) as exc:
            raise HostCapabilityError("manager window unavailable") from exc
        if not isinstance(result, Mapping):
            raise HostCapabilityError("manager window unavailable")
        return result

    @staticmethod
    def _arguments(metadata: Mapping[str, int | float | str]) -> list[str]:
        return [str(metadata[key]) for key in ("id", "x", "y", "width", "height")]

    @classmethod
    def _modal_capture_arguments(cls, parent: Mapping[str, int | float | str],
                                 modal: Mapping[str, int | float | str]) -> list[str]:
        return [str(parent[key]) for key in ("id", "pid", "x", "y", "width", "height")] + [
            str(modal[key]) for key in ("id", "pid", "x", "y", "width", "height")
        ]

    def capture(self, metadata: Mapping[str, int | float | str]) -> Image:
        try:
            encoded = self._run("capture", *self._arguments(metadata))
            buffer = np.frombuffer(base64.b64decode(encoded, validate=True), dtype=np.uint8)
            image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        except (ValueError, cv2.error) as exc:
            raise HostCapabilityError("manager window capture unavailable") from exc
        if image is None or image.ndim != 3 or image.shape[2] != 3:
            raise HostCapabilityError("manager window capture unavailable")
        return image

    def inspect_modal(self, parent: Mapping[str, int | float | str]) -> object:
        try:
            result = json.loads(self._run("modal-inspect", *self._arguments(parent)))
        except (TypeError, ValueError) as exc:
            raise HostCapabilityError("manager modal unavailable") from exc
        return result

    def capture_modal(self, parent: Mapping[str, int | float | str],
                      modal: Mapping[str, int | float | str]) -> Image:
        try:
            encoded = self._run("modal-capture", *self._modal_capture_arguments(parent, modal))
            buffer = np.frombuffer(base64.b64decode(encoded, validate=True), dtype=np.uint8)
            image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        except (ValueError, cv2.error) as exc:
            raise HostCapabilityError("manager modal capture unavailable") from exc
        if image is None or image.ndim != 3 or image.shape[2] != 3:
            raise HostCapabilityError("manager modal capture unavailable")
        return image

    def press(self, metadata: Mapping[str, int | float | str],
              point: tuple[int, int], expected_label: str) -> None:
        self._run("press", *self._arguments(metadata), str(point[0]), str(point[1]),
                  expected_label)

    def press_modal_clone(self, parent: Mapping[str, int | float | str],
                          modal: Mapping[str, int | float | str],
                          point: tuple[float, float]) -> None:
        self._run("modal-press-clone", *self._modal_capture_arguments(parent, modal),
                  str(point[0]), str(point[1]))


class MacOSMultiInstanceManager:
    """Capture and press only one visible ``BlueStacks Air Manager`` window."""

    _TITLE = "BlueStacks Air Manager"
    _MODAL_CAPTURE_RETRIES = 2

    def __init__(self, bridge: _ManagerBridge | None = None, *,
                 launcher: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep,
                 launch_timeout: float = 10.) -> None:
        self._bridge = bridge if bridge is not None else _SwiftManagerBridge()
        self._launcher = launcher if launcher is not None else self._launch_exact_manager
        self._clock = clock
        self._sleeper = sleeper
        self._launch_timeout = launch_timeout

    @staticmethod
    def _require_macos() -> None:
        if sys.platform != "darwin":
            raise HostCapabilityError("supported macOS manager window is required")

    @classmethod
    def _validated_metadata(cls, raw: Mapping[str, int | float | str]) -> dict[str, int | float | str]:
        if raw.get("title") != cls._TITLE:
            raise HostCapabilityError("exact manager title unavailable (manager window)")
        if raw.get("owner_path") not in _MANAGER_OWNER_EXECUTABLES:
            raise HostCapabilityError("exact manager owner unavailable")
        window_id = raw.get("id")
        if not isinstance(window_id, int) or isinstance(window_id, bool) or window_id <= 0:
            raise HostCapabilityError("exact manager window unavailable")
        pid = raw.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise HostCapabilityError("exact manager owner unavailable")
        metadata: dict[str, int | float | str] = {
            "id": window_id, "title": cls._TITLE, "owner_path": raw["owner_path"], "pid": pid,
        }
        for key in ("x", "y", "width", "height"):
            value = raw.get(key)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or (key in {"width", "height"} and value <= 0)):
                raise HostCapabilityError("exact manager window unavailable")
            metadata[key] = float(value)
        return metadata

    def _inspect_exact_manager(self) -> dict[str, int | float | str]:
        self._require_macos()
        try:
            return self._validated_metadata(self._bridge.inspect())
        except HostCapabilityError:
            raise
        except Exception as exc:
            raise HostCapabilityError("exact manager window unavailable") from exc

    def _capture(self, metadata: Mapping[str, int | float | str]) -> Image:
        try:
            return self._bridge.capture(metadata)
        except HostCapabilityError:
            raise
        except Exception as exc:
            raise HostCapabilityError("manager window capture unavailable") from exc

    @classmethod
    def _validated_modal(cls, raw: object, parent: Mapping[str, int | float | str]
                         ) -> dict[str, int | float | str]:
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], Mapping):
            raise HostCapabilityError("manager modal is missing or ambiguous")
        candidate = raw[0]
        if (parent.get("owner_path") != str(_MANAGER_EXECUTABLE)
                or candidate.get("parent_id") != parent.get("id")
                or candidate.get("pid") != parent.get("pid")):
            raise HostCapabilityError("manager modal parent is unavailable")
        window_id = candidate.get("id")
        title = candidate.get("title")
        if (not isinstance(window_id, int) or isinstance(window_id, bool) or window_id <= 0
                or window_id == parent.get("id") or title != ""):
            raise HostCapabilityError("manager modal is unavailable")
        modal: dict[str, int | float | str] = {
            "id": window_id, "title": title, "parent_id": parent["id"], "pid": parent["pid"],
        }
        for key in ("x", "y", "width", "height"):
            value = candidate.get(key)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or (key in {"width", "height"} and value <= 0)):
                raise HostCapabilityError("manager modal is unavailable")
            modal[key] = float(value)
        parent_x, parent_y = float(parent["x"]), float(parent["y"])
        parent_width, parent_height = float(parent["width"]), float(parent["height"])
        modal_x, modal_y = float(modal["x"]), float(modal["y"])
        modal_width, modal_height = float(modal["width"]), float(modal["height"])
        if (modal_width >= parent_width or modal_height >= parent_height
                or modal_x < parent_x or modal_y < parent_y
                or modal_x + modal_width > parent_x + parent_width
                or modal_y + modal_height > parent_y + parent_height
                or modal_x + modal_width / 2 != parent_x + parent_width / 2
                or modal_y + modal_height / 2 != parent_y + parent_height / 2):
            raise HostCapabilityError("manager modal parent is unavailable")
        return modal

    @staticmethod
    def _launch_exact_manager() -> None:
        if not _MANAGER_APP.is_dir():
            raise HostCapabilityError("BlueStacks Air Manager application is unavailable")
        try:
            subprocess.run(["/usr/bin/open", str(_MANAGER_APP)], check=True, text=True,
                           capture_output=True, timeout=20)
        except (subprocess.SubprocessError, OSError) as exc:
            raise HostCapabilityError("BlueStacks Air Manager could not be opened") from exc

    def ensure_open(self) -> ManagerFrame:
        """Return one exact Manager frame, launching only when it is absent."""
        try:
            return self.capture()
        except HostCapabilityError as exc:
            if str(exc) != "exact manager window unavailable":
                raise
        self._launcher()
        deadline = self._clock() + self._launch_timeout
        while True:
            try:
                return self.capture()
            except HostCapabilityError as exc:
                if str(exc) != "exact manager window unavailable":
                    raise
                if self._clock() >= deadline:
                    raise HostCapabilityError("BlueStacks Air Manager did not appear") from exc
                self._sleeper(.1)

    def _press_exact_manager(self, window_id: int, point: tuple[int, int],
                             expected_label: str) -> None:
        self._require_macos()
        if not isinstance(expected_label, str) or expected_label not in _MANAGER_CONTROLS:
            raise HostCapabilityError("exact manager control unavailable")
        if (not isinstance(window_id, int) or isinstance(window_id, bool) or window_id <= 0
                or not isinstance(point, tuple) or len(point) != 2
                or any(not isinstance(value, int) or isinstance(value, bool) for value in point)):
            raise HostCapabilityError("manager window changed")
        metadata = self._inspect_exact_manager()
        if metadata["id"] != window_id:
            raise HostCapabilityError("manager window changed")
        try:
            self._bridge.press(metadata, point, expected_label)
        except HostCapabilityError:
            raise
        except Exception as exc:
            raise HostCapabilityError("manager window changed") from exc

    def capture(self) -> ManagerFrame:
        metadata = self._inspect_exact_manager()
        return ManagerFrame(int(metadata["id"]), self._capture(metadata),
                            x=float(metadata["x"]), y=float(metadata["y"]),
                            width=float(metadata["width"]), height=float(metadata["height"]))

    def _capture_modal_evidence(self) -> tuple[ModalFrame, dict[str, int | float | str],
                                               dict[str, int | float | str]]:
        """Capture one verified companion-owned Manager modal without UI interaction."""
        for attempt in range(self._MODAL_CAPTURE_RETRIES + 1):
            try:
                parent = self._inspect_exact_manager()
                modal = self._validated_modal(self._bridge.inspect_modal(parent), parent)
                image = self._bridge.capture_modal(parent, modal)
            except HostCapabilityError as exc:
                if str(exc) == "manager window changed" and attempt < self._MODAL_CAPTURE_RETRIES:
                    continue
                raise
            except Exception as exc:
                raise HostCapabilityError("manager modal capture unavailable") from exc
            if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
                raise HostCapabilityError("manager modal capture unavailable")
            return (ModalFrame(int(modal["id"]), int(parent["id"]), str(modal["title"]), image,
                               float(modal["x"]), float(modal["y"]), float(modal["width"]),
                               float(modal["height"])), dict(parent), modal)
        raise HostCapabilityError("manager window changed")

    def capture_modal(self) -> ModalFrame:
        """Capture one verified companion-owned Manager modal without UI interaction."""
        frame, _, _ = self._capture_modal_evidence()
        return frame

    def press_clone_instance(self, verifier: ModalFrameVerifier | None = None) -> None:
        """Press only the Clone Instance row from one freshly verified popup."""
        frame, parent, modal = self._capture_modal_evidence()
        verifier = verifier if verifier is not None else ModalFrameVerifier()
        pixel_x, pixel_y = verifier.clone_point(frame)
        if frame.width <= 0 or frame.height <= 0:
            raise HostCapabilityError("manager modal profile is unavailable")
        image_height, image_width = frame.image.shape[:2]
        point = (frame.x + frame.width * pixel_x / image_width,
                 frame.y + frame.height * pixel_y / image_height)
        try:
            self._bridge.press_modal_clone(parent, modal, point)
        except HostCapabilityError:
            raise
        except Exception as exc:
            raise HostCapabilityError("manager modal changed") from exc

    def press(self, window_id: int, point: tuple[int, int], expected_label: str) -> None:
        """Press one allowlisted label after native window/control revalidation.

        This confirms only the native control press. Task 3 owns the subsequent
        configuration, process, and ADB endpoint observations for lifecycle state.
        """
        self._press_exact_manager(window_id, point, expected_label)


@dataclass(frozen=True)
class BlueStacksAirInventory:
    """Reconstruct named ADB ownership from the unmodified host configuration."""

    config_path: Path
    executable: Path
    process_rows: Callable[[], Mapping[str, bool]]

    def _validate_host(self) -> None:
        if sys.platform != "darwin" or self.executable != _EXECUTABLE:
            raise HostCapabilityError("supported macOS BlueStacks Air host is required")

    def _config_bytes(self) -> bytes:
        self._validate_host()
        try:
            return self.config_path.read_bytes()
        except OSError as exc:
            raise HostCapabilityError("BlueStacks Air inventory configuration is unavailable") from exc

    def digest(self) -> str:
        """Return the exact configuration fingerprint used by the live scope."""
        return hashlib.sha256(self._config_bytes()).hexdigest()

    def lease(self, name: str) -> str:
        """Bind a named instance lease to the complete current configuration."""
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise HostCapabilityError("BlueStacks Air inventory instance name is invalid")
        return hashlib.sha256(self._config_bytes() + b"\0" + name.encode("utf-8")).hexdigest()

    @staticmethod
    def _configured_instances(config: bytes) -> dict[str, tuple[int, str]]:
        """Bind each internal name to its Manager label and configured ADB port."""
        ports: dict[str, int] = {}
        labels: dict[str, str] = {}
        for raw_line in config.decode("utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if (match := _ADB_PORT.fullmatch(line)) is not None:
                name, raw_port = match.groups()
                port = int(raw_port)
                if not 1 <= port <= 65535 or name in ports:
                    raise HostCapabilityError("BlueStacks Air inventory contains ambiguous ADB endpoint")
                ports[name] = port
            elif _ADB_PORT_KEY.match(line):
                raise HostCapabilityError("BlueStacks Air inventory contains malformed ADB endpoint")
            elif (match := _DISPLAY_NAME.fullmatch(line)) is not None:
                name, label = match.groups()
                if name in labels:
                    raise HostCapabilityError("BlueStacks Air inventory contains ambiguous Manager label")
                labels[name] = label
            elif _DISPLAY_NAME_KEY.match(line):
                raise HostCapabilityError("BlueStacks Air inventory contains malformed Manager label")
        if len(set(ports.values())) != len(ports):
            raise HostCapabilityError("BlueStacks Air inventory contains duplicate ADB endpoint")
        if set(ports) != set(labels) or len(set(labels.values())) != len(labels):
            raise HostCapabilityError("BlueStacks Air inventory lacks an unambiguous Manager label")
        return {name: (port, labels[name]) for name, port in ports.items()}

    def display_name(self, name: str, *, digest: str | None = None) -> str:
        config = self._config_bytes()
        if digest is not None and hashlib.sha256(config).hexdigest() != digest:
            raise HostCapabilityError("BlueStacks Air inventory configuration changed")
        try:
            return self._configured_instances(config)[name][1]
        except KeyError as exc:
            raise HostCapabilityError("BlueStacks Air inventory instance is missing") from exc

    def snapshot(self) -> tuple[str, list[HostInstance]]:
        """Capture one configuration generation and derive all inventory evidence from it."""
        config = self._config_bytes()
        configured = self._configured_instances(config)
        try:
            running = self.process_rows()
        except Exception as exc:
            raise HostCapabilityError("BlueStacks Air inventory process observation is unavailable") from exc
        if not isinstance(running, Mapping) or any(not isinstance(value, bool)
                                                   for value in running.values()):
            raise HostCapabilityError("BlueStacks Air inventory process observation is invalid")
        digest = hashlib.sha256(config).hexdigest()
        instances = [HostInstance(
            name, f"127.0.0.1:{port}",
            hashlib.sha256(config + b"\0" + name.encode("utf-8")).hexdigest(),
            "running" if running.get(name, False) else "stopped",
        ) for name, (port, _) in sorted(configured.items())]
        return digest, instances

    def instances(self) -> list[HostInstance]:
        return self.snapshot()[1]


@dataclass(frozen=True)
class BlueStacksAirDriver:
    """Named manager lifecycle boundary with fresh, fail-closed evidence."""

    scope: QualificationScope
    inventory_source: BlueStacksAirInventory
    manager: MultiInstanceManager
    ocr_reader: Callable[[Image], tuple[ocr.TextBox, ...]] = ocr.read
    endpoint_present: Callable[[str], bool] = _adb_endpoint_present
    timeout: float = 10.
    poll_interval: float = .2
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def _capability_scope(self) -> QualificationScope | None:
        """Attest each advertised write capability from fresh host evidence."""
        scope = self.qualification_scope()
        if (scope is None or scope != self.scope
                or getattr(self.manager, "profile_version", None) != scope.bluestacks_version):
            return None
        endpoint = scope.instance_config.get("source_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return None
        try:
            frame = self.manager.capture()
        except Exception:
            return None
        if not isinstance(frame, ManagerFrame) or frame.window_id <= 0:
            return None
        if not self._endpoint_has_state(endpoint, present=True):
            return None
        return scope if self.qualification_scope() == scope else None

    @property
    def supports_lifecycle(self) -> bool:
        return self._capability_scope() is not None

    @property
    def supports_clone_staging(self) -> bool:
        return self._capability_scope() is not None

    @property
    def supports_m05_live_qualification(self) -> bool:
        return self._capability_scope() is not None

    def inventory(self) -> list[HostInstance]:
        return self.inventory_source.instances()

    def _exact_instance(self, name: str, *, required_state: str | None = None
                        ) -> tuple[str, HostInstance]:
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise HostCapabilityError("BlueStacks Air lifecycle instance name is invalid")
        try:
            digest, instances = self.inventory_source.snapshot()
        except HostCapabilityError:
            raise
        except Exception as exc:
            raise HostCapabilityError("BlueStacks Air lifecycle inventory is unavailable") from exc
        matches = [item for item in instances if item.name == name]
        if len(matches) != 1 or sum(item.endpoint == matches[0].endpoint for item in instances) != 1:
            raise HostCapabilityError("BlueStacks Air lifecycle instance is missing or ambiguous")
        instance = matches[0]
        if required_state is not None and instance.state != required_state:
            raise HostCapabilityError("BlueStacks Air lifecycle instance state is not actionable")
        return digest, instance

    def _row_control(self, name: str, *, action: str) -> _ManagerControl:
        """Find one exact named row and one exact adjacent action in one fresh frame."""
        display_name = getattr(self.inventory_source, "display_name", None)
        if not callable(display_name):
            # Simulation inventories model the Manager label as their internal name.
            return ManagerRowObservation(self.manager, self.ocr_reader).row_control(name, action=action)
        return ManagerRowObservation(self.manager, self.ocr_reader).row_control(
            display_name(name), action=action)

    def _endpoint_has_state(self, endpoint: str, *, present: bool) -> bool:
        """Return whether a fresh endpoint observation has the required strict state."""
        try:
            observed = self.endpoint_present(endpoint)
        except Exception:
            return False
        return isinstance(observed, bool) and observed == present

    def _wait_for(self, name: str, *, digest: str, endpoint: str, state: str) -> None:
        """Require new manager, inventory, process, and endpoint evidence by deadline."""
        deadline = self.clock() + self.timeout
        inverse_action = "Stop" if state == "running" else "Start"
        while True:
            current_digest, current = self._exact_instance(name)
            if current_digest != digest or current.endpoint != endpoint:
                raise HostCapabilityError("BlueStacks Air lifecycle configuration changed")
            try:
                layout_matches = self._row_control(name, action=inverse_action) is not None
            except HostCapabilityError:
                layout_matches = False
            endpoint_matches = self._endpoint_has_state(endpoint, present=state == "running")
            if current.state == state and layout_matches and endpoint_matches:
                return
            if self.clock() >= deadline:
                raise HostCapabilityError("BlueStacks Air lifecycle postcondition was not proven")
            self.sleep(min(self.poll_interval, max(0., deadline - self.clock())))

    def _prepress_evidence(self, name: str, *, action: str, before_state: str
                           ) -> tuple[str, HostInstance, _ManagerControl]:
        """Bind two consecutive fresh host observations to one unchanged manager control."""
        scope = self.qualification_scope()
        if scope is None:
            raise HostCapabilityError("BlueStacks Air lifecycle qualification scope is unavailable")
        digest, before = self._exact_instance(name, required_state=before_state)
        if scope.instance_config.get("configuration_digest") != digest:
            raise HostCapabilityError("BlueStacks Air lifecycle configuration changed")
        control = self._row_control(name, action=action)
        if not self._endpoint_has_state(before.endpoint, present=before_state == "running"):
            raise HostCapabilityError("BlueStacks Air lifecycle endpoint precondition is not proven")
        final_scope = self.qualification_scope()
        if final_scope is None:
            raise HostCapabilityError("BlueStacks Air lifecycle qualification scope changed")
        final_digest, final = self._exact_instance(name, required_state=before_state)
        if (final_scope.instance_config.get("configuration_digest") != digest
                or final_digest != digest or final.endpoint != before.endpoint):
            raise HostCapabilityError("BlueStacks Air lifecycle configuration changed")
        final_control = self._row_control(name, action=action)
        if final_control != control:
            raise HostCapabilityError("BlueStacks Air lifecycle manager row changed")
        if not self._endpoint_has_state(final.endpoint, present=before_state == "running"):
            raise HostCapabilityError("BlueStacks Air lifecycle endpoint precondition is not proven")
        return digest, final, final_control

    def _lifecycle(self, name: str, *, action: str, before_state: str, after_state: str) -> None:
        if self.timeout < 0 or self.poll_interval <= 0:
            raise ValueError("bounded lifecycle settings required")
        digest, before, control = self._prepress_evidence(name, action=action,
                                                           before_state=before_state)
        self.manager.press(control.window_id, control.point, action)
        self._wait_for(name, digest=digest, endpoint=before.endpoint, state=after_state)

    def start(self, name: str) -> None:
        self._lifecycle(name, action="Start", before_state="stopped", after_state="running")

    def stop(self, name: str) -> None:
        self._lifecycle(name, action="Stop", before_state="running", after_state="stopped")

    def _clone_snapshot(self) -> tuple[str, tuple[HostInstance, ...]]:
        """Read one complete clone pre/postcondition inventory generation."""
        try:
            digest, instances = self.inventory_source.snapshot()
        except HostCapabilityError:
            raise
        except Exception as exc:
            raise HostCapabilityError("BlueStacks Air clone inventory is unavailable") from exc
        names = [item.name for item in instances]
        endpoints = [item.endpoint for item in instances]
        if len(names) != len(set(names)) or len(endpoints) != len(set(endpoints)):
            raise HostCapabilityError("BlueStacks Air clone inventory is ambiguous")
        return digest, tuple(instances)

    def _validate_next_clone_name(self, name: str, source: str) -> tuple[str, tuple[HostInstance, ...]]:
        """Validate a requested target before changing the source lifecycle state."""
        scope = self.qualification_scope()
        if scope is None or scope.instance_config.get("source_instance") != source:
            raise HostCapabilityError("BlueStacks Air clone source is unavailable")
        digest, instances = self._clone_snapshot()
        if scope.instance_config.get("configuration_digest") != digest:
            raise HostCapabilityError("BlueStacks Air clone configuration changed")
        source_rows = [item for item in instances if item.name == source]
        if len(source_rows) != 1:
            raise HostCapabilityError("BlueStacks Air clone source is missing or ambiguous")
        match = re.fullmatch(r"(.+_)([1-9][0-9]*)", source)
        prefix = match.group(1) if match is not None else f"{source}_"
        suffixes = [int(candidate.group(1)) for item in instances
                    if (candidate := re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)", item.name))]
        if name != f"{prefix}{max(suffixes, default=0) + 1}":
            raise HostCapabilityError("BlueStacks Air next clone name is required")
        return digest, instances

    def _require_next_clone_name(self, name: str, source: str) -> tuple[str, tuple[HostInstance, ...]]:
        """Bind the already-validated clone request to a stopped, isolated source."""
        digest, instances = self._validate_next_clone_name(name, source)
        source_rows = [item for item in instances if item.name == source]
        if len(source_rows) != 1 or source_rows[0].state != "stopped":
            raise HostCapabilityError("BlueStacks Air clone source is missing or not stopped")
        if not self._endpoint_has_state(source_rows[0].endpoint, present=False):
            raise HostCapabilityError("BlueStacks Air clone source endpoint is not proven")
        return digest, instances

    def _modal_control(self, label: str, *, failure: str) -> _ManagerControl:
        """Resolve one exact labelled modal control from a newly captured frame."""
        return self._modal_controls((label,), failure=failure)[label]

    def _modal_controls(self, labels: tuple[str, ...], *, failure: str) -> dict[str, _ManagerControl]:
        """Resolve all expected modal labels from one newly captured frame."""
        try:
            frame = self.manager.capture()
            boxes = self.ocr_reader(frame.image)
        except HostCapabilityError:
            raise
        except Exception as exc:
            raise HostCapabilityError(failure) from exc
        controls: dict[str, _ManagerControl] = {}
        for label in labels:
            matches = [box for box in boxes if box.text == label]
            if len(matches) != 1:
                raise HostCapabilityError(failure)
            box = matches[0]
            controls[label] = _ManagerControl(frame.window_id, ManagerRowObservation._global_point(
                frame, (box.rect.x + box.rect.w // 2, box.rect.y + box.rect.h // 2)))
        return controls

    def _open_clone_dialog(self, source: str) -> None:
        del source
        control = self._modal_control("Instance", failure="BlueStacks Air manager Instance control is unavailable")
        self.manager.press(control.window_id, control.point, "Instance")

    def _stop_clone_source_if_running(self, source: str) -> bool:
        """Stop the scoped clone source and remember whether it must be restored."""
        _, instances = self._clone_snapshot()
        source_rows = [item for item in instances if item.name == source]
        if len(source_rows) != 1:
            raise HostCapabilityError("BlueStacks Air clone source is missing or ambiguous")
        if source_rows[0].state == "running":
            self.stop(source)
            return True
        if source_rows[0].state != "stopped":
            raise HostCapabilityError("BlueStacks Air clone source state is unavailable")
        return False

    def _restore_clone_source(self, source: str) -> None:
        """Start a source that this clone transaction stopped after creation is proven."""
        if self.timeout < 0 or self.poll_interval <= 0:
            raise ValueError("bounded lifecycle settings required")
        digest, before = self._exact_instance(source, required_state="stopped")
        if not self._endpoint_has_state(before.endpoint, present=False):
            raise HostCapabilityError("BlueStacks Air clone source endpoint is not proven")
        control = self._row_control(source, action="Start")
        final_digest, final = self._exact_instance(source, required_state="stopped")
        final_control = self._row_control(source, action="Start")
        if final_digest != digest or final.endpoint != before.endpoint or final_control != control:
            raise HostCapabilityError("BlueStacks Air clone source changed before restore")
        if not self._endpoint_has_state(final.endpoint, present=False):
            raise HostCapabilityError("BlueStacks Air clone source endpoint is not proven")
        self.manager.press(control.window_id, control.point, "Start")
        self._wait_for(source, digest=digest, endpoint=before.endpoint, state="running")

    def _select_exact_source(self, name: str, source: str, *, before_digest: str) -> None:
        """Rebind host and menu evidence immediately before opening the clone form."""
        initial_control = self._modal_control(
            "Clone instance", failure="BlueStacks Air clone dialog is unavailable")
        final_digest, _ = self._require_next_clone_name(name, source)
        if final_digest != before_digest:
            raise HostCapabilityError("BlueStacks Air clone configuration changed")
        final_control = self._modal_control(
            "Clone instance", failure="BlueStacks Air clone dialog is unavailable")
        if final_control != initial_control:
            raise HostCapabilityError("BlueStacks Air clone dialog changed")
        self.manager.press(final_control.window_id, final_control.point, "Clone instance")

    def _exact_create_form_control(self, source: str) -> _ManagerControl:
        """Prove the form's one selected source and one clone before Create."""
        try:
            frame = self.manager.capture()
            boxes = self.ocr_reader(frame.image)
        except HostCapabilityError:
            raise
        except Exception as exc:
            raise HostCapabilityError("BlueStacks Air clone create form is unavailable") from exc
        fields = [box for box in boxes if box.text == "Source"]
        source_boxes = [box for box in boxes if box.text == source]
        candidates = [box for box in boxes if re.fullmatch(r".+_[1-9][0-9]*", box.text)]
        expected_numbered_sources = 1 if re.fullmatch(r".+_[1-9][0-9]*", source) else 0
        if (len(fields) != 1 or len(source_boxes) != 1
                or len(candidates) != expected_numbered_sources):
            raise HostCapabilityError("BlueStacks Air clone create form source mismatch")
        field, selected = fields[0], source_boxes[0]
        same_row = abs((field.rect.y + field.rect.h // 2)
                       - (selected.rect.y + selected.rect.h // 2)) <= max(field.rect.h, selected.rect.h)
        selected_is_right_of_field = selected.rect.x >= field.rect.x + field.rect.w
        if not same_row or not selected_is_right_of_field:
            raise HostCapabilityError("BlueStacks Air clone create form source mismatch")
        counts = [box for box in boxes if box.text == "1"]
        creates = [box for box in boxes if box.text == "Create"]
        if len(counts) != 1 or len(creates) != 1:
            raise HostCapabilityError("BlueStacks Air clone create form is unavailable")
        create = creates[0]
        return _ManagerControl(
            frame.window_id, (create.rect.x + create.rect.w // 2,
                              create.rect.y + create.rect.h // 2))

    def _confirm_exact_create(self, name: str, source: str, *, before_digest: str) -> None:
        """Reprove source/configuration and modal state immediately before Create."""
        final_digest, _ = self._require_next_clone_name(name, source)
        if final_digest != before_digest:
            raise HostCapabilityError("BlueStacks Air clone configuration changed")
        control = self._exact_create_form_control(source)
        self.manager.press(control.window_id, control.point, "Create")

    def _require_exact_new_instance(self, name: str, *, before_digest: str,
                                    before: tuple[HostInstance, ...]) -> HostInstance:
        """Accept only one new configured/process/ADB instance record by the deadline."""
        before_names = {item.name for item in before}
        deadline = self.clock() + self.timeout
        while True:
            digest, current = self._clone_snapshot()
            current_names = {item.name for item in current}
            new_names = current_names - before_names
            if digest != before_digest and new_names == {name} and before_names <= current_names:
                created = [item for item in current if item.name == name]
                if (len(created) == 1
                        and sum(item.endpoint == created[0].endpoint for item in current) == 1
                        and self._endpoint_has_state(created[0].endpoint,
                                                     present=created[0].state == "running")):
                    return created[0]
            if self.clock() >= deadline:
                raise HostCapabilityError("BlueStacks Air clone postcondition was not proven")
            self.sleep(min(self.poll_interval, max(0., deadline - self.clock())))

    def stage_clone(self, name: str, source: str) -> HostInstance:
        """Create one scoped, deterministically named clone through the manager UI."""
        if self.timeout < 0 or self.poll_interval <= 0:
            raise ValueError("bounded clone settings required")
        self._validate_next_clone_name(name, source)
        restore_source = self._stop_clone_source_if_running(source)
        before_digest, before = self._require_next_clone_name(name, source)
        self._open_clone_dialog(source)
        self._select_exact_source(name, source, before_digest=before_digest)
        self._confirm_exact_create(name, source, before_digest=before_digest)
        created = self._require_exact_new_instance(name, before_digest=before_digest, before=before)
        if restore_source:
            self._restore_clone_source(source)
        return created

    def qualification_scope(self) -> QualificationScope | None:
        """Return the captured scope only while fresh host evidence still agrees."""
        if not self.scope.valid():
            return None
        details = self.scope.instance_config
        digest = details.get("configuration_digest")
        source_name = details.get("source_instance")
        source_endpoint = details.get("source_endpoint")
        if not all(isinstance(value, str) and value for value in
                   (digest, source_name, source_endpoint)):
            return None
        try:
            current_digest, instances = self.inventory_source.snapshot()
        except HostCapabilityError:
            return None
        if digest != current_digest:
            return None
        source = [item for item in instances if item.name == source_name]
        if len(source) != 1 or source[0].endpoint != source_endpoint:
            return None
        version = getattr(self.manager, "game_version", None)
        if callable(version):
            try:
                version = version()
            except Exception:
                return None
        source_version = getattr(self.manager, "source_version", None)
        if callable(source_version):
            try:
                source_version = source_version()
            except Exception:
                return None
        return (self.scope if (version == self.scope.game_version
                               and source_version == self.scope.source_version) else None)
