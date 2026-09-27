"""Ranking expectations for the real seed (tests/golden/*.yaml, M9)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from ripple.graph import build_graph
from ripple.propagate import Shock
from ripple.score import company_exposures, score
from ripple.store import Store

ROOT = Path(__file__).parent.parent
SEED = ROOT / "data" / "seed"
SOURCES = ROOT / "data" / "sources.yaml"
GOLDEN = sorted((Path(__file__).parent / "golden").glob("*.yaml"))

pytestmark = pytest.mark.skipif(not SEED.exists(), reason="no seed data yet")


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Store:
    s = Store(tmp_path_factory.mktemp("golden") / "seed.duckdb")
    # The graph the tool uses: every source listed in data/sources.yaml (D40, D56).
    listed = yaml.safe_load(SOURCES.read_text()) if SOURCES.exists() else {"seed": "data/seed"}
    s.load_sources({name: ROOT / path for name, path in listed.items()}, now=datetime.now(UTC))
    return s


@pytest.mark.parametrize("path", GOLDEN, ids=[p.stem for p in GOLDEN])
def test_golden(store: Store, path: Path) -> None:
    spec: dict[str, Any] = yaml.safe_load(path.read_text())
    as_of = datetime.now(UTC).date()
    top_n = spec.get("top_n", 10)
    ranked = [r.company for r in score(store, spec["theme"], as_of=as_of, limit=None).results]
    top = ranked[:top_n]
    graph = build_graph(store.snapshot(as_of))
    exposure = company_exposures(graph, Shock(spec["theme"]), max_hops=5)

    failures = []
    for company in spec.get("in_top", []):
        if company not in top:
            failures.append(f"{company} expected in top {top_n}, rank {_rank(ranked, company)}")
    for company in spec.get("not_in_top", []):
        if company in top:
            failures.append(
                f"{company} expected outside top {top_n}, rank {_rank(ranked, company)}"
            )
    for company in spec.get("nonzero", []):
        if company not in exposure:
            failures.append(f"{company} expected nonzero exposure")
    for company in spec.get("zero", []):
        if company in exposure:
            failures.append(f"{company} expected zero exposure, got {exposure[company]:+.4f}")
    for company in spec.get("positive", []):
        if exposure.get(company, 0.0) <= 0:
            failures.append(f"{company} expected positive, got {exposure.get(company, 0.0):+.4f}")
    for company in spec.get("negative", []):
        if exposure.get(company, 0.0) >= 0:
            failures.append(f"{company} expected negative, got {exposure.get(company, 0.0):+.4f}")
    for company, bound in spec.get("at_least", {}).items():
        if exposure.get(company, 0.0) < bound:
            failures.append(f"{company} expected >= {bound}, got {exposure.get(company, 0.0):+.4f}")
    assert not failures, "\n".join(failures)


def _rank(ranked: list[str], company: str) -> str:
    return str(ranked.index(company) + 1) if company in ranked else "none"
