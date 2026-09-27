import gzip
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ripple import edgar
from ripple.edgar import (
    EdgarClient,
    EdgarConfigError,
    EdgarError,
    Filing,
    UniverseEntry,
    build_universe,
    load_universe,
    read_cached,
    save_universe,
    user_agent_from_env,
)
from ripple.model import Node

FIX = Path(__file__).parent / "fixtures" / "edgar"
UA = "Ripple test@example.com"
KLA_DIR = "https://www.sec.gov/Archives/edgar/data/319201/000031920126000027"

ROUTES: dict[str, bytes] = {
    "https://www.sec.gov/files/company_tickers.json": (FIX / "company_tickers.json").read_bytes(),
    "https://data.sec.gov/submissions/CIK0000319201.json": (
        FIX / "CIK0000319201.json"
    ).read_bytes(),
    "https://data.sec.gov/submissions/CIK0001046179.json": (
        FIX / "CIK0001046179.json"
    ).read_bytes(),
    f"{KLA_DIR}/index.json": (FIX / "kla_index.json").read_bytes(),
    f"{KLA_DIR}/klac-20260630.htm": b"<html>KLA annual report</html>",
    f"{KLA_DIR}/klac-20260630_htm.xml": b"<xbrl>facts</xbrl>",
    f"{KLA_DIR}/klac-20260630_lab.xml": b"<labels/>",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Recorder:
    """A mock SEC server: serves ROUTES (plus overrides) and records every request."""

    def __init__(self, overrides: dict[str, bytes | int | list[int | bytes]] | None = None):
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, bytes | int | list[int | bytes]] = {**ROUTES, **(overrides or {})}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url), 404)
        if isinstance(route, list):  # a sequence of responses, consumed in order
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, int):
            return httpx.Response(route)
        return httpx.Response(200, content=route)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


ClientFactory = Callable[..., tuple[EdgarClient, Recorder, FakeClock]]


@pytest.fixture
def make_client(tmp_path: Path) -> ClientFactory:
    def make(overrides: dict | None = None) -> tuple[EdgarClient, Recorder, FakeClock]:
        recorder = Recorder(overrides)
        clock = FakeClock()
        client = EdgarClient(
            user_agent=UA,
            cache_dir=tmp_path / "raw",
            transport=httpx.MockTransport(recorder),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        return client, recorder, clock

    return make


# User-Agent


def test_user_agent_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIPPLE_SEC_USER_AGENT", raising=False)
    with pytest.raises(EdgarConfigError, match="RIPPLE_SEC_USER_AGENT"):
        user_agent_from_env()


def test_user_agent_needs_an_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIPPLE_SEC_USER_AGENT", "Ripple")
    with pytest.raises(EdgarConfigError, match="email"):
        user_agent_from_env()


def test_user_agent_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIPPLE_SEC_USER_AGENT", UA)
    assert user_agent_from_env() == UA


def test_every_request_sends_the_user_agent(make_client: ClientFactory) -> None:
    client, recorder, _ = make_client()
    client.tickers()
    client.latest_annual_filing(319201)
    assert {r.headers["user-agent"] for r in recorder.requests} == {UA}


# Politeness


def test_requests_are_spaced_at_least_the_minimum_interval(make_client: ClientFactory) -> None:
    client, _, clock = make_client()
    client.get("https://www.sec.gov/files/company_tickers.json")
    client.get("https://www.sec.gov/files/company_tickers.json")
    assert clock.sleeps == [pytest.approx(0.2)]


def test_retries_rate_limit_then_succeeds(make_client: ClientFactory) -> None:
    url = "https://www.sec.gov/files/company_tickers.json"
    client, recorder, clock = make_client({url: [429, 503, ROUTES[url]]})
    assert client.get(url) == ROUTES[url]
    assert len(recorder.requests) == 3
    assert clock.sleeps[:1] == [1.0]  # backoff before the first retry


def test_gives_up_after_repeated_errors(make_client: ClientFactory) -> None:
    url = "https://www.sec.gov/files/company_tickers.json"
    client, recorder, _ = make_client({url: [503]})
    with pytest.raises(EdgarError, match="503"):
        client.get(url)
    assert len(recorder.requests) == 4


def test_not_found_is_not_retried(make_client: ClientFactory) -> None:
    client, recorder, _ = make_client()
    with pytest.raises(EdgarError, match="404"):
        client.get("https://www.sec.gov/nothing-here")
    assert len(recorder.requests) == 1


# Tickers and filings


def test_tickers_map_to_cik_and_are_cached(make_client: ClientFactory) -> None:
    client, recorder, _ = make_client()
    assert client.tickers()["KLAC"] == 319201
    assert client.tickers()["TSM"] == 1046179
    assert len(recorder.requests) == 1
    assert (client.cache_dir / "company_tickers.json").exists()


def test_latest_annual_filing_10k(make_client: ClientFactory) -> None:
    client, _, _ = make_client()
    assert client.latest_annual_filing(319201) == Filing(
        cik=319201,
        accession="0000319201-26-000027",
        form="10-K",
        filing_date=date(2026, 8, 6),
        report_date=date(2026, 6, 30),
        primary_document="klac-20260630.htm",
    )


def test_latest_annual_filing_20f(make_client: ClientFactory) -> None:
    client, _, _ = make_client()
    filing = client.latest_annual_filing(1046179)
    assert filing is not None
    assert (filing.form, filing.report_date) == ("20-F", date(2025, 12, 31))


def test_amendments_and_other_forms_are_skipped(make_client: ClientFactory) -> None:
    data = json.loads(ROUTES["https://data.sec.gov/submissions/CIK0000319201.json"])
    recent = data["filings"]["recent"]
    recent["form"][0] = "10-K/A"  # an amendment filed after the 10-K must not win
    client, _, _ = make_client(
        {"https://data.sec.gov/submissions/CIK0000319201.json": json.dumps(data).encode()}
    )
    filing = client.latest_annual_filing(319201)
    assert filing is not None
    assert filing.form == "10-K"


def test_no_annual_filing_returns_none(make_client: ClientFactory) -> None:
    data = json.loads(ROUTES["https://data.sec.gov/submissions/CIK0000319201.json"])
    recent = data["filings"]["recent"]
    recent["form"] = ["8-K"] * len(recent["form"])
    client, _, _ = make_client(
        {"https://data.sec.gov/submissions/CIK0000319201.json": json.dumps(data).encode()}
    )
    assert client.latest_annual_filing(319201) is None


def test_filing_urls() -> None:
    filing = Filing(319201, "0000319201-26-000027", "10-K", date(2026, 8, 6), None, "k.htm")
    assert filing.base_url == KLA_DIR
    assert filing.url == f"{KLA_DIR}/k.htm"


# Cache


def test_fetch_filing_caches_primary_instance_and_labels(make_client: ClientFactory) -> None:
    client, recorder, _ = make_client()
    filing = client.latest_annual_filing(319201)
    assert filing is not None
    cached = client.fetch_filing(filing)
    assert set(cached.files) == {"primary", "instance", "labels"}
    assert read_cached(cached.files["instance"]) == b"<xbrl>facts</xbrl>"
    assert cached.files["primary"].suffix == ".gz"
    with gzip.open(cached.files["primary"]) as f:
        assert f.read() == b"<html>KLA annual report</html>"
    meta = json.loads((cached.directory / "meta.json").read_text())
    assert meta["accession"] == "0000319201-26-000027"
    assert meta["urls"]["instance"] == f"{KLA_DIR}/klac-20260630_htm.xml"
    # Exhibits, R pages and the zip are not downloaded.
    assert not any("exhibit" in u or "R1.htm" in u or ".zip" in u for u in recorder.urls())


def test_second_fetch_uses_the_cache(make_client: ClientFactory) -> None:
    client, recorder, _ = make_client()
    filing = client.latest_annual_filing(319201)
    assert filing is not None
    client.fetch_filing(filing)
    before = len(recorder.requests)
    again = client.fetch_filing(filing)
    assert len(recorder.requests) == before
    assert set(again.files) == {"primary", "instance", "labels"}


def test_filing_without_xbrl_caches_primary_only(make_client: ClientFactory) -> None:
    index = json.loads(ROUTES[f"{KLA_DIR}/index.json"])
    index["directory"]["item"] = [
        i for i in index["directory"]["item"] if not i["name"].endswith(".xml")
    ]
    client, _, _ = make_client({f"{KLA_DIR}/index.json": json.dumps(index).encode()})
    filing = client.latest_annual_filing(319201)
    assert filing is not None
    assert set(client.fetch_filing(filing).files) == {"primary"}


# Universe


def company(id_: str, ticker: str | None) -> Node:
    return Node(id=id_, type="company", label=id_.split("/")[1], ticker=ticker)


def test_build_universe_resolves_sec_filers(make_client: ClientFactory) -> None:
    client, _, _ = make_client()
    nodes = [
        company("company/kla", "KLAC"),
        company("company/tsmc", "TSM"),
        company("company/sk-hynix", "000660.KS"),
        company("company/private-co", None),
        Node(id="product/euv", type="product", label="EUV"),
    ]
    entries = build_universe(nodes, client)
    assert entries == [
        UniverseEntry("company/kla", "KLAC", 319201, "10-K", True),
        UniverseEntry("company/private-co", None, None, None, False),
        UniverseEntry("company/sk-hynix", "000660.KS", None, None, False),
        UniverseEntry("company/tsmc", "TSM", 1046179, "20-F", True),
    ]


def test_universe_round_trips_through_yaml(tmp_path: Path) -> None:
    entries = [
        UniverseEntry("company/kla", "KLAC", 319201, "10-K", True),
        UniverseEntry("company/sk-hynix", "000660.KS", None, None, False),
    ]
    path = tmp_path / "universe.yaml"
    save_universe(path, entries)
    assert load_universe(path) == entries


# CLI


runner = CliRunner()


def test_fetch_refuses_without_user_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ripple.cli import app

    monkeypatch.delenv("RIPPLE_SEC_USER_AGENT", raising=False)
    result = runner.invoke(app, ["fetch", "company/kla", "--cache", str(tmp_path)])
    assert result.exit_code == 1
    assert "RIPPLE_SEC_USER_AGENT" in result.output


def test_fetch_command_caches_universe_filers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make_client: ClientFactory
) -> None:
    from ripple.cli import app

    client, _, _ = make_client()
    monkeypatch.setattr(edgar, "make_client", lambda cache_dir: client)
    universe = tmp_path / "universe.yaml"
    save_universe(
        universe,
        [
            UniverseEntry("company/kla", "KLAC", 319201, "10-K", True),
            UniverseEntry("company/sk-hynix", "000660.KS", None, None, False),
        ],
    )
    result = runner.invoke(
        app, ["fetch", "--universe", str(universe), "--cache", str(tmp_path / "raw")]
    )
    assert result.exit_code == 0, result.output
    assert "company/kla" in result.output and "10-K" in result.output
    assert "company/sk-hynix" not in result.output  # not an SEC filer: skipped


def test_listed_company_without_annual_filing_is_not_a_filer(make_client: ClientFactory) -> None:
    data = json.loads(ROUTES["https://data.sec.gov/submissions/CIK0000319201.json"])
    data["filings"]["recent"]["form"] = ["6-K"] * len(data["filings"]["recent"]["form"])
    client, _, _ = make_client(
        {"https://data.sec.gov/submissions/CIK0001045810.json": json.dumps(data).encode()}
    )
    [entry] = build_universe([company("company/nvidia", "NVDA")], client)
    assert entry == UniverseEntry("company/nvidia", "NVDA", 1045810, None, False)


def test_latest_cached_filing(make_client: ClientFactory, tmp_path: Path) -> None:
    client, _, _ = make_client()
    assert edgar.latest_cached(client.cache_dir, 319201) is None
    filing = client.latest_annual_filing(319201)
    assert filing is not None
    client.fetch_filing(filing)
    cached = edgar.latest_cached(client.cache_dir, 319201)
    assert cached is not None
    assert cached.filing == filing
    assert set(cached.files) == {"primary", "instance", "labels"}
