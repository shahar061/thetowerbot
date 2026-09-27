"""Production recovery requests carry a bounded, redacted frame (seam 5 C1)."""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import cv2
import httpx2
import numpy as np
import pytest

from openrouter_recovery import _MAX_IMAGE_BYTES, _MAX_IMAGE_DIMENSION, _raster_dimensions
from recovery_image import encode_recovery_image
from tests.test_recovery_coordinator import Scans, configure, context
from tests.test_recovery_service import Exchange, completion, metadata


def test_scanned_frame_becomes_a_bounded_png_without_metadata() -> None:
    frame = cv2.imread(str(Path('tests/fixtures/menu_main.png')))
    image = encode_recovery_image(frame)
    assert image is not None and image.media_type == 'image/png'
    assert len(image.data) <= _MAX_IMAGE_BYTES
    assert max(image.width, image.height) <= _MAX_IMAGE_DIMENSION
    assert _raster_dimensions(image.media_type, image.data) == (image.width, image.height)
    assert b'tEXt' not in image.data and b'iTXt' not in image.data
    noise = np.random.default_rng(1).integers(0, 255, (2400, 1080, 3), dtype=np.uint8)
    bounded = encode_recovery_image(noise)
    assert bounded is None or len(bounded.data) <= _MAX_IMAGE_BYTES
    assert encode_recovery_image(None) is None


class _Proposal(Exchange):
    async def handle(self, item: httpx2.Request) -> httpx2.Response:
        self.requests.append(item.method)
        if item.method == 'GET':
            return httpx2.Response(200, json=metadata())
        body = completion(0)
        body['choices'][0]['message']['content'] = json.dumps({
            'action_id': 'close_overlay', 'candidate_id': 'dismiss:CLOSE',
            'expected_postcondition': 'overlay_absent', 'explanation': 'close the overlay'})
        return httpx2.Response(200, json=body)


def _coordinator(tmp_path: Path, exchange: Exchange) -> Any:
    from recovery_coordinator import RecoveryCoordinator
    from recovery_service import RecoveryService

    def service(root: Path, **kwargs: Any) -> RecoveryService:
        return RecoveryService(root, client_factory=exchange.factory, **kwargs)

    return RecoveryCoordinator(tmp_path, worker='worker-a', key_loader=lambda: 'synthetic-key',
                               service_factory=service)


def _run(coordinator: Any, predicate: Any, *, image: Any, dispatched: list[Any],
         paused: list[int], timeout: float = 5.) -> None:
    scans = Scans()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        coordinator.step(scans(), stalled=True, image=image,
                         dispatch=lambda *a: dispatched.append(a),
                         pause=lambda: paused.append(1))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError(coordinator.status())


def test_real_coordinator_and_service_send_one_request_with_frame_in_shadow(tmp_path: Path) -> None:
    configure(tmp_path)  # shadow for worker-a
    exchange = _Proposal()
    coordinator = _coordinator(tmp_path, exchange)
    frame = cv2.imread(str(Path('tests/fixtures/menu_main.png')))
    encoded: list[int] = []

    def image() -> Any:
        encoded.append(1)
        return encode_recovery_image(frame)

    dispatched: list[Any] = []
    paused: list[int] = []
    try:
        _run(coordinator, lambda: coordinator.status().get('last_proposal') is not None
             and bool(paused), image=image, dispatched=dispatched, paused=paused)
    finally:
        coordinator.close()
    assert exchange.requests == ['GET', 'POST']  # Exactly one metadata and one completion.
    assert coordinator.status()['last_proposal'] == dict(
        action_id='close_overlay', candidate_id='dismiss:CLOSE',
        expected_postcondition='overlay_absent')
    assert dispatched == []  # Shadow never inputs.
    assert encoded == [1]  # The frame is encoded once, only for the request.


def test_missing_frame_never_reaches_the_provider(tmp_path: Path) -> None:
    configure(tmp_path)
    exchange = _Proposal()
    coordinator = _coordinator(tmp_path, exchange)
    dispatched: list[Any] = []
    paused: list[int] = []
    try:
        _run(coordinator, lambda: coordinator.status().get('blocker') == 'image_unavailable',
             image=lambda: None, dispatched=dispatched, paused=paused)
    finally:
        coordinator.close()
    assert exchange.requests == []
