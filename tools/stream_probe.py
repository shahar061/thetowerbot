"""Watch a worker's live stream from the terminal and report frames, fps and bitrate.

    uv run tools/stream_probe.py 10199 --seconds 20
"""

from __future__ import annotations

import argparse
import json
import time

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect


def main() -> int:
    parser = argparse.ArgumentParser(description="Report a worker's live stream frame rate and bitrate.")
    parser.add_argument("port", type=int, help="the worker's web port")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args()
    frames = keys = size = 0
    first: float | None = None
    started = time.monotonic()
    try:
        with connect(f"ws://{args.host}:{args.port}/api/stream", open_timeout=5) as ws:
            while time.monotonic() - started < args.seconds:
                try:
                    message = ws.recv(timeout=1.0)
                except TimeoutError:
                    continue
                if isinstance(message, str):
                    print("config", json.loads(message))
                    continue
                if first is None:
                    first = time.monotonic()
                    print(f"first frame after {first - started:.2f}s")
                frames += 1
                keys += message[0] & 1
                size += len(message)
    except ConnectionClosed as closed:
        print(f"closed by the worker: code={closed.rcvd.code if closed.rcvd else None}")
        return 1
    elapsed = max(time.monotonic() - (first or started), 1e-6)
    print(f"{frames} frames ({keys} keyframes) in {elapsed:.1f}s = "
          f"{frames / elapsed:.1f} fps, {size * 8 / elapsed / 1e6:.2f} Mbps")
    return 0 if frames else 1


if __name__ == "__main__":
    raise SystemExit(main())
