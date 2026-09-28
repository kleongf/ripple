"""GDELT client (Phase 2, M24). Never touches the network: every response is a fixture."""

from datetime import date
from pathlib import Path

import httpx
import pytest

from ripple import news
from ripple.model import Node

FIXTURES = Path(__file__).parent / "fixtures" / "gdelt"
START = date(2025, 1, 1)
END = date(2025, 1, 5)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def transport(*responses: httpx.Response) -> httpx.MockTransport:
    """Answer each request with the next response; repeat the last one after that."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.MockTransport(handler)


def client(tmp_path: Path, *responses: httpx.Response, **kwargs: object) -> news.NewsClient:
    return news.NewsClient(
        cache_dir=tmp_path / "news",
        transport=transport(*responses),
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
        **kwargs,  # type: ignore[arg-type]
    )


def ok(body: str) -> httpx.Response:
    return httpx.Response(200, text=body)


# --- parsing --------------------------------------------------------------------------


def test_volume_parses_matched_and_norm(tmp_path: Path) -> None:
    with client(tmp_path, ok(fixture("volume_thick.json"))) as c:
        points = c.volume('("test theme")', START, END)
    assert [p.day for p in points] == [date(2025, 1, d) for d in range(1, 6)]
    assert [p.matched for p in points] == [40, 44, 38, 41, 39]
    assert points[0].norm == 500_000
    assert points[0].share == pytest.approx(40 / 500_000)


def test_tone_parses_one_value_per_day(tmp_path: Path) -> None:
    with client(tmp_path, ok(fixture("tone.json"))) as c:
        tones = c.tone('("test theme")', START, END)
    assert tones[date(2025, 1, 4)] == pytest.approx(-5.6)
    assert len(tones) == 5


def test_articles_parses_urls_and_dates(tmp_path: Path) -> None:
    with client(tmp_path, ok(fixture("artlist.json"))) as c:
        articles = c.articles('("test theme")', START, END)
    assert [a.url for a in articles] == ["https://example.com/a", "https://example.com/b"]
    assert articles[0].day == date(2025, 1, 4)


def test_thin_series_still_parses(tmp_path: Path) -> None:
    with client(tmp_path, ok(fixture("volume_thin.json"))) as c:
        points = c.volume('("thin")', START, END)
    assert [p.matched for p in points] == [0, 1, 0, 2, 1]


# --- rate limiting --------------------------------------------------------------------


def test_plain_text_refusal_is_treated_as_rate_limiting(tmp_path: Path) -> None:
    """GDELT answers 200 with a plain-text scolding, not an error status (M24 spike)."""
    with client(tmp_path, ok(fixture("refusal.txt"))) as c:
        with pytest.raises(news.RateLimited):
            c.volume('("test theme")', START, END)


def test_429_is_retried_then_succeeds(tmp_path: Path) -> None:
    with client(tmp_path, httpx.Response(429), ok(fixture("volume_thick.json"))) as c:
        points = c.volume('("test theme")', START, END)
    assert len(points) == 5
    assert c.requests == 2


def test_429_beyond_the_retry_budget_raises(tmp_path: Path) -> None:
    with client(tmp_path, httpx.Response(429)) as c:
        with pytest.raises(news.RateLimited):
            c.volume('("test theme")', START, END)
    assert c.requests == news.MAX_RETRIES + 1


def test_other_statuses_raise_news_error(tmp_path: Path) -> None:
    with client(tmp_path, httpx.Response(500)) as c:
        with pytest.raises(news.NewsError):
            c.volume('("test theme")', START, END)


def test_malformed_json_raises(tmp_path: Path) -> None:
    with client(tmp_path, ok("{not json")) as c:
        with pytest.raises(news.NewsError):
            c.volume('("test theme")', START, END)


def test_requests_are_paced(tmp_path: Path) -> None:
    waits: list[float] = []
    c = news.NewsClient(
        cache_dir=tmp_path / "news",
        transport=transport(ok(fixture("volume_thick.json"))),
        sleep=waits.append,
        monotonic=lambda: 0.0,
    )
    with c:
        c.volume('("a")', START, END)
        c.volume('("b")', START, END)
    # The clock never advances, so the second request waits the full interval.
    assert waits == [pytest.approx(news.MIN_INTERVAL)]


# --- cache ----------------------------------------------------------------------------


def test_second_identical_call_is_served_from_cache(tmp_path: Path) -> None:
    with client(tmp_path, ok(fixture("volume_thick.json"))) as c:
        first = c.volume('("test theme")', START, END)
        second = c.volume('("test theme")', START, END)
    assert first == second
    assert (c.requests, c.cache_hits) == (1, 1)


def test_cache_survives_a_new_client(tmp_path: Path) -> None:
    with client(tmp_path, ok(fixture("volume_thick.json"))) as c:
        c.volume('("test theme")', START, END)
    # A transport that would fail if used at all.
    with client(tmp_path, httpx.Response(500)) as second:
        points = second.volume('("test theme")', START, END)
    assert len(points) == 5
    assert second.requests == 0


def test_a_different_query_is_a_different_cache_entry(tmp_path: Path) -> None:
    with client(tmp_path, ok(fixture("volume_thick.json"))) as c:
        c.volume('("one")', START, END)
        c.volume('("two")', START, END)
    assert c.requests == 2


# --- queries --------------------------------------------------------------------------


def test_theme_query_comes_from_the_node(tmp_path: Path) -> None:
    node = Node(id="theme/t", type="theme", label="T", kind="volume", query='("x" OR "y")')
    assert news.theme_query(node) == '("x" OR "y")'


def test_theme_without_a_query_raises() -> None:
    node = Node(id="theme/t", type="theme", label="T", kind="volume")
    with pytest.raises(news.NewsError):
        news.theme_query(node)


def test_company_query_uses_label_and_aliases() -> None:
    node = Node(
        id="company/asml",
        type="company",
        label="ASML Holding",
        aliases=["ASML", "AS"],
        ticker="ASML",
    )
    # "AS" is too short to identify a company on its own and is dropped.
    assert news.company_query(node) == '("ASML Holding" OR "ASML")'


def test_history_before_2017_is_clamped(tmp_path: Path) -> None:
    """Timeline modes start at 2017-01-01, so an earlier request is pulled forward (D85)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(fixture("volume_thick.json"))

    c = news.NewsClient(
        cache_dir=tmp_path / "news",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
    )
    with c:
        c.volume('("test theme")', date(2010, 5, 1), END)
    assert "startdatetime=20170101000000" in str(seen[0].url)


def test_a_network_failure_stops_the_batch_like_a_refusal(tmp_path: Path) -> None:
    """A TLS timeout used to crash `signals fetch` with a traceback; it is now a clean stop."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("handshake timed out")

    c = news.NewsClient(
        cache_dir=tmp_path / "news",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
    )
    with c, pytest.raises(news.RateLimited, match="unreachable"):
        c.volume('("test theme")', START, END)
