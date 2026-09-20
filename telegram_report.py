"""One-way status digests to Telegram, on a timer.

Why this exists: the dashboard binds 127.0.0.1 and nothing else in the
process ever talks outward, so "is the bot still running?" is unanswerable
from anywhere but the host machine. Answering it from a phone previously
meant a VPN and an SSH client. This closes the gap with the smallest thing
that works from behind NAT - an outbound POST every interval. No inbound
port, no webhook, no public URL, nothing for a router to forward.

Deliberately NOT a sink. Everything under sinks/ is a bus consumer because
it reacts to individual events; this reports a periodic *aggregate*, and
BotState already accumulates exactly that aggregate for the TUI and for
/api/status. Feeding a second accumulator off the bus would be a third copy
of the same tallies, free to drift from the two that currently agree - which
is the argument sinks/state.py makes for its own existence, applied once
more. So this reads a snapshot on a timer and owns no counters of its own.

View-only on purpose, and structurally so rather than by discipline: there
is no getUpdates loop and no command parser anywhere in this module, so
nothing typed into the Telegram chat has a path into the bot. The token
grants "send messages to this chat" and nothing else, which is also why a
leaked token cannot drive the emulator.

Module name is `telegram_report`, not `telegram`, because a top-level
`telegram.py` shadows the python-telegram-bot package for the whole process
the moment anything pulls it in. Nothing here needs that package - a digest
is one HTTPS POST and urllib does it - but the shadowing would be silent and
would surface a long way from this file.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import config

logger = logging.getLogger("tower_bot.telegram")

TELEGRAM_API = "https://api.telegram.org"

# Telegram rejects a sendMessage body over 4096 characters outright, so a
# long tail of errors would cost the whole digest rather than its last line.
# Truncating here means the interesting part - the header, which is what
# says whether the bot is alive - always survives.
MAX_MESSAGE_CHARS = 4096
_TRUNCATION_NOTE = "\n[truncated]"

ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_INTERVAL = "TELEGRAM_SUMMARY_SECONDS"


@dataclass(frozen=True)
class TelegramConfig:
    """Where to send, and how often.

    Not in config.py with the other tunables because two of the three are
    secrets: config.py is committed, and a bot token in it would be a token
    in the git history. Only the interval has a committed default.
    """

    token: str
    chat_id: str
    interval: float = config.TELEGRAM_SUMMARY_SECONDS

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        interval: float | None = None,
    ) -> TelegramConfig | None:
        """Build from the environment, or None when it is not configured.

        Absence is the off switch. There is no --telegram flag to forget
        alongside the variables: if both halves are set the digests go out,
        and if either is missing they do not. A half-configured environment
        (token, no chat id) is the same as unconfigured rather than an
        error, because the bot must still start on a machine where someone
        exported one variable and stopped.

        `interval` overrides the environment, which overrides the committed
        default - the CLI flag is the caller passing this argument.
        """
        env = os.environ if env is None else env
        token = (env.get(ENV_TOKEN) or "").strip()
        chat_id = (env.get(ENV_CHAT_ID) or "").strip()
        if not token or not chat_id:
            return None

        resolved = config.TELEGRAM_SUMMARY_SECONDS
        raw = (env.get(ENV_INTERVAL) or "").strip()
        if raw:
            try:
                resolved = float(raw)
            except ValueError:
                # A typo'd interval must not take the digests down with it:
                # the whole point of this module is reporting when nobody is
                # at the machine to read a traceback.
                logger.warning(
                    "%s=%r is not a number - using the default of %.0fs",
                    ENV_INTERVAL, raw, resolved,
                )
        if interval is not None:
            resolved = interval
        if resolved <= 0:
            logger.warning(
                "telegram summary interval must be positive, got %r - using %.0fs",
                resolved, config.TELEGRAM_SUMMARY_SECONDS,
            )
            resolved = config.TELEGRAM_SUMMARY_SECONDS
        return cls(token=token, chat_id=chat_id, interval=resolved)


def redact(text: str, token: str) -> str:
    """Remove a bot token from text on its way to a log.

    urllib puts the request URL in its exceptions, and the token sits in the
    path of every Telegram call (/bot<token>/sendMessage). Logging the raw
    exception would therefore write the token into the bot's own log file on
    the first network hiccup - a secret leaked by the error path of a feature
    whose entire job is to run unattended.
    """
    if not token:
        return text
    return text.replace(token, "<token>")


def post_message(
    settings: TelegramConfig,
    text: str,
    *,
    timeout: float = config.TELEGRAM_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    """Send one message. Raises on any failure; the caller decides.

    No parse_mode. Telegram's Markdown and HTML modes reject a message whose
    body contains unbalanced markup, and this digest interpolates values the
    bot read off a screen - an error string with a stray underscore or a `<`
    would turn into a 400 and lose the whole report. Plain text cannot fail
    that way, and the digest has no formatting worth the risk.
    """
    body = urllib.parse.urlencode(
        {
            "chat_id": settings.chat_id,
            "text": text,
            # A status digest is ambient, not urgent. It should be there when
            # you look, not buzz a pocket every hour.
            "disable_notification": "true",
        }
    ).encode()
    request = urllib.request.Request(
        f"{TELEGRAM_API}/bot{settings.token}/sendMessage",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener(request, timeout=timeout) as response:
        # Telegram answers 200 with {"ok": false, ...} for some rejections
        # (a bad chat id among them), so status alone does not mean sent.
        # The body is small; reading it is what makes a misconfigured chat
        # id visible in the log instead of silently doing nothing forever.
        payload = response.read(2048).decode("utf-8", "replace")
    if '"ok":true' not in payload.replace(" ", ""):
        raise RuntimeError(f"telegram rejected the message: {payload}")


def _duration(seconds: float) -> str:
    """Compact human duration: 45s, 12m, 3h 07m, 2d 04h.

    Two units at most. The digest is read on a phone, and "2d 4h 13m 6s" is
    longer without being more useful than "2d 04h" for a number whose job is
    to answer "has it been up the whole time?"
    """
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins:02d}m"
    days, hrs = divmod(hours, 24)
    return f"{days}d {hrs:02d}h"


def _tally(counts: Mapping[str, int], limit: int = 4) -> str:
    """The biggest few entries of a Counter-shaped dict, as one line."""
    if not counts:
        return "none"
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ", ".join(f"{name} {count:,}" for name, count in ranked[:limit])
    remaining = len(ranked) - limit
    return f"{shown} (+{remaining} more)" if remaining > 0 else shown


def render_summary(
    snapshot: Mapping[str, Any],
    *,
    paused: bool | None = None,
    label: str = "The Tower bot",
) -> str:
    """Format one digest from a BotState.snapshot().

    A pure function of the snapshot so it tests without a thread, a socket
    or a clock. Every value here is one the dashboard already shows; the
    ordering is the difference - the first line has to answer "alive?" on a
    lock screen, before anything is expanded.
    """
    scans = int(snapshot.get("scans") or 0)
    state = "paused" if paused else "running"
    lines = [
        f"{label} - {state}, up {_duration(snapshot.get('uptime') or 0)}",
        f"Screen: {snapshot.get('screen') or 'UNKNOWN'}",
        f"Scans: {scans:,}",
    ]

    wallet = snapshot.get("wallet")
    if wallet is not None:
        lines.append(f"Wallet: ${int(wallet):,}")

    run = snapshot.get("run")
    if isinstance(run, Mapping) and run.get("id") is not None:
        elapsed = _duration(run.get("elapsed") or 0)
        lines.append(f"Run #{run['id']}: {elapsed} in")
    else:
        # Saying so explicitly matters: "no run" during a battle-only
        # strategy is the symptom of a bot stuck on a menu, and its absence
        # from the digest would read as though the line just didn't apply.
        lines.append("Run: none in progress")
    lines.append(f"Runs completed: {int(snapshot.get('runs_completed') or 0):,}")

    lines.append(f"Taps: {_tally(snapshot.get('taps') or {})}")
    lines.append(f"Skips: {_tally(snapshot.get('skips') or {})}")

    last_error = snapshot.get("last_error")
    lines.append(f"Last error: {last_error}" if last_error else "Last error: none")

    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - len(_TRUNCATION_NOTE)] + _TRUNCATION_NOTE
    return text


class TelegramReporter:
    """Owns the timer thread that sends the digests.

    Shaped like a sink from the outside - start(), close() - so the wiring in
    tower_bot.py reads the same as its neighbours, even though it never
    subscribes to the bus.
    """

    def __init__(
        self,
        state: Any,
        settings: TelegramConfig,
        *,
        controls: Any | None = None,
        send: Callable[[str], None] | None = None,
        label: str = "The Tower bot",
    ) -> None:
        self._state = state
        self._settings = settings
        self._controls = controls
        self._send = send if send is not None else self._post
        self._label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Counters for the tests and for a human asking whether this thing
        # has ever actually worked; nothing reads them in the hot path.
        self.sent = 0
        self.failures = 0

    def _post(self, text: str) -> None:
        post_message(self._settings, text)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="TelegramReporter", daemon=True
        )
        self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        """Stop the timer. Safe to call twice, and safe to call before start.

        The thread waits on an Event rather than sleeping, so this returns in
        microseconds instead of after however much of the hour is left.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("telegram reporter did not stop within %.1fs", timeout)

    def _run(self) -> None:
        # One digest immediately, before the first wait. An hour is a long
        # time to discover you pasted the chat id wrong, and the startup
        # message doubles as the "the bot just came up" signal.
        self.tick()
        while not self._stop.wait(self._settings.interval):
            self.tick()

    def render(self) -> str:
        paused = None
        if self._controls is not None:
            paused = self._controls.snapshot().paused
        return render_summary(
            self._state.snapshot(), paused=paused, label=self._label
        )

    def tick(self) -> bool:
        """Build and send one digest. Never raises.

        A reporter that dies on a flaky network is worse than no reporter:
        the digests stop, and their stopping is exactly the signal this
        feature uses to mean "the bot is gone". So every failure is counted
        and logged, and the timer keeps its next appointment.
        """
        try:
            text = self.render()
        except Exception:  # noqa: BLE001 - a bad snapshot must not kill the timer
            self.failures += 1
            logger.exception("telegram digest could not be rendered")
            return False

        try:
            self._send(text)
        except urllib.error.HTTPError as exc:
            self.failures += 1
            logger.warning(
                "telegram digest rejected: %s",
                redact(f"{exc.code} {exc.reason} for {exc.url}", self._settings.token),
            )
            return False
        except Exception as exc:  # noqa: BLE001 - network, DNS, timeouts, bad payloads
            self.failures += 1
            logger.warning(
                "telegram digest not sent: %s",
                redact(f"{type(exc).__name__}: {exc}", self._settings.token),
            )
            return False

        self.sent += 1
        return True
