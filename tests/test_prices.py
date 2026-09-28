"""The price client, store and CLI, against recorded Yahoo responses (Phase 4, M36).

Tests never touch the network: responses come from `tests/fixtures/prices/`, trimmed from the
M36 spike.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ripple import prices
from ripple.cli import app
from ripple.store import PriceRow, Store

FIXTURES = Path(__file__).parent / "fixtures" / "prices"
MINI = Path(__file__).parent / "fixtures" / "mini"
runner = CliRunner()


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def client(tmp_path: Path, handler, sleeps: list[float] | None = None) -> prices.PriceClient:
    clock = iter(float(i) for i in range(1000))
    return prices.PriceClient(
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
        monotonic=lambda: next(clock),
    )


def serve(body: str, status: int = 200):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, text=body)

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


START, END = date(2024, 6, 1), date(2024, 6, 30)


def test_a_split_is_already_adjusted_and_dividends_are_added_back(tmp_path: Path) -> None:
    """Nvidia split 10:1 on 2024-06-10 and paid a dividend on 06-11 (recorded, trimmed)."""
    with client(tmp_path, serve(fixture("nvda_split.json"))) as c:
        series = c.fetch("NVDA", START, END)
    assert series.currency == "USD"
    days = [p.day for p in series.points]
    assert days[0] == date(2024, 6, 5) and days[-1] == date(2024, 6, 12)
    by_day = {p.day: p for p in series.points}
    # Split-adjusted: no tenfold drop across the split.
    ratio = by_day[date(2024, 6, 10)].close / by_day[date(2024, 6, 7)].close
    assert ratio == pytest.approx(1.0, abs=0.05)
    # Dividend-adjusted: the series ran to 2026, so every day here is adjusted for the dividends
    # paid after it and sits below its close; the adjustment only shrinks as time moves forward.
    assert all(p.adj_close < p.close for p in series.points)
    factor = {day: p.adj_close / p.close for day, p in by_day.items()}
    # Constant between dividends, and a step on the 2024-06-11 ex-date ($0.01 on about $121).
    assert factor[date(2024, 6, 10)] == pytest.approx(factor[date(2024, 6, 5)], abs=1e-6)
    assert factor[date(2024, 6, 11)] - factor[date(2024, 6, 10)] > 5e-5


def test_a_tokyo_listing_keeps_its_own_trading_days(tmp_path: Path) -> None:
    """Golden Week: Showa Day (29 April) and 5 to 6 May have no row, and nothing is zero-filled."""
    with client(tmp_path, serve(fixture("tokyo_electron.json"))) as c:
        series = c.fetch("8035.T", date(2025, 4, 28), date(2025, 5, 9))
    assert series.currency == "JPY"
    days = {p.day for p in series.points}
    assert date(2025, 4, 28) in days
    assert not days & {date(2025, 4, 29), date(2025, 5, 5), date(2025, 5, 6)}
    assert all(p.close > 0 for p in series.points)


def test_a_timestamp_is_dated_in_the_exchange_time_zone() -> None:
    """23:30 UTC on 1 January is 09:30 on 2 January for an exchange ten hours ahead."""
    stamp = int(datetime(2025, 1, 1, 23, 30, tzinfo=UTC).timestamp())
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "AUD", "gmtoffset": 36000},
                    "timestamp": [stamp],
                    "indicators": {"quote": [{"close": [10.0]}], "adjclose": [{"adjclose": [9.5]}]},
                }
            ],
            "error": None,
        }
    }
    [point] = prices.parse("X.AX", payload).points
    assert point.day == date(2025, 1, 2)


def test_a_day_with_no_close_is_skipped_not_zeroed() -> None:
    payload = json.loads(fixture("nvda_split.json"))
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][2] = None
    series = prices.parse("NVDA", payload)
    assert len(series.points) == 5
    assert all(p.close > 0 for p in series.points)


def test_an_unknown_symbol_raises_and_is_not_cached(tmp_path: Path) -> None:
    handler = serve(fixture("not_found.json"), status=404)
    with client(tmp_path, handler) as c:
        for _ in range(2):
            with pytest.raises(prices.PriceError, match="No data found"):
                c.fetch("NOPE", START, END)
    assert len(handler.calls) == 2  # asked twice: an error is never served from the cache


def test_a_plain_text_upstream_error_raises_and_is_not_cached(tmp_path: Path) -> None:
    """The M36 spike met this exact body for 4186.T; a retry succeeded."""
    with client(tmp_path, serve(fixture("upstream_error.txt"))) as c:
        with pytest.raises(prices.PriceError, match="not JSON"):
            c.fetch("4186.T", START, END)
    assert not list((tmp_path / "cache").glob("*"))


def test_a_network_failure_raises_a_price_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with client(tmp_path, handler) as c, pytest.raises(prices.PriceError, match="ConnectTimeout"):
        c.fetch("NVDA", START, END)


def test_a_repeat_request_is_served_from_the_cache(tmp_path: Path) -> None:
    handler = serve(fixture("nvda_split.json"))
    with client(tmp_path, handler) as c:
        first = c.fetch("NVDA", START, END)
        second = c.fetch("NVDA", START, END)
        assert (c.requests, c.cache_hits) == (1, 1)
    assert first == second


def test_requests_are_paced(tmp_path: Path) -> None:
    sleeps: list[float] = []
    with client(tmp_path, serve(fixture("nvda_split.json")), sleeps) as c:
        c.fetch("NVDA", START, END)
        c.fetch("AMD", START, END)
    # The fake clock advances one second per reading, so the second request waits the rest.
    assert sleeps and all(0 < s <= prices.MIN_INTERVAL for s in sleeps)


@pytest.mark.parametrize(
    ("listing", "country", "index"),
    [
        ("NVDA", "US", "^GSPC"),
        ("TSM", "US", "^GSPC"),  # an ADR trades in New York
        ("8035.T", "JP", "^N225"),
        ("000660.KS", "KR", "^KS11"),
        ("ENR.DE", "DE", "^GDAXI"),
        ("SU.PA", "FR", "^FCHI"),
        ("^N225", "JP", "^N225"),
    ],
)
def test_each_listing_maps_to_its_market_index(listing: str, country: str, index: str) -> None:
    assert prices.listing_country(listing) == country
    assert prices.market_index(listing) == index


def test_an_unknown_market_is_refused() -> None:
    with pytest.raises(prices.PriceError, match=".XX"):
        prices.listing_country("ABC.XX")


def row(listing: str, day: date, close: float) -> PriceRow:
    return PriceRow(listing, day, close, close, "USD", prices.SOURCE)


def test_the_store_keeps_every_version_and_reads_the_latest(tmp_path: Path) -> None:
    with Store(tmp_path / "p.duckdb") as store:
        store.add_prices(
            [row("NVDA", date(2024, 6, 5), 100.0)], now=datetime(2026, 9, 1, tzinfo=UTC)
        )
        store.add_prices(
            [row("NVDA", date(2024, 6, 5), 101.0)], now=datetime(2026, 9, 20, tzinfo=UTC)
        )
        assert [r.close for r in store.prices("NVDA")] == [101.0]
        # As known before the revision, the old close is the answer.
        assert [r.close for r in store.prices("NVDA", known_at=date(2026, 9, 10))] == [100.0]
        assert store.price_summary() == [("NVDA", "USD", 1, date(2024, 6, 5), date(2024, 6, 5))]


@pytest.fixture
def loaded_db(tmp_path: Path) -> Path:
    db = tmp_path / "cli.duckdb"
    assert runner.invoke(app, ["load", str(MINI), "--db", str(db)]).exit_code == 0
    return db


def test_fetch_takes_every_company_listing_and_its_indices(
    loaded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def fake_fetch(self, listing: str, start: date, end: date) -> prices.Series:
        asked.append(listing)
        if listing == "TSM":
            raise prices.PriceError("TSM: No data found")
        return prices.Series(listing, "USD", [prices.PricePoint(date(2026, 9, 25), 10.0, 10.0)])

    monkeypatch.setattr(prices.PriceClient, "fetch", fake_fetch)
    args = [
        "prices",
        "fetch",
        "--to",
        "2026-09-27",
        "--db",
        str(loaded_db),
        "--cache",
        str(tmp_path / "c"),
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    # Mini companies with tickers: ASML, NVDA, TSM, VRT; all US listings, so one index.
    assert asked == ["ASML", "NVDA", "TSM", "VRT", "^GSPC"]
    assert "1 listings failed" in result.output  # TSM failed; the batch went on

    asked.clear()
    again = runner.invoke(app, args)
    assert "4 listings already held" in again.output
    assert asked == ["TSM"]  # only the listing that failed is asked again


def test_a_store_written_before_the_prices_table_reads_as_empty(tmp_path: Path) -> None:
    """A read-only open never creates tables, so an older store has no `prices` table until the
    next write. Readers, the UI's health route included, must not crash on it."""
    import duckdb

    db = tmp_path / "old.duckdb"
    with Store(db):
        pass
    con = duckdb.connect(str(db))
    con.execute("DROP TABLE prices")
    con.close()
    with Store(db, read_only=True) as store:
        assert store.price_summary() == []
        assert store.prices("NVDA") == []
        assert store.row_counts()["prices"] == 0
