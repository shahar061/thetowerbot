"""Expose the dashboard and every running worker dashboard on the tailnet.

Every server here binds loopback on purpose (see config.WEB_HOST): no auth,
live screenshots, and a control plane that writes to disk. Tailscale Serve
keeps it that way. The tailscale daemon proxies https://<mac>.<tailnet>.ts.net
to 127.0.0.1, and only devices signed in to the same tailnet can reach it.

The main dashboard goes on 443. Each worker dashboard keeps its own port
(10000 + emulator number) so the UI can reach it at the same host, see
reachableDashboardUrl in web/ui/lib/api.ts. Workers come and go with every
reroll, so this reconciles instead of configuring once: it asks the main
dashboard which workers are running, serves those, and unserves worker ports
that are gone. It only touches ports it owns (443 and the worker range).

    uv run tools/tailscale_serve.py            # sync once
    uv run tools/tailscale_serve.py --watch    # keep syncing every 20s
    uv run tools/tailscale_serve.py --off      # remove everything it served
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

MAIN_PORT = 8765
MAIN_HTTPS = 443
WORKER_PORTS = range(10000, 11000)


def desired(accounts: list[dict[str, object]]) -> dict[int, int]:
    """HTTPS port on the tailnet -> local port, for the main and running workers."""
    ports = {MAIN_HTTPS: MAIN_PORT}
    for account in accounts:
        url = account.get("dashboard_url")
        if not account.get("running") or not isinstance(url, str):
            continue
        port = urlsplit(url).port
        if port in WORKER_PORTS:
            ports[port] = port
    return ports


def served(status: dict[str, object]) -> set[int]:
    """HTTPS ports this script owns that Tailscale Serve currently exposes."""
    tcp = status.get("TCP") or {}
    assert isinstance(tcp, dict)
    return {int(port) for port in tcp if int(port) == MAIN_HTTPS or int(port) in WORKER_PORTS}


def tailscale(*args: str) -> str:
    return subprocess.run(["tailscale", *args], check=True, capture_output=True, text=True).stdout


def sync(main: str) -> None:
    with urlopen(f"{main}/api/accounts", timeout=5) as response:
        want = desired(json.load(response)["accounts"])
    have = served(json.loads(tailscale("serve", "status", "--json") or "{}"))
    for port in sorted(have - want.keys()):
        tailscale("serve", f"--https={port}", "off")
        print(f"unserved :{port}")
    for port, local in sorted(want.items()):
        if port not in have:
            tailscale("serve", "--bg", f"--https={port}", f"http://127.0.0.1:{local}")
            print(f"served :{port} -> 127.0.0.1:{local}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--main", default=f"http://127.0.0.1:{MAIN_PORT}", help="main dashboard URL")
    parser.add_argument("--watch", action="store_true", help="keep syncing as workers come and go")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--off", action="store_true", help="remove every port this script serves")
    args = parser.parse_args()

    if args.off:
        for port in sorted(served(json.loads(tailscale("serve", "status", "--json") or "{}"))):
            tailscale("serve", f"--https={port}", "off")
            print(f"unserved :{port}")
        return 0

    while True:
        try:
            sync(args.main)
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            detail = getattr(error, "stderr", "") or error
            print(f"sync failed: {detail}", file=sys.stderr)
            if not args.watch:
                return 1
        if not args.watch:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
