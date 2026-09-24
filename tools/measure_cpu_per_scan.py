"""CPU seconds per scan for one live fleet worker (spec invariant 5).

    .venv/bin/python tools/measure_cpu_per_scan.py --worker-id Tiramisu64_36
    .venv/bin/python tools/measure_cpu_per_scan.py --summarize profile.raw

Samples `ps -o cputime=` and the worker's /api/status `scans` counter,
waits, samples again, and prints CPU seconds per scan. It prints the py-spy
command for the matching 30 s profile rather than running it, because
py-spy needs sudo.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass

DEFAULT_NEEDLES: dict[str, str] = {
    "ocr.read": r"\bread \([^)]*ocr\.py:",
    "screens.classify": r"\bclassify \([^)]*screens\.py:",
    "pages.classify_page": r"\bclassify_page \([^)]*pages\.py:",
    "account_screens.scan": r"\bscan \([^)]*account_screens\.py:",
    "device.capture_screen": r"\bcapture_screen \([^)]*device\.py:",
}


@dataclass(frozen=True)
class Sample:
    cpu: float
    scans: int


def parse_cputime(text: str) -> float:
    """`ps -o cputime=` output, `[[dd-]hh:]mm:ss[.ff]`, as seconds."""
    text = text.strip()
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        days = int(day_text)
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return days * 86400 + seconds


def find_worker(worker_id: str, ps_output: str) -> tuple[int, int]:
    """(pid, web port) of the tower_bot.py process running `worker_id`."""
    for line in ps_output.splitlines():
        fields = line.split()
        if "tower_bot.py" not in line or "--worker-id" not in fields or "--web-port" not in fields:
            continue
        if fields[fields.index("--worker-id") + 1] == worker_id:
            return int(fields[0]), int(fields[fields.index("--web-port") + 1])
    raise LookupError(f"no running tower_bot.py worker {worker_id!r}")


def cpu_per_scan(before: Sample, after: Sample) -> float | None:
    scans = after.scans - before.scans
    return None if scans <= 0 else (after.cpu - before.cpu) / scans


def summarize(raw: str, needles: dict[str, str]) -> dict[str, float]:
    """Share of all py-spy raw samples whose stack matches each needle."""
    total = 0
    hits = {name: 0 for name in needles}
    patterns = {name: re.compile(pattern) for name, pattern in needles.items()}
    for line in raw.splitlines():
        stack, _, count_text = line.rstrip().rpartition(" ")
        if not stack or not count_text.isdigit():
            continue
        count = int(count_text)
        total += count
        for name, pattern in patterns.items():
            if pattern.search(stack):
                hits[name] += count
    return {name: (hits[name] / total if total else 0.0) for name in needles}


def _sample(pid: int, port: int) -> Sample:
    cpu = parse_cputime(subprocess.run(["ps", "-o", "cputime=", "-p", str(pid)],
                                       capture_output=True, text=True, check=True).stdout)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as response:
        scans = int(json.load(response)["scans"])
    return Sample(cpu, scans)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worker-id")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--summarize", metavar="RAW_PROFILE")
    args = parser.parse_args(argv)
    if args.summarize:
        with open(args.summarize, encoding="utf-8") as handle:
            for name, share in summarize(handle.read(), DEFAULT_NEEDLES).items():
                print(f"{name:24s} {share:6.1%}")
        return 0
    if not args.worker_id:
        parser.error("--worker-id is required unless --summarize is given")
    ps_output = subprocess.run(["ps", "-axo", "pid=,command="],
                               capture_output=True, text=True, check=True).stdout
    pid, port = find_worker(args.worker_id, ps_output)
    before = _sample(pid, port)
    time.sleep(args.seconds)
    after = _sample(pid, port)
    per_scan = cpu_per_scan(before, after)
    print(f"worker {args.worker_id} pid {pid} port {port}")
    print(f"cpu {after.cpu - before.cpu:.2f} s over {args.seconds:.0f} s "
          f"({(after.cpu - before.cpu) / args.seconds:.0%}), scans {after.scans - before.scans}")
    print("cpu per scan: " + ("n/a (no scans)" if per_scan is None else f"{per_scan:.3f} s"))
    print(f"profile: sudo ~/.local/bin/py-spy record --pid {pid} --duration 30 --format raw -o <file>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
