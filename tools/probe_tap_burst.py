"""Time one tap burst on a live battle row and count the levels the game took.

Pause the worker first (dashboard Pause) so the bot sends no input, open its
battle upgrade panel on the tab showing UPGRADE with cash for every level,
then run:

    uv run python tools/probe_tap_burst.py 127.0.0.1:5555 attack_speed --taps 10 --gap 0
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device import burst_command, capture_screen, connect_device  # noqa: E402
from fleet.battle_prices import catalog, price_matches  # noqa: E402
from perception import ObservedUpgrade, observe_frame  # noqa: E402


def read_row(device: object, upgrade_id: str) -> ObservedUpgrade | None:
    observation = observe_frame(capture_screen(device), "battle")
    return next((row for row in observation.rows if row.upgrade_id == upgrade_id), None)


def level(row: ObservedUpgrade | None, curve: list[int]) -> int | None:
    if row is None or type(row.price) is not int:
        return None
    matches = [i for i, price in enumerate(curve) if price_matches(price, row.price, row.raw_price)]
    return matches[0] if len(matches) == 1 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", help="the worker's ADB endpoint, host:port")
    parser.add_argument("upgrade_id")
    parser.add_argument("--taps", type=int, default=10)
    parser.add_argument("--gap", type=float, default=0.0)
    args = parser.parse_args()
    host, _, port = args.endpoint.rpartition(":")
    device = connect_device(host=host, port=int(port))
    curve = catalog()["curves"][args.upgrade_id]
    before = read_row(device, args.upgrade_id)
    start = level(before, curve)
    if before is None or before.tap is None or start is None:
        print("The row is not visible with a readable price; open its tab and retry.")
        return 1
    print(f"start level {start}, price {curve[start]}, "
          f"cost of {args.taps} levels {sum(curve[start:start + args.taps])}")
    began = time.monotonic()
    device.shell(burst_command(*before.tap, args.taps, args.gap))
    took = time.monotonic() - began
    time.sleep(1.0)
    end = level(read_row(device, args.upgrade_id), curve)
    registered = None if end is None else end - start
    print(f"burst of {args.taps} taps, gap {args.gap}s: took {took:.2f}s, "
          f"registered {registered} levels")
    return 0 if registered == args.taps else 2


if __name__ == "__main__":
    raise SystemExit(main())
