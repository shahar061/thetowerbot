"""Walk the in-battle speed arrows and cut a readout template per step.

config.SPEED_TEMPLATE_VALUES shipped with x1.0 alone, because x1.0 is the only speed
there is a committed frame for. This is the tool that fills in the rest: it
taps `+` on a live run, saves the readout crop each time it changes, and
stops when tapping stops changing it - which is how the account's actual
ceiling is discovered rather than guessed.

It deliberately does NOT name the values. Nothing here can read "x2.0" off a
crop - reading the readout is exactly the thing the templates are needed for,
so a tool that could name them would not be necessary. It writes numbered
files and leaves the labelling to you, the same division of labour
tools/label_glyphs.py already uses for the digit atlas.

Usage, with a battle running and the game speed turned all the way DOWN:

    uv run tools/harvest_speed_glyphs.py

Then:
  1. Look at the files it wrote in templates/speed/harvest/.
  2. Rename each to templates/speed/x<value>.png - x1.0, x2.0, and so on,
     one decimal place, matching what the crop shows.
  3. Put the same values in config.SPEED_TEMPLATE_VALUES, ascending.
  4. Run: uv run pytest tests/test_speed_loop.py -k template

Step 4 is the check that they agree; it fails naming any value with no
template.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import screens  # noqa: E402
import vision  # noqa: E402
from device import Image, tap  # noqa: E402
from tower_bot import capture_screen, connect_device  # noqa: E402

OUT_DIR = config.TEMPLATE_DIR / "speed" / "harvest"

# Two crops this close are the same reading. Not zero: the play area behind
# the widget is animating, and the readout's own anti-aliasing shifts by a
# grey level or two between frames.
SAME_READING = 2.0


def readout(screen: Image, anchor: tuple[int, int]) -> Image:
    region = config.SPEED_READOUT_REGION
    x = anchor[0] + region.dx
    y = anchor[1] + region.dy
    return screen[y : y + region.h, x : x + region.w]


def differs(a: Image, b: Image) -> bool:
    return float(np.abs(a.astype(float) - b.astype(float)).mean()) > SAME_READING


def arrow_point(anchor: tuple[int, int], direction: str) -> tuple[int, int]:
    region = (
        config.SPEED_PLUS_REGION if direction == "up" else config.SPEED_MINUS_REGION
    )
    return (
        anchor[0] + region.dx + region.w // 2,
        anchor[1] + region.dy + region.h // 2,
    )


def in_run_anchor(screen: Image, cache: vision.TemplateCache) -> tuple[int, int] | None:
    reading = screens.classify(screen, cache)
    if reading.state is not screens.ScreenState.IN_RUN or reading.top_left is None:
        return None
    return reading.top_left


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-steps", type=int, default=12,
        help="give up after this many taps (default: 12)",
    )
    parser.add_argument(
        "--settle", type=float, default=0.6,
        help="seconds to wait after a tap before capturing (default: 0.6)",
    )
    args = parser.parse_args(argv[1:])

    device = connect_device(host=config.DEVICE_HOST, port=config.DEVICE_PORT)
    cache = vision.TemplateCache(config.TEMPLATE_DIR)

    anchor = in_run_anchor(capture_screen(device), cache)
    if anchor is None:
        print(
            "Not on the battle screen. Start a run, turn the speed all the "
            "way down, and try again.",
            file=sys.stderr,
        )
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    crops: list[Image] = [readout(capture_screen(device), anchor)]
    print(f"step 0: captured the starting readout (anchor {anchor})")

    taps = 0
    for step in range(1, args.max_steps + 1):
        tap(device, *arrow_point(anchor, "up"))
        taps += 1
        time.sleep(args.settle)

        screen = capture_screen(device)
        # Re-derived every pass: a run that ends mid-harvest would otherwise
        # have every later crop measured from an anchor that is no longer on
        # screen, and they would all look identical - which this would read
        # as "reached the ceiling" and report as a result.
        moved = in_run_anchor(screen, cache)
        if moved is None:
            print(f"step {step}: left the battle screen - stopping here", file=sys.stderr)
            break
        anchor = moved

        crop = readout(screen, anchor)
        if not differs(crop, crops[-1]):
            print(f"step {step}: readout did not change - that is the ceiling")
            break
        crops.append(crop)
        print(f"step {step}: new reading")
    else:
        print(f"hit --max-steps ({args.max_steps}) without finding a ceiling")

    for index, crop in enumerate(crops):
        path = OUT_DIR / f"step_{index}.png"
        cv2.imwrite(str(path), crop)

    # Put it back where it was found. Harvesting is not meant to change how
    # the account plays, and leaving the game at maximum speed after a probe
    # would be a side effect nobody asked for.
    for _ in range(taps):
        tap(device, *arrow_point(anchor, "down"))
        time.sleep(args.settle)

    print(f"\nWrote {len(crops)} crops to {OUT_DIR}")
    print("Rename each to templates/speed/x<value>.png, then list the values")
    print("in config.SPEED_TEMPLATE_VALUES and run:")
    print("  uv run pytest tests/test_speed_loop.py -k template")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
