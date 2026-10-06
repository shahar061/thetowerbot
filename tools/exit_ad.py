"""Drive an emulator out of a rewarded ad with the bot's own exit detection.

Taps each skip or close `ad_exit.find_close` locates, from a frame under
5 s old like the supervisor requires, until The Tower has focus again.
Run it only while that emulator's worker is stopped.

    venv/bin/python tools/exit_ad.py --port 6385
    venv/bin/python tools/exit_ad.py --port 6385 --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ad_exit  # noqa: E402
from device import capture_screen, connect_device, tap  # noqa: E402
from vision import TemplateCache  # noqa: E402

FRESH_SECONDS = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--dry-run", action="store_true",
                        help="report the close each frame shows, never tap")
    args = parser.parse_args(argv)
    device = connect_device(host=args.host, port=args.port)
    templates = TemplateCache(ROOT / "templates")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if ad_exit.game_foreground(device):
            print("The Tower has focus; the ad is gone.")
            return 0
        if not ad_exit.ad_foreground(device):
            print("Neither the game nor an ad has focus; stopping.")
            return 2
        captured = time.monotonic()
        close = ad_exit.find_close(capture_screen(device), templates, device)
        age = time.monotonic() - captured
        if close is None:
            print(f"no skip or close yet ({age:.1f}s)")
        elif args.dry_run:
            print(f"would tap {close} ({age:.1f}s after capture)")
            return 0
        elif age > FRESH_SECONDS:
            print(f"found {close} but the frame is {age:.1f}s old; recapturing")
            continue
        else:
            tap(device, *close)
            print(f"tapped {close} ({age:.1f}s after capture)")
        time.sleep(2)
    print(f"the ad still has focus after {args.timeout:.0f}s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
