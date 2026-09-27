"""EDGAR client, filing cache and universe file (Phase 1, M10).

SEC's fair-access policy asks for a descriptive User-Agent with a contact email and at most
10 requests per second. The client reads the User-Agent from RIPPLE_SEC_USER_AGENT, refuses
to run without one, and waits at least 0.2 s between requests.
"""

import gzip
import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import yaml

from ripple.model import Node

SEC = "https://www.sec.gov"
DATA = "https://data.sec.gov"
TICKERS_URL = f"{SEC}/files/company_tickers.json"
USER_AGENT_ENV = "RIPPLE_SEC_USER_AGENT"
ANNUAL_FORMS = ("10-K", "20-F", "40-F")
MIN_INTERVAL = 0.2  # seconds between requests: 5 per second, half of SEC's limit
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
DEFAULT_CACHE = Path("data/raw")
DEFAULT_UNIVERSE = Path("data/universe.yaml")

# Cached file name per role. Exhibits, rendered R pages and the XBRL zip are not kept.
CACHE_NAMES = {
    "primary": "primary.htm.gz",
    "instance": "instance.xml.gz",
    "labels": "labels.xml.gz",
}


class EdgarConfigError(Exception):
    """The client cannot run as configured (for example, no User-Agent)."""


class EdgarError(Exception):
    """A request to EDGAR failed."""


def user_agent_from_env() -> str:
    value = os.environ.get(USER_AGENT_ENV, "").strip()
    if not value:
        raise EdgarConfigError(
            f"Set {USER_AGENT_ENV} to a name and contact email, e.g. 'Ripple you@example.com'. "
            "SEC requires it on every request."
        )
    if "@" not in value:
        raise EdgarConfigError(f"{USER_AGENT_ENV} must include a contact email address")
    return value


@dataclass(frozen=True)
class Filing:
    cik: int
    accession: str
    form: str
    filing_date: date
    report_date: date | None
    primary_document: str

    @property
    def base_url(self) -> str:
        return f"{SEC}/Archives/edgar/data/{self.cik}/{self.accession.replace('-', '')}"

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.primary_document}"


@dataclass(frozen=True)
class CachedFiling:
    filing: Filing
    directory: Path
    # Role ("primary", "instance", "labels") to gzipped file.
    files: dict[str, Path]


@dataclass(frozen=True)
class UniverseEntry:
    node: str
    ticker: str | None
    cik: int | None
    form: str | None
    sec: bool


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path = DEFAULT_CACHE,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache_dir = cache_dir
        self._http = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            transport=transport,
            timeout=60.0,
            follow_redirects=True,
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None
        self._tickers: dict[str, int] | None = None

    def get(self, url: str) -> bytes:
        for attempt in range(MAX_RETRIES + 1):
            self._wait_turn()
            response = self._http.get(url)
            self._last_request = self._monotonic()
            if response.status_code == 200:
                return response.content
            if response.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                raise EdgarError(f"GET {url} returned {response.status_code}")
            self._sleep(_retry_delay(response, attempt))
        raise AssertionError("unreachable")

    def _wait_turn(self) -> None:
        if self._last_request is None:
            return
        wait = MIN_INTERVAL - (self._monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)

    def tickers(self, refresh: bool = False) -> dict[str, int]:
        """Ticker to CIK, from SEC's company_tickers.json (cached on disk)."""
        if self._tickers is not None and not refresh:
            return self._tickers
        path = self.cache_dir / "company_tickers.json"
        if refresh or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.get(TICKERS_URL))
        data = json.loads(path.read_text())
        self._tickers = {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}
        return self._tickers

    def latest_annual_filing(self, cik: int) -> Filing | None:
        """The newest 10-K, 20-F or 40-F. Amendments (10-K/A) are skipped."""
        data = json.loads(self.get(f"{DATA}/submissions/CIK{cik:010d}.json"))
        recent = data["filings"]["recent"]
        for i, form in enumerate(recent["form"]):
            if form in ANNUAL_FORMS:
                return Filing(
                    cik=cik,
                    accession=recent["accessionNumber"][i],
                    form=form,
                    filing_date=date.fromisoformat(recent["filingDate"][i]),
                    report_date=_optional_date(recent["reportDate"][i]),
                    primary_document=recent["primaryDocument"][i],
                )
        return None

    def fetch_filing(self, filing: Filing) -> CachedFiling:
        """Download the primary document, XBRL instance and labels, gzipped, unless cached."""
        directory = self.cache_dir / str(filing.cik) / filing.accession
        meta_path = directory / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            files = {role: directory / CACHE_NAMES[role] for role in meta["urls"]}
            if all(path.exists() for path in files.values()):
                return CachedFiling(filing, directory, files)

        index = json.loads(self.get(f"{filing.base_url}/index.json"))
        names = {item["name"] for item in index["directory"]["item"]}
        urls = {role: f"{filing.base_url}/{name}" for role, name in _pick_files(filing, names)}

        directory.mkdir(parents=True, exist_ok=True)
        files = {}
        for role, url in urls.items():
            path = directory / CACHE_NAMES[role]
            path.write_bytes(gzip.compress(self.get(url)))
            files[role] = path
        meta = {
            **{k: str(v) if isinstance(v, date) else v for k, v in asdict(filing).items()},
            "urls": urls,
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        meta_path.write_text(json.dumps(meta, indent=1))
        return CachedFiling(filing, directory, files)


def _pick_files(filing: Filing, names: set[str]) -> list[tuple[str, str]]:
    picked = [("primary", filing.primary_document)]
    stem = filing.primary_document.rsplit(".", 1)[0]
    if f"{stem}_htm.xml" in names:  # inline XBRL: the extracted instance document
        picked.append(("instance", f"{stem}_htm.xml"))
    else:  # older filings ship a standalone instance such as abc-20190630.xml
        standalone = sorted(n for n in names if re.fullmatch(r"[a-z0-9]+-\d{8}\.xml", n))
        if standalone:
            picked.append(("instance", standalone[0]))
    labels = sorted(n for n in names if n.endswith("_lab.xml"))
    if labels:
        picked.append(("labels", labels[0]))
    return picked


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after", "")
    if retry_after.isdigit():
        return float(retry_after)
    return float(2**attempt)


def _optional_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def latest_cached(cache_dir: Path, cik: int) -> CachedFiling | None:
    """The most recently filed annual report in the cache for a company, or None."""
    found = []
    for meta_path in (cache_dir / str(cik)).glob("*/meta.json"):
        meta = json.loads(meta_path.read_text())
        filing = Filing(
            cik=int(meta["cik"]),
            accession=meta["accession"],
            form=meta["form"],
            filing_date=date.fromisoformat(meta["filing_date"]),
            report_date=_optional_date(meta["report_date"] or ""),
            primary_document=meta["primary_document"],
        )
        files = {role: meta_path.parent / CACHE_NAMES[role] for role in meta["urls"]}
        found.append(CachedFiling(filing, meta_path.parent, files))
    return max(found, key=lambda c: c.filing.filing_date, default=None)


def read_cached(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes())


def make_client(cache_dir: Path = DEFAULT_CACHE) -> EdgarClient:
    """A client configured from the environment. Raises EdgarConfigError without a User-Agent."""
    return EdgarClient(user_agent_from_env(), cache_dir)


# Universe file


def build_universe(nodes: Iterable[Node], client: EdgarClient) -> list[UniverseEntry]:
    """Resolve company nodes to SEC filers by ticker. A company is an SEC filer (sec: true) when
    it has a 10-K, 20-F or 40-F. Others are kept with sec: false (and their CIK, if they have
    one), so the universe file lists every company in the graph."""
    tickers = client.tickers()
    entries = []
    for node in sorted((n for n in nodes if n.type == "company"), key=lambda n: n.id):
        cik = tickers.get(node.ticker.upper()) if node.ticker else None
        filing = client.latest_annual_filing(cik) if cik else None
        entries.append(
            UniverseEntry(
                node=node.id,
                ticker=node.ticker,
                cik=cik,
                form=filing.form if filing else None,
                sec=filing is not None,
            )
        )
    return entries


def save_universe(path: Path, entries: list[UniverseEntry]) -> None:
    header = (
        "# Companies in scope for Phase 1 (docs/phase-1.md). sec: false means the company does\n"
        "# not file annual reports with the SEC and keeps its hand-seeded edges.\n"
    )
    body = yaml.safe_dump([asdict(e) for e in entries], sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + body)


def load_universe(path: Path) -> list[UniverseEntry]:
    return [UniverseEntry(**row) for row in yaml.safe_load(path.read_text()) or []]
