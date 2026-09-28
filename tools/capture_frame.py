"""Save one full-resolution frame from an emulator ADB port.

    venv/bin/python tools/capture_frame.py --port 6115 out.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device import capture_screen, connect_device  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("out")
    args = parser.parse_args(argv)
    frame = capture_screen(connect_device(host=args.host, port=args.port))
    cv2.imwrite(args.out, frame)
    print(f"Saved {args.out} ({frame.shape[1]}x{frame.shape[0]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
