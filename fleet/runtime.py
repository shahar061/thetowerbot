"""Per-worker writable roots and collision validation."""

from __future__ import annotations

import re
import fcntl
import hashlib
import os
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


# Browsers reject these ports before contacting the HTTP/WebSocket server.
# https://fetch.spec.whatwg.org/#port-blocking
BROWSER_BLOCKED_PORTS = frozenset({
    0, 1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77,
    79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135,
    137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531,
    532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720,
    1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667,
    6668, 6669, 6679, 6697, 10080,
})
# The only blocked port in the 10000+ worker range gets a reserved port below
# that range. Shifting later workers would collide with their existing ports.
WORKER_PORT_REPLACEMENTS = {10080: 9999}


def worker_dashboard_port(number: int) -> int:
    """Stable browser-safe dashboard port, preserving all safe legacy assignments."""
    if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 55_535:
        raise ValueError("worker number is out of range")
    legacy = 10_000 + number
    return WORKER_PORT_REPLACEMENTS.get(legacy, legacy)


class RuntimeIsolationError(ValueError):
    """Another live worker owns the requested root or web port."""


@contextmanager
def reserve_endpoint(endpoint: str) -> Iterator[None]:
    """One process lease for an ADB endpoint, shared by fleet and standalone runs."""
    host, separator, port = endpoint.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise ValueError("ADB endpoint must be host:port")
    if host in {"localhost", "::1"}:
        endpoint = f"127.0.0.1:{port}"
    lock_dir = Path(tempfile.gettempdir()) / f"thetowerbot-fleet-locks-{os.getuid()}"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(endpoint.encode()).hexdigest()
    with (lock_dir / f"adb-{digest}.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeIsolationError("identity incident: ADB endpoint already reserved") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class WorkerRuntime:
    worker_id: str
    root: Path
    db_path: Path
    strategy_root: Path
    evidence_root: Path
    checkpoint_root: Path
    web_port: int

    @classmethod
    def for_worker(cls, fleet_root: Path, worker_id: str, web_port: int) -> WorkerRuntime:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", worker_id):
            raise ValueError("worker id must be a simple path component")
        if not 1 <= web_port <= 65535:
            raise ValueError("web port is out of range")
        if web_port in BROWSER_BLOCKED_PORTS:
            raise ValueError(f"web port {web_port} is blocked by browsers")
        root = (Path(fleet_root).resolve() / worker_id).resolve()
        return cls(
            worker_id, root, root / "tower_bot.db", root / "strategies",
            root / "evidence", root / "checkpoints", web_port,
        )

    def writable_paths(self) -> set[Path]:
        return {
            self.db_path.resolve(), self.strategy_root.resolve(),
            self.evidence_root.resolve(), self.checkpoint_root.resolve(),
        }

    def ensure_directories(self) -> None:
        for directory in (self.root, self.strategy_root, self.evidence_root,
                          self.checkpoint_root):
            directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def reserve(self, endpoint: str | None = None) -> Iterator[None]:
        """Hold process locks for this worker root, dashboard port, and ADB endpoint."""
        self.root.mkdir(parents=True, exist_ok=True)
        port_dir = Path(tempfile.gettempdir()) / f"thetowerbot-fleet-locks-{os.getuid()}"
        port_dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            locks = [
                (self.root / ".worker.lock", "runtime path"),
                (port_dir / f"{self.web_port}.lock", "web port"),
            ]
            for path, label in locks:
                handle = stack.enter_context(path.open("a+"))
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeIsolationError(
                        f"identity incident: {label} already reserved"
                    ) from None
            if endpoint is not None:
                stack.enter_context(reserve_endpoint(endpoint))
            yield


def validate_isolation(runtimes: Iterable[WorkerRuntime]) -> None:
    seen_ports: set[int] = set()
    seen_roots: list[Path] = []
    for runtime in runtimes:
        if runtime.web_port in seen_ports:
            raise ValueError(f"web port {runtime.web_port} is shared")
        seen_ports.add(runtime.web_port)
        root = runtime.root.resolve()
        if any(root == other or root.is_relative_to(other) or other.is_relative_to(root)
               for other in seen_roots):
            raise ValueError(f"runtime path {root} is shared")
        seen_roots.append(root)
