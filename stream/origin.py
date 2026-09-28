"""Which pages may open a worker's live stream.

Workers listen on loopback, but any website open in a browser on this Mac can
still dial ws://127.0.0.1:<port>. Unlike an MJPEG <img>, whose pixels a foreign
page cannot read, a WebSocket hands that page the video. So only our own pages
get it: loopback origins (the local dashboard and worker ports), or the page
host the request arrived on (Tailscale Serve's https://<mac>.ts.net). A
browser cannot forge Origin, or Host / X-Forwarded-Host, on a WebSocket.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(value: str) -> str | None:
    return urlsplit(value if "//" in value else f"//{value}").hostname


def origin_allowed(origin: str | None, host: str | None) -> bool:
    if origin is None:
        return True  # not a browser page (curl, tools/stream_probe.py)
    name = _hostname(origin)
    if name is None:
        return False
    if name in _LOOPBACK:
        return True
    return host is not None and _hostname(host) == name
