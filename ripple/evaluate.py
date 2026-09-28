"""Returns and benchmarks for the backward test (Phase 4, M37).

The rules are fixed in docs/phase-4.md and pre-registered in M38; this module only implements
them, as pure functions over stored price rows:

- a US-trading-day master calendar (`^GSPC`'s trading days), and a window of h trading days
  starting on the first master day after the burst's UTC detection day (D138);
- per listing, entry at its first close strictly after the detection day and exit at its last
  close on or before the window's end, with no close within `SLACK_DAYS` of either end
  excluding the listing for that window (D138);
- buy-and-hold returns on adjusted closes (total return), converted to USD at same-day FX
  closes (D134, D135);
- the primary abnormal return against the equal-weighted universe, the stock included (D124,
  D134), and the secondary one against the listing's market index in local currency;
- sign adjustment by the exposure's sign (D133).

A missing price is never a zero return: the listing is left out of that window.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import fmean

from ripple import prices
from ripple.store import PriceRow

CALENDAR_LISTING = "^GSPC"
SLACK_DAYS = 5
HORIZONS = (20, 60)


@dataclass(frozen=True)
class Window:
    detected: date
    horizon: int
    # First master trading day after the detection day, and the master trading day `horizon`
    # trading days after it.
    start: date
    end: date


@dataclass(frozen=True)
class Leg:
    """One listing's entry and exit closes for a window."""

    listing: str
    entry: PriceRow
    exit: PriceRow


@dataclass(frozen=True)
class Outcome:
    listing: str
    entry_day: date
    exit_day: date
    usd_return: float
    local_return: float
    # USD buy-and-hold return minus the universe's equal-weighted USD return (primary).
    abnormal: float
    # Local return minus the listing's market index over the same days; None without the index.
    index_abnormal: float | None


def calendar(rows: list[PriceRow]) -> list[date]:
    """The master calendar: the trading days of `CALENDAR_LISTING`, in order."""
    return sorted({r.day for r in rows})


def window(days: list[date], detected: date, horizon: int) -> Window | None:
    """The window of `horizon` master trading days after `detected`, or None if the calendar
    ends before it does (the burst is too recent to score at this horizon)."""
    start = bisect_right(days, detected)
    end = start + horizon
    if end >= len(days):
        return None
    return Window(detected=detected, horizon=horizon, start=days[start], end=days[end])


def leg(rows: list[PriceRow], win: Window, slack: int = SLACK_DAYS) -> Leg | None:
    """Entry at the first close strictly after the detection day, exit at the last close on or
    before the window's end; None when either is more than `slack` days away (not yet listed,
    suspended or delisted) or they coincide."""
    if not rows:
        return None
    days = [r.day for r in rows]
    first = bisect_right(days, win.detected)
    if first == len(rows) or rows[first].day > win.detected + timedelta(days=slack):
        return None
    last = bisect_right(days, win.end) - 1
    if last <= first or rows[last].day < win.end - timedelta(days=slack):
        return None
    return Leg(listing=rows[first].listing, entry=rows[first], exit=rows[last])


def as_of(rows: list[PriceRow], day: date, slack: int = SLACK_DAYS) -> PriceRow | None:
    """The last row on or before `day`, if it is within `slack` days of it."""
    index = bisect_right([r.day for r in rows], day) - 1
    if index < 0 or rows[index].day < day - timedelta(days=slack):
        return None
    return rows[index]


def to_usd(price: float, currency: str, day: date, fx: dict[str, list[PriceRow]]) -> float | None:
    """`price` in USD at the FX close on `day` (or the last one within the slack), or None."""
    if currency == "USD":
        return price
    rule = prices.FX_BY_CURRENCY.get(currency)
    if rule is None:
        return None
    listing, operation = rule
    rate = as_of(fx.get(listing, []), day)
    if rate is None or rate.close <= 0:
        return None
    return price / rate.close if operation == "divide" else price * rate.close


def local_return(g: Leg) -> float:
    return g.exit.adj_close / g.entry.adj_close - 1


def usd_return(g: Leg, fx: dict[str, list[PriceRow]]) -> float | None:
    start = to_usd(g.entry.adj_close, g.entry.currency, g.entry.day, fx)
    end = to_usd(g.exit.adj_close, g.exit.currency, g.exit.day, fx)
    if start is None or end is None or start <= 0:
        return None
    return end / start - 1


def index_return(g: Leg, series: dict[str, list[PriceRow]]) -> float | None:
    """The listing's market index over the listing's own entry and exit days, locally."""
    rows = series.get(prices.market_index(g.listing), [])
    start, end = as_of(rows, g.entry.day), as_of(rows, g.exit.day)
    if start is None or end is None or start.adj_close <= 0:
        return None
    return end.adj_close / start.adj_close - 1


def outcomes(
    series: dict[str, list[PriceRow]],
    universe: list[str],
    win: Window,
    fx: dict[str, list[PriceRow]],
) -> dict[str, Outcome]:
    """Every universe listing with valid entry, exit and FX for the window, with its abnormal
    returns. The benchmark is the equal-weighted USD return of exactly these listings."""
    usd: dict[str, tuple[Leg, float]] = {}
    for listing in universe:
        g = leg(series.get(listing, []), win)
        if g is None:
            continue
        r = usd_return(g, fx)
        if r is not None:
            usd[listing] = (g, r)
    if not usd:
        return {}
    benchmark = fmean(r for _, r in usd.values())
    out: dict[str, Outcome] = {}
    for listing, (g, r) in usd.items():
        idx = index_return(g, series)
        local = local_return(g)
        out[listing] = Outcome(
            listing=listing,
            entry_day=g.entry.day,
            exit_day=g.exit.day,
            usd_return=r,
            local_return=local,
            abnormal=r - benchmark,
            index_abnormal=None if idx is None else local - idx,
        )
    return out


def benchmark(results: dict[str, Outcome]) -> float | None:
    """The equal-weighted universe USD return the abnormal returns were measured against."""
    if not results:
        return None
    return fmean(o.usd_return for o in results.values())


def sign_adjust(value: float, exposure: float) -> float:
    """A loser (negative exposure) that falls scores positive (D133)."""
    if exposure > 0:
        return value
    if exposure < 0:
        return -value
    raise ValueError("a company with zero exposure is not exposed")
