"""The live CPU-per-scan measurement tool (spec invariant 5)."""
from __future__ import annotations

import pytest

from tools import measure_cpu_per_scan as m

PS = """96668 /Users/x/.venv/bin/python3 /Users/x/thetowerbot/tower_bot.py --worker-id Tiramisu64_36 --port 5915 --web-port 10036 --web
96653 /Users/x/.venv/bin/python3 /Users/x/thetowerbot/tower_bot.py --worker-id Tiramisu64_35 --web-port 10035 --web
96190 /Users/x/.venv/bin/python3 tower_bot.py --web --dismiss-bluestacks-upgrade
"""


@pytest.mark.parametrize("text,seconds", [
    ("25:05.92", 1505.92),
    ("1:02:03.50", 3723.5),
    ("2-01:00:00", 176400.0),
    (" 0:07.00\n", 7.0),
])
def test_ps_cputime_parses_to_seconds(text: str, seconds: float) -> None:
    assert m.parse_cputime(text) == pytest.approx(seconds)


def test_a_worker_is_found_by_its_id_with_its_web_port() -> None:
    assert m.find_worker("Tiramisu64_35", PS) == (96653, 10035)


def test_an_absent_worker_is_an_error_not_a_guess() -> None:
    with pytest.raises(LookupError):
        m.find_worker("Tiramisu64_99", PS)


def test_cpu_per_scan_divides_cpu_by_scans() -> None:
    assert m.cpu_per_scan(m.Sample(10.0, 100), m.Sample(40.0, 120)) == pytest.approx(1.5)
    assert m.cpu_per_scan(m.Sample(10.0, 100), m.Sample(40.0, 100)) is None


def test_a_raw_profile_is_summarised_as_shares_of_all_samples() -> None:
    raw = (
        "main (/r/tower_bot.py:10);run_once (/r/tower_bot.py:620);read (/r/ocr.py:152) 30\n"
        "main (/r/tower_bot.py:10);run_once (/r/tower_bot.py:610);classify (/r/screens.py:53) 10\n"
    )
    shares = m.summarize(raw, {"ocr.read": r"\bread \([^)]*ocr\.py:",
                               "screens.classify": r"\bclassify \([^)]*screens\.py:"})
    assert shares == {"ocr.read": pytest.approx(0.75), "screens.classify": pytest.approx(0.25)}
