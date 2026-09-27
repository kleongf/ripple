"""Every expected number from docs/phase-0.md M2. If one of these moves, the change is wrong
or the fixture needs a written reason."""

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest

from ripple.graph import Graph, build_graph
from ripple.paths import min_hops, top_paths
from ripple.propagate import Shock, propagate, shock_vector
from ripple.store import Store

MINI = Path(__file__).parent / "fixtures" / "mini"
AS_OF = date(2026, 9, 26)
LOADED = datetime(2026, 9, 1, tzinfo=UTC)
TOL = 1e-9

AI_COMPUTE_EXPOSURE = {
    "company/nvidia": 0.900,
    "company/parts": 0.450,
    "company/tsmc": 0.240,
    "company/multi": 0.164,
    "company/asml": 0.160,
    "company/vertiv": 0.135,
    "company/rumor": 0.0,
    "company/aircool": 0.0,
}
AI_COMPUTE_MIN_HOPS = {
    "company/nvidia": 2,
    "company/parts": 3,
    "company/tsmc": 3,
    "company/multi": 2,
    "company/asml": 4,
    "company/vertiv": 3,
}


def graph_from(directory: Path, tmp_path: Path, min_confidence: float = 0.5) -> Graph:
    with Store(tmp_path / "mini.duckdb") as store:
        store.load(directory, now=LOADED)
        return build_graph(store.snapshot(AS_OF), min_confidence=min_confidence)


@pytest.fixture
def mini(tmp_path: Path) -> Graph:
    return graph_from(MINI, tmp_path)


def exposure(graph: Graph, theme: str, hops: int = 5, direction: str = "up") -> dict[str, float]:
    total = propagate(graph.W, shock_vector(graph, Shock(theme, 1.0, direction)), hops)
    return {node_id: float(total[i]) for node_id, i in graph.index.items()}


# M4: propagation


def test_ai_compute_exposures(mini: Graph) -> None:
    result = exposure(mini, "theme/ai-compute")
    for company, expected in AI_COMPUTE_EXPOSURE.items():
        assert result[company] == pytest.approx(expected, abs=TOL), company


def test_max_hops_three_cuts_the_four_edge_path(mini: Graph) -> None:
    result = exposure(mini, "theme/ai-compute", hops=3)
    assert result["company/asml"] == pytest.approx(0.0, abs=TOL)
    assert result["company/multi"] == pytest.approx(0.100, abs=TOL)


def test_min_confidence_zero_includes_rumor(tmp_path: Path) -> None:
    result = exposure(graph_from(MINI, tmp_path, min_confidence=0.0), "theme/ai-compute")
    assert result["company/rumor"] == pytest.approx(0.500, abs=TOL)
    for company, expected in AI_COMPUTE_EXPOSURE.items():
        if company != "company/rumor":
            assert result[company] == pytest.approx(expected, abs=TOL), company


def test_cooling_shift(mini: Graph) -> None:
    result = exposure(mini, "theme/cooling-shift")
    assert result["company/aircool"] == pytest.approx(-0.210, abs=TOL)
    assert result["company/vertiv"] == pytest.approx(0.090, abs=TOL)


def test_direction_down_flips_sign(mini: Graph) -> None:
    result = exposure(mini, "theme/ai-compute", direction="down")
    assert result["company/nvidia"] == pytest.approx(-0.900, abs=TOL)


def test_matrix_has_no_confidence_factor(tmp_path: Path) -> None:
    graph = graph_from(MINI, tmp_path, min_confidence=0.0)
    i, j = graph.index["product/accelerators"], graph.index["company/rumor"]
    assert graph.W[i, j] == pytest.approx(0.5)


def test_cycle_stays_finite_and_matches_truncated_sum(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    shutil.copytree(MINI, seed)
    evidence = "evidence: [{url: https://example.com/fixture, note: cycle, accessed: 2026-01-01}]"
    common = (
        f"weight_source: manual, polarity: 1, confidence: 1.0, valid_from: 2020-01-01, {evidence}"
    )
    (seed / "cycle.yaml").write_text(
        "- {id: company/a, type: company, label: A}\n"
        "- {id: company/b, type: company, label: B}\n"
        f"- {{src: company/a, rel: SUPPLIES, dst: company/nvidia, weight: 0.5, {common}}}\n"
        f"- {{src: company/a, rel: SUPPLIES, dst: company/b, weight: 0.5, {common}}}\n"
        f"- {{src: company/b, rel: SUPPLIES, dst: company/a, weight: 0.5, {common}}}\n"
    )
    result = exposure(graph_from(seed, tmp_path), "theme/ai-compute")
    # a: 0.9 x 0.5 at hop 3, then around the cycle once more at hop 5.
    assert result["company/a"] == pytest.approx(0.45 + 0.1125, abs=TOL)
    assert result["company/b"] == pytest.approx(0.225, abs=TOL)
    assert np.isfinite(list(result.values())).all()


# M5: paths


def test_min_hops(mini: Graph) -> None:
    hops = min_hops(mini, "theme/ai-compute", max_hops=5)
    for company, expected in AI_COMPUTE_MIN_HOPS.items():
        assert hops[company] == expected, company
    assert "company/rumor" not in hops


def test_asml_top_path_is_the_four_edge_chain(mini: Graph) -> None:
    [best, *_] = top_paths(mini, "theme/ai-compute", "company/asml", k=3, max_hops=5)
    assert best.nodes == [
        "theme/ai-compute",
        "product/accelerators",
        "product/leading-edge-logic",
        "product/euv",
        "company/asml",
    ]
    assert len(best.edges) == 4
    assert best.contribution == pytest.approx(0.16, abs=TOL)
    assert best.confidence == pytest.approx(1.0)


def test_paths_are_ordered_by_absolute_contribution(mini: Graph) -> None:
    paths = top_paths(mini, "theme/ai-compute", "company/multi", k=3, max_hops=5)
    assert [round(p.contribution, 9) for p in paths] == [0.1, 0.064]


def test_path_edges_are_derived_ids(mini: Graph) -> None:
    [path] = top_paths(mini, "theme/ai-compute", "company/nvidia", k=3, max_hops=5)
    assert path.edges == [e.id for e in path.edge_records]
    assert path.edge_records[1].rel == "PRODUCES"


def test_path_sum_equals_exposure(mini: Graph) -> None:
    result = exposure(mini, "theme/ai-compute")
    for company in AI_COMPUTE_MIN_HOPS:
        paths = top_paths(mini, "theme/ai-compute", company, k=1000, max_hops=5)
        assert sum(p.contribution for p in paths) == pytest.approx(result[company], abs=TOL)


def test_negative_path_contribution(mini: Graph) -> None:
    [path] = top_paths(mini, "theme/cooling-shift", "company/aircool", k=3, max_hops=5)
    assert path.contribution == pytest.approx(-0.21, abs=TOL)


def test_max_hops_limits_paths(mini: Graph) -> None:
    assert top_paths(mini, "theme/ai-compute", "company/asml", k=3, max_hops=3) == []


def test_weakest_confidence_is_the_minimum_edge(tmp_path: Path) -> None:
    graph = graph_from(MINI, tmp_path, min_confidence=0.0)
    [path] = top_paths(graph, "theme/ai-compute", "company/rumor", k=3, max_hops=5)
    assert path.weakest_confidence == pytest.approx(0.3)
    assert path.confidence == pytest.approx(0.3)
