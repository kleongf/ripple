"""Daily adjusted prices for the universe (Phase 4, M36, D128).

The source is Yahoo's chart endpoint, chosen by the M36 spike: it covered all 61 listings and
the five country indices from 2022, while Stooq answered every request with a JavaScript browser
challenge. The endpoint is unofficial and has no published terms for programmatic use, so it is
used at low volume for research, every response is cached, and the source is recorded on every
stored row so a licensed API can replace it without mixing series.

`close` is split-adjusted; `adj_close` also adds dividends back, so returns on `adj_close` are
total returns. Days are the exchange's local trading days: a timestamp is shifted by the
exchange's UTC offset before its date is taken, so a Tokyo close lands on the Tokyo date.
"""

import gzip
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from ripple.news import user_agent

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{listing}"
SOURCE = "yahoo-chart"
DEFAULT_CACHE = Path("data/prices")
# One request per 1.5 s. A full refresh is about 66 requests, so this costs under two minutes.
MIN_INTERVAL = 1.5

# The secondary benchmark is the listing country's broad index (docs/phase-4.md, D124).
INDEX_BY_COUNTRY = {"US": "^GSPC", "JP": "^N225", "KR": "^KS11", "DE": "^GDAXI", "FR": "^FCHI"}
SUFFIX_COUNTRY = {"T": "JP", "KS": "KR", "DE": "DE", "PA": "FR"}
# How each non-USD currency converts to USD (docs/phase-4.md, D134), checked against the source
# on 2026-09-28: `JPY=X` and `KRW=X` are quoted in yen and won per dollar (115 and 1,188 in
# January 2022), so a local price is divided by them; `EURUSD=X` is dollars per euro (1.14), so
# a euro price is multiplied by it.
FX_BY_CURRENCY: dict[str, tuple[str, str]] = {
    "JPY": ("JPY=X", "divide"),
    "KRW": ("KRW=X", "divide"),
    "EUR": ("EURUSD=X", "multiply"),
}


def is_fx(listing: str) -> bool:
    return listing.endswith("=X")


class PriceError(Exception):
    """A price request failed or returned something unusable."""


@dataclass(frozen=True)
class PricePoint:
    day: date
    # Split-adjusted close.
    close: float
    # Split- and dividend-adjusted close: returns on it are total returns.
    adj_close: float


@dataclass(frozen=True)
class Series:
    listing: str
    currency: str
    points: list[PricePoint]


def listing_country(listing: str) -> str:
    """The market a listing trades on, from its suffix. US listings, ADRs included, have none."""
    if is_fx(listing):
        raise PriceError(f"{listing} is an FX rate, not a listing on a market")
    if listing.startswith("^"):
        country = next((c for c, i in INDEX_BY_COUNTRY.items() if i == listing), None)
        if country is None:
            raise PriceError(f"unknown index {listing}")
        return country
    if "." not in listing:
        return "US"
    suffix = listing.rsplit(".", 1)[1]
    if suffix not in SUFFIX_COUNTRY:
        raise PriceError(f"no market known for listing suffix .{suffix} ({listing})")
    return SUFFIX_COUNTRY[suffix]


def market_index(listing: str) -> str:
    return INDEX_BY_COUNTRY[listing_country(listing)]


def _cache_key(params: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]


class PriceClient:
    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        min_interval: float = MIN_INTERVAL,
    ) -> None:
        self.cache_dir = cache_dir
        self._http = httpx.Client(
            headers={"User-Agent": user_agent()},
            transport=transport,
            timeout=30.0,
            follow_redirects=True,
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._min_interval = min_interval
        self._last_request: float | None = None
        self.requests = 0
        self.cache_hits = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PriceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(self, listing: str, start: date, end: date) -> Series:
        """Daily closes for `listing` from `start` to `end` inclusive, in its own currency."""
        params = {
            "listing": listing,
            "period1": str(int(datetime.combine(start, datetime.min.time(), UTC).timestamp())),
            "period2": str(int(datetime.combine(end, datetime.max.time(), UTC).timestamp())),
            "interval": "1d",
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
        return parse(listing, self._get_cached(params))

    def _get_cached(self, params: dict[str, str]) -> dict[str, Any]:
        path = self.cache_dir / f"{_cache_key(params)}.json.gz"
        if path.exists():
            self.cache_hits += 1
            return json.loads(gzip.decompress(path.read_bytes()))
        body = self._request(params)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            # Upstream errors arrive as plain text ("upstream connect error ..."); never cached.
            raise PriceError(f"{params['listing']}: not JSON: {body[:120]!r}") from exc
        if _chart_error(payload) is None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(body.encode()))
        return payload

    def _request(self, params: dict[str, str]) -> str:
        if self._last_request is not None:
            wait = self._min_interval - (self._monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        query = {k: v for k, v in params.items() if k != "listing"}
        try:
            response = self._http.get(BASE_URL.format(listing=params["listing"]), params=query)
        except httpx.HTTPError as exc:
            raise PriceError(f"{params['listing']}: {type(exc).__name__}: {exc}") from exc
        finally:
            self._last_request = self._monotonic()
            self.requests += 1
        if response.status_code == 404:
            # A 404 still carries the JSON error; parse() reports it.
            return response.text
        if response.status_code != 200:
            raise PriceError(f"{params['listing']}: HTTP {response.status_code}")
        return response.text


def _chart_error(payload: dict[str, Any]) -> str | None:
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        return "no chart object"
    error = chart.get("error")
    if error:
        return error.get("description") or error.get("code") or "error"
    if not chart.get("result"):
        return "empty result"
    return None


def parse(listing: str, payload: dict[str, Any]) -> Series:
    error = _chart_error(payload)
    if error is not None:
        raise PriceError(f"{listing}: {error}")
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    currency = meta.get("currency")
    if not currency:
        raise PriceError(f"{listing}: no currency in the response")
    offset = int(meta.get("gmtoffset") or 0)
    stamps = result.get("timestamp") or []
    closes = result["indicators"]["quote"][0].get("close") or []
    adjusted = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose") or []
    if len(closes) != len(stamps) or len(adjusted) != len(stamps):
        raise PriceError(f"{listing}: close and timestamp arrays differ in length")
    points = [
        PricePoint(
            day=datetime.fromtimestamp(stamp + offset, UTC).date(),
            close=float(close),
            adj_close=float(adj),
        )
        for stamp, close, adj in zip(stamps, closes, adjusted, strict=True)
        # Yahoo leaves a null close on days a listing did not trade; skip rather than zero.
        if close is not None and adj is not None
    ]
    return Series(listing=listing, currency=currency, points=points)
