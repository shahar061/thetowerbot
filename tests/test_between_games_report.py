from tools import between_games_report as r


def test_gaps_pairs_run_end_with_next_start() -> None:
    rows = [(0., "RunStarted"), (100., "RunEnded"), (130., "Tapped"), (160., "RunStarted"),
            (900., "RunEnded"), (950., "RunEnded"), (980., "RunStarted")]
    assert r.gaps(rows) == [60.0, 30.0]


def test_summary_median_and_p90() -> None:
    s = r.summary([10., 20., 30., 40., 50., 60., 70., 80., 90., 100.])
    assert s["n"] == 10 and s["median"] == 55.0 and 90.0 <= s["p90"] <= 100.0
