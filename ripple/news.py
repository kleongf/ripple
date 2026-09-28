"""GDELT DOC 2.0 client and response cache (Phase 2, M24).

GDELT asks for at most one request every 5 seconds and enforces it hard: over the limit it
answers either HTTP 429 or a 200 whose body is a plain-text scolding rather than JSON. Both are
treated as rate limiting here. The client waits at least MIN_INTERVAL between requests, backs
off on a refusal, and caches every successful response so a repeat run costs nothing.

Timeline modes cover 2017-01-01 to now and return one point per day for a multi-year span, so a
theme's entire history arrives in a single request (D85). The non-timeline modes, including
ArtList, only look at the last three months of whatever window is asked for.
"""

import gzip
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from ripple.model import Node

BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
# GDELT does not require a User-Agent, but sending a contact is the same courtesy EDGAR asks
# for, and it reuses the variable already set in ~/.zshenv (D45).
USER_AGENT_ENV = "RIPPLE_SEC_USER_AGENT"
FALLBACK_USER_AGENT = "Ripple/0.1 (research tool)"
DEFAULT_CACHE = Path("data/news")


def user_agent() -> str:
    return os.environ.get(USER_AGENT_ENV, "").strip() or FALLBACK_USER_AGENT


# GDELT enforces one request per 5 s **per IP**, and this machine's address is shared, so the
# budget is often already spent by unrelated traffic: batches were refused on their own first
# request no matter how long the preceding gap, while isolated requests succeeded whenever the
# address happened to be quiet. Our own interval is therefore not the binding constraint; 8 s
# stays inside GDELT's guidance and makes a successful run fast. The way through contention is
# many independent attempts plus the response cache, not longer sleeps (D102).
MIN_INTERVAL = 8.0
MAX_RETRIES = 1
FIRST_BACKOFF = 45.0
BACKOFF_FACTOR = 1.0

# The earliest date timeline modes cover, confirmed by the M24 spike (D102).
HISTORY_START = date(2017, 1, 1)

VOLUME_MODE = "TimelineVolRaw"
TONE_MODE = "TimelineTone"
ARTICLE_MODE = "ArtList"
MAX_ARTICLES = 250

# A rate-limit refusal arrives as a 200 with this text instead of JSON.
REFUSAL_MARKERS = ("please limit requests", "rate limit", "too many requests")


class NewsError(Exception):
    """A GDELT request failed."""


class RateLimited(NewsError):
    """GDELT refused the request for rate reasons; the batch should slow down or stop."""


class Unreachable(RateLimited):
    """GDELT could not be reached at all (timeout, TLS or connection failure). Treated like a
    refusal: stop the batch cleanly and let a later run resume from the cache."""


@dataclass(frozen=True)
class VolumePoint:
    """One day of coverage for a query."""

    day: date
    # Articles matching the query.
    matched: int
    # All articles GDELT monitored that day, the denominator for coverage share.
    norm: int

    @property
    def share(self) -> float:
        return self.matched / self.norm if self.norm else 0.0


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    domain: str
    day: date | None
    language: str


def theme_query(node: Node) -> str:
    """The GDELT query stored on a theme node (D87)."""
    if not (node.query or "").strip():
        raise NewsError(f"{node.id} has no query; see validator warning W10")
    return node.query.strip()  # type: ignore[union-attr]


def company_query(node: Node) -> str:
    """A company's query, built from its label, aliases and ticker rather than stored (D87).

    Aliases that are too short or too generic to identify a company on their own are dropped,
    because GDELT has no entity resolution and a two-letter token matches everything.
    """
    names = [node.label, *node.aliases]
    phrases: list[str] = []
    for name in names:
        cleaned = name.strip().strip(",")
        if len(cleaned) < 4 or not any(ch.isalpha() for ch in cleaned):
            continue
        phrase = f'"{cleaned}"'
        if phrase not in phrases:
            phrases.append(phrase)
    if not phrases:
        raise NewsError(f"{node.id} has no usable name for a query")
    return "(" + " OR ".join(phrases) + ")"


def _stamp(day: date, end_of_day: bool = False) -> str:
    return day.strftime("%Y%m%d") + ("235959" if end_of_day else "000000")


def _cache_key(params: dict[str, str]) -> str:
    canonical = json.dumps(params, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:20]


def query_hash(query: str) -> str:
    """Identifies which query produced a stored series, so a query change is visible (D86)."""
    return hashlib.sha256(query.encode()).hexdigest()[:16]


class NewsClient:
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
            headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"},
            transport=transport,
            timeout=120.0,
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

    def __enter__(self) -> "NewsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- volume, tone and articles ------------------------------------------------------

    def volume(self, query: str, start: date, end: date) -> list[VolumePoint]:
        """Daily matched-article counts and the all-coverage denominator."""
        payload = self._timeline(VOLUME_MODE, query, start, end)
        points = []
        for row in _series_rows(payload):
            day = _parse_day(row.get("date"))
            if day is None:
                continue
            points.append(
                VolumePoint(
                    day=day, matched=int(row.get("value") or 0), norm=int(row.get("norm") or 0)
                )
            )
        return points

    def tone(self, query: str, start: date, end: date) -> dict[date, float]:
        """Mean tone of matching articles per day. Standardized later, never used as a sign."""
        payload = self._timeline(TONE_MODE, query, start, end)
        out: dict[date, float] = {}
        for row in _series_rows(payload):
            day = _parse_day(row.get("date"))
            value = row.get("value")
            if day is not None and value is not None:
                out[day] = float(value)
        return out

    def articles(self, query: str, start: date, end: date, limit: int = 75) -> list[Article]:
        """Recent articles for a query. ArtList only sees the last 3 months (D85)."""
        params = {
            "query": query,
            "mode": ARTICLE_MODE,
            "format": "json",
            "maxrecords": str(min(limit, MAX_ARTICLES)),
            "sort": "DateDesc",
            "startdatetime": _stamp(start),
            "enddatetime": _stamp(end, end_of_day=True),
        }
        payload = self._get_json(params)
        out = []
        for row in payload.get("articles") or []:
            url = (row.get("url") or "").strip()
            if not url:
                continue
            out.append(
                Article(
                    url=url,
                    title=(row.get("title") or "").strip(),
                    domain=(row.get("domain") or "").strip(),
                    day=_parse_day(row.get("seendate")),
                    language=(row.get("language") or "").strip(),
                )
            )
        return out

    def _timeline(self, mode: str, query: str, start: date, end: date) -> dict[str, Any]:
        if start < HISTORY_START:
            start = HISTORY_START
        return self._get_json(
            {
                "query": query,
                "mode": mode,
                "format": "json",
                "startdatetime": _stamp(start),
                "enddatetime": _stamp(end, end_of_day=True),
            }
        )

    # --- transport, cache and pacing ----------------------------------------------------

    def _get_json(self, params: dict[str, str]) -> dict[str, Any]:
        raw = self._get_cached(params)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NewsError(
                f"GDELT returned non-JSON for {params.get('mode')}: {raw[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise NewsError(f"GDELT returned {type(payload).__name__}, expected an object")
        return payload

    def _get_cached(self, params: dict[str, str]) -> str:
        path = self.cache_dir / f"{_cache_key(params)}.json.gz"
        if path.exists():
            self.cache_hits += 1
            return gzip.decompress(path.read_bytes()).decode("utf-8")
        body = self._request(params)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(body.encode("utf-8")))
        return body

    def _request(self, params: dict[str, str]) -> str:
        delay = FIRST_BACKOFF
        for attempt in range(MAX_RETRIES + 1):
            self._wait_turn()
            try:
                response = self._http.get(BASE_URL, params=params)
            except httpx.TransportError as exc:
                raise Unreachable(f"GDELT unreachable for {params.get('mode')}: {exc}") from exc
            self._last_request = self._monotonic()
            self.requests += 1
            refused = response.status_code == 429 or _is_refusal(response.text)
            if not refused and response.status_code == 200:
                return response.text
            if not refused:
                raise NewsError(f"GDELT returned {response.status_code} for {params.get('mode')}")
            if attempt == MAX_RETRIES:
                raise RateLimited(
                    f"GDELT rate-limited {MAX_RETRIES + 1} attempts for {params.get('mode')}; "
                    "wait a few minutes and rerun, cached responses are kept"
                )
            self._sleep(delay)
            delay *= BACKOFF_FACTOR
        raise AssertionError("unreachable")

    def _wait_turn(self) -> None:
        if self._last_request is None:
            return
        wait = self._min_interval - (self._monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)


def _is_refusal(text: str) -> bool:
    head = text[:400].lower()
    return not text.lstrip().startswith(("{", "[")) and any(m in head for m in REFUSAL_MARKERS)


def _series_rows(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for series in payload.get("timeline") or []:
        yield from series.get("data") or []


def _parse_day(value: object) -> date | None:
    """GDELT stamps are '20170101T000000Z'; ArtList uses the same shape."""
    if not isinstance(value, str) or len(value) < 8:
        return None
    try:
        return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None


def latest_day(points: Sequence[VolumePoint]) -> date | None:
    return max((p.day for p in points), default=None)
