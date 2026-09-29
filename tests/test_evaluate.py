"""Returns and benchmarks on synthetic prices with hand-computed answers (Phase 4, M37).

The return equivalent of tests/fixtures/mini/: if a change moves an answer here, the change is
wrong or the case needs a written reason.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ripple import evaluate
from ripple.store import CoverageRow, PriceRow, Store

JAN = date(2025, 1, 6)  # a Monday


def weekdays(start: date, n: int, skip: set[date] = frozenset()) -> list[date]:
    days, day = [], start
    while len(days) < n:
        if day.weekday() < 5 and day not in skip:
            days.append(day)
        day += timedelta(days=1)
    return days


def rows(
    listing: str, days: list[date], prices: list[float] | float, currency: str = "USD"
) -> list[PriceRow]:
    values = prices if isinstance(prices, list) else [prices] * len(days)
    return [PriceRow(listing, d, v, v, currency, "test") for d, v in zip(days, values, strict=True)]


def ramp(n: int, start: float, end: float) -> list[float]:
    """Flat at `start` until the last day, then `end`: a buy-and-hold return of end/start - 1."""
    return [start] * (n - 1) + [end]


US_DAYS = weekdays(JAN, 40)
DETECTED = US_DAYS[4]  # detected on a Friday; entry is the next trading day
H = 10


def win(days: list[date] = US_DAYS, detected: date = DETECTED, h: int = H) -> evaluate.Window:
    w = evaluate.window(days, detected, h)
    assert w is not None
    return w


def run(series: dict[str, list[PriceRow]], fx: dict | None = None, w=None):
    universe = [k for k in series if not k.startswith("^")]
    return evaluate.outcomes(series, universe, w or win(), fx or {})


def test_the_window_starts_the_trading_day_after_detection() -> None:
    w = win()
    assert w.start == US_DAYS[5] and w.start > DETECTED
    assert w.end == US_DAYS[5 + H]


def test_a_window_past_the_end_of_the_calendar_is_none() -> None:
    assert evaluate.window(US_DAYS, US_DAYS[-5], H) is None


def test_a_flat_universe_has_zero_abnormal_return() -> None:
    results = run({name: rows(name, US_DAYS, 100.0) for name in ("A", "B", "C")})
    assert {o.abnormal for o in results.values()} == {0.0}


def test_one_stock_up_ten_percent_against_a_flat_universe() -> None:
    """Four listings, one up 10%: the benchmark is 2.5% (the stock included), so its abnormal
    return is +7.5% and each flat listing's is -2.5%."""
    w = win()
    end = US_DAYS.index(w.end) + 1
    series = {name: rows(name, US_DAYS, 100.0) for name in ("B", "C", "D")}
    series["A"] = rows("A", US_DAYS[:end], ramp(end, 100.0, 110.0)) + rows(
        "A", US_DAYS[end:], 110.0
    )
    results = run(series)
    assert results["A"].usd_return == pytest.approx(0.10)
    assert results["A"].abnormal == pytest.approx(0.075)
    assert results["B"].abnormal == pytest.approx(-0.025)
    assert evaluate.benchmark(results) == pytest.approx(0.025)


def test_a_stock_listed_mid_window_is_excluded_not_zeroed() -> None:
    late = US_DAYS[12:]  # first close a week after the detection day
    series = {"A": rows("A", US_DAYS, 100.0), "B": rows("B", US_DAYS, 100.0)}
    series["NEW"] = rows("NEW", late, 50.0)
    results = run(series)
    assert "NEW" not in results
    assert set(results) == {"A", "B"}


def test_entry_is_strictly_after_the_detection_day() -> None:
    """A close on the detection day itself is never the entry: the day's coverage is only
    complete once it ends."""
    w = win()
    prices = [100.0] * len(US_DAYS)
    prices[US_DAYS.index(DETECTED)] = 80.0  # a detection-day close that must be ignored
    [outcome] = run({"A": rows("A", US_DAYS, prices)}, w=w).values()
    assert outcome.entry_day == w.start
    assert outcome.usd_return == pytest.approx(0.0)


def test_a_us_holiday_inside_the_window_keeps_h_trading_days() -> None:
    holiday = US_DAYS[9]
    days = weekdays(JAN, 40, skip={holiday})
    w = evaluate.window(days, DETECTED, H)
    assert w is not None
    start, end = days.index(w.start), days.index(w.end)
    assert end - start == H and holiday not in days
    assert (w.end - w.start).days > (win().end - win().start).days


def test_a_tokyo_holiday_on_the_entry_day_moves_entry_to_the_next_tokyo_close() -> None:
    jp_days = [d for d in US_DAYS if d != US_DAYS[5]]  # Tokyo is shut on the first US day
    series = {
        "8035.T": rows("8035.T", jp_days, 1000.0, "JPY"),
        "A": rows("A", US_DAYS, 100.0),
    }
    fx = {"JPY=X": rows("JPY=X", US_DAYS, 150.0, "JPY")}
    results = run(series, fx)
    assert results["8035.T"].entry_day == US_DAYS[6]


def test_a_ten_percent_yen_weakening_turns_a_flat_jpy_stock_into_minus_9_1_percent() -> None:
    """USD/JPY from 100 to 110 is a 10% weaker yen: 1,000 yen is $10.00, then $9.09."""
    w = win()
    end = US_DAYS.index(w.end) + 1
    fx_prices = ramp(end, 100.0, 110.0) + [110.0] * (len(US_DAYS) - end)
    series = {"8035.T": rows("8035.T", US_DAYS, 1000.0, "JPY")}
    results = run(series, {"JPY=X": rows("JPY=X", US_DAYS, fx_prices, "JPY")})
    assert results["8035.T"].usd_return == pytest.approx(100 / 110 - 1)
    assert results["8035.T"].local_return == pytest.approx(0.0)


def test_a_stronger_euro_lifts_a_flat_eur_stock_in_usd() -> None:
    """EUR/USD is dollars per euro, so it multiplies: 1.00 to 1.10 is +10% in USD."""
    w = win()
    end = US_DAYS.index(w.end) + 1
    fx_prices = ramp(end, 1.0, 1.1) + [1.1] * (len(US_DAYS) - end)
    series = {"SU.PA": rows("SU.PA", US_DAYS, 200.0, "EUR")}
    results = run(series, {"EURUSD=X": rows("EURUSD=X", US_DAYS, fx_prices, "USD")})
    assert results["SU.PA"].usd_return == pytest.approx(0.10)


def test_missing_fx_excludes_the_listing() -> None:
    series = {"8035.T": rows("8035.T", US_DAYS, 1000.0, "JPY"), "A": rows("A", US_DAYS, 100.0)}
    assert set(run(series, {})) == {"A"}


def test_the_market_index_comparison_is_local() -> None:
    """The stock gains 10% and the S&P 500 5% over the same days: +5% against the index."""
    w = win()
    end = US_DAYS.index(w.end) + 1
    series = {
        "A": rows("A", US_DAYS[:end], ramp(end, 100.0, 110.0)),
        "^GSPC": rows("^GSPC", US_DAYS[:end], ramp(end, 4000.0, 4200.0)),
    }
    results = run(series)
    assert results["A"].index_abnormal == pytest.approx(0.05)


def test_a_loser_that_falls_scores_positive_after_sign_adjustment() -> None:
    assert evaluate.sign_adjust(-0.08, exposure=-0.2) == pytest.approx(0.08)
    assert evaluate.sign_adjust(-0.08, exposure=0.3) == pytest.approx(-0.08)
    with pytest.raises(ValueError):
        evaluate.sign_adjust(0.1, exposure=0.0)


# --- attention as of the burst, graph as of today (D137) ----------------------------------

MINI = Path(__file__).parent / "fixtures" / "mini"


def coverage(key: str, start: date, days: int, matched: int) -> list[CoverageRow]:
    return [
        CoverageRow("company", key, start + timedelta(days=i), matched, 100_000, None, "q")
        for i in range(days)
    ]


def test_attention_can_be_taken_on_another_day_than_the_graph(tmp_path: Path) -> None:
    """Nvidia is heavily covered in the first quarter and quiet in the summer, ASML the other
    way round. Exposures come from the same graph either way; only attention moves."""
    from ripple.score import score

    store = Store(tmp_path / "mini.duckdb")
    store.load(MINI, now=datetime(2026, 9, 1, tzinfo=UTC))
    store.add_coverage(
        coverage("company/nvidia", date(2026, 1, 1), 90, 100)
        + coverage("company/asml", date(2026, 1, 1), 90, 10)
        + coverage("company/nvidia", date(2026, 6, 1), 90, 10)
        + coverage("company/asml", date(2026, 6, 1), 90, 100),
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    as_of = date(2026, 9, 26)

    def scored(attention_day: date) -> dict:
        result = score(
            store, "theme/ai-compute", as_of=as_of, limit=None, attention_as_of=attention_day
        )
        return {r.company: r for r in result.results}

    spring, summer = scored(date(2026, 3, 31)), scored(date(2026, 8, 29))
    assert {c: r.exposure for c, r in spring.items()} == {c: r.exposure for c, r in summer.items()}
    assert spring["company/nvidia"].attention > spring["company/asml"].attention
    assert summer["company/nvidia"].attention < summer["company/asml"].attention
    # Without attention_as_of, attention is taken on the graph's day, as before.
    default = score(store, "theme/ai-compute", as_of=as_of, limit=None)
    by = {r.company: r for r in default.results}
    assert by["company/nvidia"].attention == summer["company/nvidia"].attention


def test_the_window_command_prints_the_hand_checkable_numbers(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ripple.cli import app

    db = tmp_path / "cli.duckdb"
    with Store(db) as store:
        store.load(MINI, now=datetime(2026, 9, 1, tzinfo=UTC))
        w = win()
        end = US_DAYS.index(w.end) + 1
        store.add_prices(
            rows("NVDA", US_DAYS[:end], ramp(end, 100.0, 120.0))
            + [r for name in ("ASML", "TSM", "VRT") for r in rows(name, US_DAYS[:end], 50.0)]
            + rows("^GSPC", US_DAYS[:end], ramp(end, 4000.0, 4400.0))
        )
    args = [
        "evaluate",
        "window",
        "NVDA",
        "--detected",
        DETECTED.isoformat(),
        "--horizon",
        str(H),
        "--theme",
        "theme/ai-compute",
        "--db",
        str(db),
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    # NVDA +20%, three flat names: benchmark +5%, abnormal +15%; against the index, +10%.
    assert "return in USD +20.00%" in result.output
    assert "universe benchmark +5.00% over 4 listings" in result.output
    assert "abnormal vs universe +15.00%" in result.output
    assert "abnormal vs ^GSPC (local) +10.00%" in result.output
    assert "sign-adjusted abnormal +15.00%" in result.output


# --- priced-in check (M41) ------------------------------------------------------------------


def test_priced_in_is_plus_one_when_returns_rise_with_coverage() -> None:
    days = weekdays(JAN, 45)
    share = {d: (i + 1) / 1000 for i, d in enumerate(days)}
    up = {d: (i + 1) / 100 for i, d in enumerate(days)}
    down = {d: -(i + 1) / 100 for i, d in enumerate(days)}
    assert evaluate.priced_in(up, share) == pytest.approx(1.0)
    assert evaluate.priced_in(down, share) == pytest.approx(-1.0)


def test_priced_in_is_unknown_on_too_few_days_or_a_constant_side() -> None:
    days = weekdays(JAN, 45)
    share = {d: (i + 1) / 1000 for i, d in enumerate(days)}
    few = {d: float(i) for i, d in enumerate(days[: evaluate.PRICED_IN_MIN_DAYS - 1])}
    assert evaluate.priced_in(few, share) is None
    assert evaluate.priced_in(dict.fromkeys(days, 0.01), share) is None


def test_a_move_the_whole_universe_shares_is_not_priced_into_one_name() -> None:
    """Both stocks rise and fall together with the theme's coverage: against the universe the
    abnormal return is zero every day, so neither is priced in (unknown, not +1)."""
    days = weekdays(JAN, 45)
    prices = [100.0 * (1 + 0.01 * (i % 5)) for i in range(len(days))]
    series = {"A": rows("A", days, prices), "B": rows("B", days, prices)}
    abnormal = evaluate.daily_abnormal(series, ["A", "B"], days[1], days[-1], {})
    assert set(abnormal["A"].values()) == {0.0}
    share = {d: (i % 5) / 1000 for i, d in enumerate(days)}
    assert evaluate.priced_in(abnormal["A"], share) is None


def test_the_daily_benchmark_is_the_mean_of_listings_that_traded() -> None:
    """A rises 10% on day 2 and B is flat: the benchmark is +5%, so A is +5% and B -5%."""
    days = weekdays(JAN, 3)
    series = {"A": rows("A", days, [100.0, 110.0, 110.0]), "B": rows("B", days, 50.0)}
    abnormal = evaluate.daily_abnormal(series, ["A", "B"], days[1], days[-1], {})
    assert abnormal["A"][days[1]] == pytest.approx(0.05)
    assert abnormal["B"][days[1]] == pytest.approx(-0.05)


def test_a_tokyo_holiday_is_a_missing_day_not_a_stale_zero() -> None:
    days = weekdays(JAN, 5)
    tokyo = [d for d in days if d != days[2]]
    fx = {"JPY=X": rows("JPY=X", days, 150.0, "JPY")}
    returns = evaluate.daily_usd_returns(
        rows("8035.T", tokyo, [1000.0, 1010.0, 1030.0, 1030.0], "JPY"), days[1], days[-1], fx
    )
    assert days[2] not in returns
    # The day after the holiday carries the return from the last Tokyo close before it.
    assert returns[days[3]] == pytest.approx(1030 / 1010 - 1)


def test_the_priced_in_window_ends_on_or_before_the_detection_day() -> None:
    days = weekdays(JAN, 80)
    first, last = evaluate.priced_in_window(days, days[70])
    assert last == days[70] and first == days[70 - evaluate.PRICED_IN_DAYS + 1]
    assert evaluate.priced_in_window(days, days[10]) is None


def priced_in_store(tmp_path: Path) -> tuple[Path, list[date]]:
    """Mini graph, 80 US days of prices, and an AI compute coverage series. NVDA's daily return
    is highest on the days coverage is highest; the three others are flat."""
    db = tmp_path / "priced.duckdb"
    # After the graph's load date (2026-09-01), so a snapshot as of the last day holds the graph.
    days = weekdays(date(2026, 6, 1), 80)
    wave = [(i * 7) % 11 for i in range(len(days))]  # a scrambled but fixed daily pattern
    nvda, price = [], 100.0
    for w in wave:
        price *= 1 + w / 1000
        nvda.append(price)
    with Store(db) as store:
        store.load(MINI, now=datetime(2026, 9, 1, tzinfo=UTC))
        store.add_prices(
            rows("NVDA", days, nvda)
            + [r for name in ("ASML", "TSM", "VRT") for r in rows(name, days, 50.0)]
            + rows("^GSPC", days, 4000.0)
        )
        store.add_coverage(
            [
                CoverageRow("theme", "theme/ai-compute", d, 10 + w, 100_000, None, "q")
                for d, w in zip(days, wave, strict=True)
            ]
        )
    return db, days


def test_priced_in_for_ranks_the_stock_that_moves_with_the_news(tmp_path: Path) -> None:
    db, days = priced_in_store(tmp_path)
    with Store(db, read_only=True) as store:
        values = evaluate.priced_in_for(
            store,
            "theme/ai-compute",
            {"company/nvidia": "NVDA", "company/asml": "ASML"},
            ["ASML", "NVDA", "TSM", "VRT"],
            as_of=days[-1],
        )
    # NVDA's abnormal return is 3/4 of its own return, a monotone function of coverage; ASML's
    # is -1/4 of NVDA's, so it moves against the news.
    assert values["company/nvidia"] == pytest.approx(1.0)
    assert values["company/asml"] == pytest.approx(-1.0)


def test_priced_in_is_unknown_without_prices(tmp_path: Path) -> None:
    store = Store(tmp_path / "empty.duckdb")
    store.load(MINI, now=datetime(2026, 9, 1, tzinfo=UTC))
    values = evaluate.priced_in_for(
        store, "theme/ai-compute", {"company/nvidia": "NVDA"}, ["NVDA"], as_of=date(2026, 9, 26)
    )
    assert values == {"company/nvidia": None}


def test_exposed_shows_a_priced_in_column_on_request(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ripple.cli import app

    db, days = priced_in_store(tmp_path)
    base = ["exposed", "theme/ai-compute", "--as-of", days[-1].isoformat(), "--db", str(db)]
    runner = CliRunner()
    plain = runner.invoke(app, base)
    assert plain.exit_code == 0 and "Priced-in" not in plain.output
    table = runner.invoke(app, [*base, "--priced-in"], env={"COLUMNS": "220"})
    assert table.exit_code == 0, table.output
    assert "Priced-in" in table.output and "+1.00" in table.output
    import json

    data = json.loads(runner.invoke(app, [*base, "--priced-in", "--json"]).output)
    by = {r["company"]: r["priced_in"] for r in data["results"]}
    assert by["company/nvidia"] == pytest.approx(1.0)
    assert any("priced_in" in note for note in data["notes"])
