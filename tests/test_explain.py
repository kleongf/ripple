"""Paths, edges and company profiles as compact records (Phase 3, M31 and M32)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ripple import explain, profile, signal
from ripple.store import CoverageRow, Store
from tests.conftest import SeedWriter, base_edges, base_nodes, edge
from tests.test_sources import write_dir

T1 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
LATER = date(2026, 6, 1)
MINI = Path(__file__).parent / "fixtures" / "mini"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "ripple.duckdb")


def test_get_evidence_shows_the_layer_that_won_and_the_one_that_lost(
    store: Store, write_seed: SeedWriter, tmp_path: Path
) -> None:
    filing = edge(
        "company/x",
        "PRODUCES",
        "product/a",
        0.55,
        weight_source="filing",
        evidence=[{"url": "https://sec.gov/x", "note": "10-K split", "accessed": date(2026, 1, 1)}],
    )
    xbrl = write_dir(tmp_path / "xbrl", [], [filing])
    store.load_sources({"seed": write_seed(base_nodes(), base_edges()), "xbrl": xbrl}, now=T1)
    snap = store.snapshot(LATER)
    [winner] = [e for e in snap.edges if e.key == ("company/x", "PRODUCES", "product/a")]

    data = explain.get_evidence(store, winner.id, LATER)
    assert data["source"] == "xbrl"
    assert data["weight"] == 0.55 and data["weight_source"] == "filing"
    assert data["evidence"][0]["url"] == "https://sec.gov/x"
    [lost] = data["alternatives"]
    assert lost["source"] == "seed" and lost["weight_source"] == "bucket"
    assert lost["evidence"][0]["url"] == "https://example.com/source"


def test_get_evidence_on_an_edge_not_valid_yet(store: Store, write_seed: SeedWriter) -> None:
    store.load_sources({"seed": write_seed(base_nodes(), base_edges())}, now=T1)
    snap = store.snapshot(LATER)
    edge_id = snap.edges[0].id
    with pytest.raises(ValueError, match="no edge"):
        explain.get_evidence(store, edge_id, date(2019, 1, 1))


def test_path_record_counts_guessed_weights(store: Store, write_seed: SeedWriter) -> None:
    store.load_sources({"seed": write_seed(base_nodes(), base_edges())}, now=T1)
    data = explain.explain_link(store, "theme/vol", "company/y", LATER)
    (path,) = data["paths"]
    # theme/vol DRIVES a (0.8) -> a REQUIRES b (0.4) -> y PRODUCES b (0.6), all buckets.
    assert path["contribution"] == pytest.approx(0.192)
    assert path["guessed_weights"] == 3
    assert all(e["weight_source"] == "bucket" for e in path["edges"])


def test_explain_link_unknown_node(store: Store, write_seed: SeedWriter) -> None:
    store.load_sources({"seed": write_seed(base_nodes(), base_edges())}, now=T1)
    with pytest.raises(ValueError, match="unknown node"):
        explain.explain_link(store, "theme/vol", "company/ghost", LATER)


def test_profile_of_a_company_with_no_products(store: Store) -> None:
    store.load(MINI, now=T1)
    data = profile.company_profile(store, "company/parts", LATER)
    assert data["revenue_mix"] == []
    assert data["mapped_share"] == 0
    assert [(c["customer"], c["weight"]) for c in data["customers"]] == [("company/nvidia", 0.5)]
    # Parts reaches AI compute only through Nvidia: 1.0 x 0.9 x 0.5 = 0.45, ranked 2nd of 6.
    [theme] = data["themes"]
    assert (theme["exposure"], theme["rank"], theme["of"]) == (0.45, 2, 6)


def test_profile_lists_suppliers_from_the_customer_side(store: Store) -> None:
    store.load(MINI, now=T1)
    data = profile.company_profile(store, "company/nvidia", LATER)
    assert [s["supplier"] for s in data["suppliers"]] == ["company/parts"]


def burst_rows(key: str, start: date, days: int, burst_day: date) -> list[CoverageRow]:
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        spike = burst_day <= day < burst_day + timedelta(days=4)
        rows.append(CoverageRow("theme", key, day, 600 if spike else 60, 500_000, -1.0, "q1"))
    return rows


def test_trending_themes_reports_a_burst_with_its_drives_edges(store: Store) -> None:
    store.load(MINI, now=T1)
    as_of = date(2026, 5, 1)
    store.add_coverage(
        burst_rows("theme/cooling-shift", date(2025, 12, 1), 150, date(2026, 4, 20)), now=T1
    )
    data = signal.trending_themes(store, as_of, window=30)
    [burst] = data["bursts"]
    assert burst["theme"] == "theme/cooling-shift"
    assert burst["kind"] == "share_shift"
    assert burst["start"] == "2026-04-20"
    assert burst["query_hash"] == "q1"
    # Winners first: liquid cooling gains, air cooling loses.
    assert [(d["product"], d["polarity"]) for d in burst["drives"]] == [
        ("product/liquid-cooling", 1),
        ("product/air-cooling", -1),
    ]
    assert data["unmeasured"] == ["theme/ai-compute"]


def test_trending_themes_is_point_in_time(store: Store) -> None:
    """Coverage recorded after as_of is not visible, the same rule the graph follows."""
    store.load(MINI, now=T1)
    recorded = datetime(2026, 5, 10, tzinfo=UTC)
    store.add_coverage(
        burst_rows("theme/cooling-shift", date(2025, 12, 1), 150, date(2026, 4, 20)),
        now=recorded,
    )
    assert signal.trending_themes(store, date(2026, 5, 1), window=30)["bursts"] == []
    assert signal.trending_themes(store, date(2026, 5, 10), window=30)["bursts"] != []
