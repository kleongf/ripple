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

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import fmean

from scipy import stats

from ripple import prices
from ripple.store import PriceRow, Store

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


# --- priced-in check (Phase 4, M41) ---------------------------------------------------------
#
# PLAN §8: a company whose price already moves with the theme's news has less left to move. For
# each exposed company, the Spearman correlation between its daily abnormal return and the
# theme's daily coverage share over the `PRICED_IN_DAYS` US trading days up to the detection
# day. High means the stock already outperforms on the days the theme is in the news. It is
# reported next to novelty and never multiplied into it (D88).
#
# Daily returns use each listing's own trading days, so a Tokyo holiday is a missing day, not a
# stale zero return; the benchmark for a date is the mean USD return of the universe listings
# that traded on it.

PRICED_IN_DAYS = 60
PRICED_IN_MIN_DAYS = 40


def daily_usd_returns(
    rows: list[PriceRow], first: date, last: date, fx: dict[str, list[PriceRow]]
) -> dict[date, float]:
    """Close-to-close USD returns for each of the listing's trading days in [first, last]. The
    first day needs the listing's previous close, which may fall before `first`."""
    out: dict[date, float] = {}
    for previous, current in zip(rows, rows[1:], strict=False):
        if not first <= current.day <= last:
            continue
        start = to_usd(previous.adj_close, previous.currency, previous.day, fx)
        end = to_usd(current.adj_close, current.currency, current.day, fx)
        if start and end:
            out[current.day] = end / start - 1
    return out


def daily_abnormal(
    series: dict[str, list[PriceRow]],
    universe: list[str],
    first: date,
    last: date,
    fx: dict[str, list[PriceRow]],
) -> dict[str, dict[date, float]]:
    """Per listing, its daily USD return minus the mean of the universe listings that traded
    that day."""
    returns = {
        listing: daily_usd_returns(series.get(listing, []), first, last, fx) for listing in universe
    }
    by_day: dict[date, list[float]] = {}
    for daily in returns.values():
        for day, r in daily.items():
            by_day.setdefault(day, []).append(r)
    mean = {day: fmean(values) for day, values in by_day.items()}
    return {
        listing: {day: r - mean[day] for day, r in daily.items()}
        for listing, daily in returns.items()
    }


def priced_in(abnormal: dict[date, float], share: dict[date, float]) -> float | None:
    """Spearman correlation of daily abnormal return with the theme's coverage share on the same
    dates; None with fewer than `PRICED_IN_MIN_DAYS` paired days or when either side is
    constant."""
    days = sorted(set(abnormal) & set(share))
    if len(days) < PRICED_IN_MIN_DAYS:
        return None
    returns = [abnormal[d] for d in days]
    shares = [share[d] for d in days]
    if len(set(returns)) < 2 or len(set(shares)) < 2:
        return None
    rho = stats.spearmanr(returns, shares).statistic
    return None if math.isnan(rho) else float(rho)


def priced_in_window(days: list[date], detected: date) -> tuple[date, date] | None:
    """The `PRICED_IN_DAYS` master trading days ending on or before the detection day."""
    last = bisect_right(days, detected)
    if last < PRICED_IN_DAYS:
        return None
    return days[last - PRICED_IN_DAYS], days[last - 1]


def priced_in_for(
    store: Store,
    theme: str,
    listings: dict[str, str],
    universe: list[str],
    as_of: date,
    known_at: date | None = None,
) -> dict[str, float | None]:
    """The priced-in correlation for each company in `listings` (company ID to listing), as of
    `as_of`, against the universe's daily benchmark. Unknown (None) without prices or
    coverage."""
    days = calendar(store.prices(CALENDAR_LISTING, end=as_of, known_at=known_at))
    span = priced_in_window(days, as_of)
    if span is None:
        return dict.fromkeys(listings)
    first, last = span
    lookback = first - timedelta(days=14)  # room for the previous close before `first`
    wanted = sorted(set(universe) | set(listings.values()))
    series = {x: store.prices(x, start=lookback, end=last, known_at=known_at) for x in wanted}
    fx = {
        x: store.prices(x, start=lookback, end=last, known_at=known_at)
        for x, _ in prices.FX_BY_CURRENCY.values()
    }
    abnormal = daily_abnormal(series, wanted, first, last, fx)
    coverage = store.coverage(theme, start=first, end=last, known_at=known_at)
    share = {row.day: row.share for row in coverage if row.norm}
    return {
        company: priced_in(abnormal.get(listing, {}), share)
        for company, listing in listings.items()
    }


PRICED_IN_NOTE = (
    "priced_in is the Spearman correlation between the company's daily abnormal return (USD, "
    "against the universe) and the theme's daily coverage share over the last 60 US trading "
    "days: high means the stock already outperforms when the theme is in the news. Null means "
    "unknown (no prices or coverage, or under 40 paired days). Read it next to novelty; never "
    "combine the two into one score."
)


def with_priced_in(
    store: Store, result: dict, nodes: dict, as_of: date, known_at: date | None = None
) -> dict:
    """A `ScoreResult.to_dict()` with `priced_in` added to each company and the note appended."""
    universe = sorted({n.ticker for n in nodes.values() if n.type == "company" and n.ticker})
    listings = {
        r["company"]: nodes[r["company"]].ticker
        for r in result["results"]
        if nodes.get(r["company"]) is not None and nodes[r["company"]].ticker
    }
    values = priced_in_for(store, result["theme"], listings, universe, as_of, known_at)
    for r in result["results"]:
        value = values.get(r["company"])
        r["priced_in"] = None if value is None else round(value, 3)
    result["notes"] = [*result["notes"], PRICED_IN_NOTE]
    return result
