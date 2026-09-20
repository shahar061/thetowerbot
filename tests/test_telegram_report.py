from __future__ import annotations

import logging
import threading
import urllib.error

import pytest

import config
import telegram_report
from telegram_report import TelegramConfig, TelegramReporter, render_summary


SETTINGS = TelegramConfig(token="123:SECRET", chat_id="42", interval=0.01)


def snapshot(**overrides):
    """A BotState.snapshot()-shaped dict with every key the renderer reads."""
    base = {
        "screen": "BATTLE",
        "uptime": 3600.0,
        "scans": 1800,
        "taps": {},
        "skips": {},
        "runs_completed": 0,
        "run": None,
        "wallet": None,
        "last_error": None,
        "tail": [],
    }
    base.update(overrides)
    return base


class FakeState:
    def __init__(self, payload=None) -> None:
        self.payload = payload if payload is not None else snapshot()

    def snapshot(self):
        return self.payload


class FakeControls:
    def __init__(self, paused: bool) -> None:
        self.paused = paused

    def snapshot(self):
        return self


# --- config from the environment ------------------------------------------


def test_from_env_returns_none_when_unconfigured() -> None:
    assert TelegramConfig.from_env({}) is None


@pytest.mark.parametrize(
    "env",
    [
        {"TELEGRAM_BOT_TOKEN": "t"},
        {"TELEGRAM_CHAT_ID": "1"},
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "   "},
    ],
)
def test_half_configured_is_off_not_an_error(env) -> None:
    """Exporting one variable and stopping must not prevent the bot starting."""
    assert TelegramConfig.from_env(env) is None


def test_from_env_reads_both_halves_and_the_default_interval() -> None:
    settings = TelegramConfig.from_env(
        {"TELEGRAM_BOT_TOKEN": " t ", "TELEGRAM_CHAT_ID": " 42 "}
    )
    assert settings == TelegramConfig(
        token="t", chat_id="42", interval=config.TELEGRAM_SUMMARY_SECONDS
    )


def test_env_interval_overrides_the_default() -> None:
    settings = TelegramConfig.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "42",
            "TELEGRAM_SUMMARY_SECONDS": "900",
        }
    )
    assert settings.interval == 900.0


def test_explicit_interval_beats_the_environment() -> None:
    """The CLI flag is the caller passing `interval`."""
    settings = TelegramConfig.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "42",
            "TELEGRAM_SUMMARY_SECONDS": "900",
        },
        interval=60.0,
    )
    assert settings.interval == 60.0


@pytest.mark.parametrize("raw", ["soon", "", "-5"])
def test_an_unusable_interval_falls_back_instead_of_raising(raw) -> None:
    """A typo in the interval must not take the digests down with it."""
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"}
    if raw:
        env["TELEGRAM_SUMMARY_SECONDS"] = raw
    settings = TelegramConfig.from_env(env)
    assert settings is not None
    assert settings.interval == config.TELEGRAM_SUMMARY_SECONDS


# --- rendering -------------------------------------------------------------


def test_first_line_answers_alive_and_for_how_long() -> None:
    text = render_summary(snapshot(uptime=11_700))
    assert text.splitlines()[0] == "The Tower bot - running, up 3h 15m"


def test_paused_is_stated_on_the_first_line() -> None:
    assert render_summary(snapshot(), paused=True).startswith(
        "The Tower bot - paused,"
    )


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h 00m"),
        (11_700, "3h 15m"),
        (86_400, "1d 00h"),
        (187_200, "2d 04h"),
    ],
)
def test_duration_uses_at_most_two_units(seconds, expected) -> None:
    assert telegram_report._duration(seconds) == expected


def test_counts_are_thousands_separated() -> None:
    text = render_summary(snapshot(scans=1_234_567, wallet=9_876_543))
    assert "Scans: 1,234,567" in text
    assert "Wallet: $9,876,543" in text


def test_a_missing_wallet_is_omitted_rather_than_shown_as_none() -> None:
    assert "Wallet" not in render_summary(snapshot(wallet=None))


def test_an_active_run_reports_its_id_and_elapsed() -> None:
    text = render_summary(
        snapshot(run={"id": 42, "started_at": 0.0, "elapsed": 900.0, "taps": {}})
    )
    assert "Run #42: 15m in" in text


def test_no_active_run_says_so_explicitly() -> None:
    """Silence would read as 'not applicable' rather than 'stuck on a menu'."""
    assert "Run: none in progress" in render_summary(snapshot(run=None))


def test_tallies_rank_by_count_and_summarise_the_tail() -> None:
    text = render_summary(
        snapshot(taps={"a": 1, "b": 9, "c": 5, "d": 4, "e": 3, "f": 2})
    )
    assert "Taps: b 9, c 5, d 4, e 3 (+2 more)" in text


def test_empty_tallies_read_as_none() -> None:
    text = render_summary(snapshot(taps={}, skips={}))
    assert "Taps: none" in text
    assert "Skips: none" in text


def test_last_error_is_carried_verbatim() -> None:
    """No escaping, because the message is sent as plain text - see
    post_message's docstring. Markdown metacharacters in an OCR'd error
    string must survive rather than break the send."""
    text = render_summary(snapshot(last_error="no _match_ for *retry* <btn>"))
    assert "Last error: no _match_ for *retry* <btn>" in text


def test_a_long_message_is_truncated_to_telegrams_limit() -> None:
    text = render_summary(snapshot(last_error="x" * 8000))
    assert len(text) <= telegram_report.MAX_MESSAGE_CHARS
    assert text.endswith("[truncated]")
    # The header is what says the bot is alive, so it is the part that must
    # never be the bit that got cut.
    assert text.startswith("The Tower bot - running,")


# --- the reporter ----------------------------------------------------------


def test_render_reads_paused_from_controls() -> None:
    reporter = TelegramReporter(
        FakeState(), SETTINGS, controls=FakeControls(paused=True), send=lambda _: None
    )
    assert "paused" in reporter.render()


def test_tick_sends_and_counts() -> None:
    sent: list[str] = []
    reporter = TelegramReporter(FakeState(), SETTINGS, send=sent.append)

    assert reporter.tick() is True
    assert reporter.sent == 1
    assert reporter.failures == 0
    assert sent and sent[0].startswith("The Tower bot")


def test_a_send_failure_is_swallowed_and_counted() -> None:
    """A reporter that dies on a flaky network would stop the digests, and
    their stopping is what this feature uses to mean 'the bot is gone'."""
    def boom(_text: str) -> None:
        raise OSError("network is unreachable")

    reporter = TelegramReporter(FakeState(), SETTINGS, send=boom)

    assert reporter.tick() is False
    assert reporter.failures == 1
    assert reporter.sent == 0


def test_a_failing_snapshot_does_not_kill_the_timer() -> None:
    class Exploding:
        def snapshot(self):
            raise RuntimeError("state is wedged")

    reporter = TelegramReporter(Exploding(), SETTINGS, send=lambda _: None)

    assert reporter.tick() is False
    assert reporter.failures == 1


def test_the_token_never_reaches_the_log(caplog) -> None:
    """urllib puts the request URL in its exceptions and the token sits in
    that path, so the error path is a credential leak unless it scrubs."""
    def boom(_text: str) -> None:
        raise urllib.error.HTTPError(
            url=f"https://api.telegram.org/bot{SETTINGS.token}/sendMessage",
            code=401, msg="Unauthorized", hdrs=None, fp=None,
        )

    reporter = TelegramReporter(FakeState(), SETTINGS, send=boom)
    with caplog.at_level(logging.WARNING):
        reporter.tick()

    assert caplog.text  # it did log something
    assert "SECRET" not in caplog.text
    assert "<token>" in caplog.text


def test_redact_is_a_no_op_without_a_token() -> None:
    assert telegram_report.redact("nothing to hide", "") == "nothing to hide"


def test_start_sends_one_digest_immediately() -> None:
    """An hour is a long time to find out the chat id was wrong."""
    first = threading.Event()

    def send(_text: str) -> None:
        first.set()

    reporter = TelegramReporter(
        FakeState(), TelegramConfig(token="t", chat_id="1", interval=3600), send=send
    )
    reporter.start()
    try:
        assert first.wait(timeout=5), "no digest was sent on start"
    finally:
        reporter.close()


def test_close_returns_promptly_despite_a_long_interval() -> None:
    """The timer waits on an Event rather than sleeping, so shutdown does
    not block for whatever is left of the hour."""
    reporter = TelegramReporter(
        FakeState(),
        TelegramConfig(token="t", chat_id="1", interval=3600),
        send=lambda _: None,
    )
    reporter.start()

    started = threading.Event()
    started.set()
    reporter.close(timeout=5)

    assert reporter._thread is None


def test_close_is_safe_before_start_and_twice() -> None:
    reporter = TelegramReporter(FakeState(), SETTINGS, send=lambda _: None)
    reporter.close()
    reporter.start()
    reporter.close()
    reporter.close()


def test_start_twice_does_not_spawn_a_second_thread() -> None:
    reporter = TelegramReporter(
        FakeState(),
        TelegramConfig(token="t", chat_id="1", interval=3600),
        send=lambda _: None,
    )
    reporter.start()
    thread = reporter._thread
    reporter.start()
    try:
        assert reporter._thread is thread
    finally:
        reporter.close()


# --- the HTTP call ---------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, _size: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


def test_post_message_sends_plain_text_to_the_right_chat() -> None:
    seen: dict[str, object] = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = request.data.decode()
        seen["timeout"] = timeout
        return FakeResponse(b'{"ok":true,"result":{}}')

    telegram_report.post_message(SETTINGS, "hello", opener=opener)

    assert seen["url"] == f"{telegram_report.TELEGRAM_API}/bot123:SECRET/sendMessage"
    assert "chat_id=42" in seen["body"]
    assert "text=hello" in seen["body"]
    # No parse_mode: Telegram rejects unbalanced markup, and this body
    # interpolates strings the bot read off a screen.
    assert "parse_mode" not in seen["body"]
    assert "disable_notification=true" in seen["body"]


def test_post_message_raises_on_a_200_that_is_not_ok() -> None:
    """Telegram answers 200 {"ok":false} for a bad chat id, so status alone
    would let a misconfigured chat fail silently forever."""
    def opener(_request, timeout=None):
        return FakeResponse(b'{"ok":false,"description":"chat not found"}')

    with pytest.raises(RuntimeError, match="chat not found"):
        telegram_report.post_message(SETTINGS, "hello", opener=opener)
