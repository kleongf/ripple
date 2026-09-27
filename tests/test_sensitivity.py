import random
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ripple.graph import Graph, build_graph
from ripple.model import bucket_quantity, bucket_range
from ripple.score import jitter_factors, sensitivity
from ripple.store import Store
from tests.conftest import SeedWriter, base_edges, base_nodes

MINI = Path(__file__).parent / "fixtures" / "mini"


def load_graph(tmp_path: Path, bucket: bool) -> Graph:
    seed = tmp_path / "seed"
    seed.mkdir()
    edges = (MINI / "edges.yaml").read_text()
    if bucket:
        edges = edges.replace("weight_source: manual", "weight_source: bucket")
    (seed / "edges.yaml").write_text(edges)
    (seed / "nodes.yaml").write_text((MINI / "nodes.yaml").read_text())
    with Store(tmp_path / "s.duckdb") as store:
        store.load(seed, now=datetime(2026, 9, 1, tzinfo=UTC))
        return build_graph(store.snapshot(date(2026, 9, 26)))


def test_zero_jitter_is_perfectly_stable(tmp_path: Path) -> None:
    result = sensitivity(
        load_graph(tmp_path, bucket=True), "theme/ai-compute", jitter=0.0, mode="flat"
    )
    assert result.median_spearman_top == pytest.approx(1.0)
    assert result.median_kendall_all == pytest.approx(1.0)
    assert all(shift == 0 for _, shift in result.least_stable)


def test_only_bucket_weights_are_jittered(tmp_path: Path) -> None:
    result = sensitivity(
        load_graph(tmp_path, bucket=False), "theme/ai-compute", jitter=0.9, mode="flat"
    )
    assert result.median_spearman_top == pytest.approx(1.0)
    assert all(shift == 0 for _, shift in result.least_stable)


def test_large_jitter_moves_ranks(tmp_path: Path) -> None:
    result = sensitivity(
        load_graph(tmp_path, bucket=True), "theme/ai-compute", trials=50, jitter=0.9, mode="flat"
    )
    assert result.least_stable[0][1] > 0
    assert result.median_kendall_all < 1.0
    assert len(result.least_stable) == 5


def test_same_seed_same_result(tmp_path: Path) -> None:
    graph = load_graph(tmp_path, bucket=True)
    first = sensitivity(graph, "theme/ai-compute", trials=30, jitter=0.5, seed=7, mode="flat")
    second = sensitivity(graph, "theme/ai-compute", trials=30, jitter=0.5, seed=7, mode="flat")
    assert first == second


def test_top_n_is_capped_at_ranked_companies(tmp_path: Path) -> None:
    result = sensitivity(load_graph(tmp_path, bucket=True), "theme/ai-compute", top_n=10)
    assert result.top_n == 6


def test_jitter_does_not_change_the_original_graph(tmp_path: Path) -> None:
    graph = load_graph(tmp_path, bucket=True)
    before = graph.W.toarray().copy()
    sensitivity(graph, "theme/ai-compute", trials=5, jitter=0.5, mode="flat")
    sensitivity(graph, "theme/ai-compute", trials=5)
    assert (graph.W.toarray() == before).all()


def base_graph(tmp_path: Path, write_seed: SeedWriter) -> Graph:
    with Store(tmp_path / "base.duckdb") as store:
        store.load(write_seed(base_nodes(), base_edges()), now=datetime(2026, 9, 1, tzinfo=UTC))
        return build_graph(store.snapshot(date(2026, 9, 26)))


def test_bucket_mode_draws_within_each_bucket_range(tmp_path: Path, write_seed: SeedWriter) -> None:
    graph = base_graph(tmp_path, write_seed)
    rng = random.Random(1)
    for _ in range(50):
        factors = jitter_factors(graph, rng, mode="bucket", jitter=0.5)
        assert set(factors) == set(graph.edges)
        for cell, factor in factors.items():
            edge = graph.edges[cell]
            quantity = bucket_quantity(edge.rel)
            assert quantity is not None
            span = bucket_range(quantity, edge.weight)
            assert span is not None
            assert span[0] - 1e-12 <= edge.weight * factor <= span[1] + 1e-12
            assert edge.weight * factor <= 1.0


def test_bucket_mode_falls_back_to_flat_for_off_bucket_values(tmp_path: Path) -> None:
    graph = load_graph(tmp_path, bucket=True)  # fixture weights such as 0.9 and 0.3 are not buckets
    factors = jitter_factors(graph, random.Random(2), mode="bucket", jitter=0.5)
    off_bucket = [
        cell
        for cell, e in graph.edges.items()
        if bucket_range(bucket_quantity(e.rel) or "revenue", e.weight) is None
    ]
    assert off_bucket
    assert all(0.5 <= factors[cell] <= 1.5 for cell in off_bucket)


def test_default_mode_is_bucket(tmp_path: Path, write_seed: SeedWriter) -> None:
    result = sensitivity(base_graph(tmp_path, write_seed), "theme/vol", trials=5)
    assert result.mode == "bucket"


def test_reports_top_set_overlap(tmp_path: Path) -> None:
    graph = load_graph(tmp_path, bucket=True)
    stable = sensitivity(graph, "theme/ai-compute", trials=5, jitter=0.0, mode="flat", top_n=3)
    assert stable.median_top_overlap == 3
