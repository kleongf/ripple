from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from ripple.store import SeedInvalidError, Store
from tests.conftest import SeedWriter, base_edges, base_nodes, edge

T1 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "ripple.duckdb")


def weight_of(
    store: Store, as_of: date, src: str, dst: str, known_at: date | None = None
) -> float | None:
    for e in store.snapshot(as_of, known_at).edges:
        if e.src == src and e.dst == dst:
            return e.weight
    return None


def test_snapshot_returns_loaded_nodes_and_edges(store: Store, write_seed: SeedWriter) -> None:
    store.load(write_seed(base_nodes(), base_edges()), now=T1)
    snap = store.snapshot(date(2026, 2, 1))
    assert set(snap.nodes) == {n["id"] for n in base_nodes()}
    assert len(snap.edges) == len(base_edges())
    assert snap.as_of == date(2026, 2, 1)


def test_snapshot_round_trips_edge_fields(store: Store, write_seed: SeedWriter) -> None:
    store.load(write_seed(base_nodes(), base_edges()), now=T1)
    [e] = [
        e
        for e in store.snapshot(date(2026, 2, 1)).edges
        if e.src == "theme/shift" and e.dst == "product/c"
    ]
    assert e.polarity == -1
    assert e.weight_source == "bucket"
    assert e.evidence[0].url == "https://example.com/source"
    assert e.id.startswith("e-")


def test_future_valid_from_is_excluded(store: Store, write_seed: SeedWriter) -> None:
    edges = [
        *base_edges(),
        edge("company/x", "PRODUCES", "product/b", 0.1, valid_from=date(2027, 1, 1)),
    ]
    store.load(write_seed(base_nodes(), edges), now=T1)
    assert weight_of(store, date(2026, 2, 1), "company/x", "product/b") is None
    assert weight_of(store, date(2027, 1, 1), "company/x", "product/b") == 0.1


def test_expired_valid_to_is_excluded(store: Store, write_seed: SeedWriter) -> None:
    edges = [
        *base_edges(),
        edge("company/x", "PRODUCES", "product/b", 0.1, valid_to=date(2025, 1, 1)),
    ]
    store.load(write_seed(base_nodes(), edges), now=T1)
    now = date(2026, 2, 1)
    assert weight_of(store, date(2024, 12, 31), "company/x", "product/b", known_at=now) == 0.1
    assert weight_of(store, now, "company/x", "product/b") is None
    # Without known_at, 2024 is seen with 2024 knowledge: nothing had been loaded yet.
    assert weight_of(store, date(2024, 12, 31), "company/x", "product/b") is None


def test_edge_recorded_after_as_of_is_excluded(store: Store, write_seed: SeedWriter) -> None:
    store.load(write_seed(base_nodes(), base_edges()), now=T1)
    assert store.snapshot(date(2026, 1, 9)).edges == []
    assert store.snapshot(date(2026, 1, 9)).nodes == {}
    # as_of means the end of that UTC day, so a load at noon is visible the same day.
    assert len(store.snapshot(date(2026, 1, 10)).edges) == len(base_edges())


def test_changed_weight_is_superseded(store: Store, write_seed: SeedWriter) -> None:
    store.load(write_seed(base_nodes(), base_edges()), now=T1)
    edges = base_edges()
    edges[0]["weight"] = 0.4
    report = store.load(write_seed(base_nodes(), edges), now=T2)
    assert (report.inserted, report.superseded) == (1, 1)
    assert weight_of(store, date(2026, 3, 9), "theme/vol", "product/a") == 0.8
    assert weight_of(store, date(2026, 3, 10), "theme/vol", "product/a") == 0.4


def test_removed_edge_is_closed_not_deleted(store: Store, write_seed: SeedWriter) -> None:
    store.load(write_seed(base_nodes(), base_edges()), now=T1)
    edges = [e for e in base_edges() if e["src"] != "company/z"]
    report = store.load(write_seed(base_nodes(), edges), now=T2)
    assert (report.inserted, report.superseded) == (0, 1)
    assert weight_of(store, date(2026, 3, 9), "company/z", "product/c") == 0.25
    assert weight_of(store, date(2026, 3, 10), "company/z", "product/c") is None


def test_changed_node_is_versioned(store: Store, write_seed: SeedWriter) -> None:
    store.load(write_seed(base_nodes(), base_edges()), now=T1)
    nodes = base_nodes()
    nodes[5]["label"] = "Renamed"
    store.load(write_seed(nodes, base_edges()), now=T2)
    assert store.snapshot(date(2026, 3, 9)).nodes["company/x"].label == "x"
    assert store.snapshot(date(2026, 3, 10)).nodes["company/x"].label == "Renamed"


def test_unchanged_reload_inserts_nothing(store: Store, write_seed: SeedWriter) -> None:
    directory = write_seed(base_nodes(), base_edges())
    store.load(directory, now=T1)
    before = store.row_counts()
    report = store.load(directory, now=T2)
    assert (report.inserted, report.superseded) == (0, 0)
    assert store.row_counts() == before


def test_invalid_seed_is_refused(store: Store, write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/ghost", "PRODUCES", "product/a")]
    with pytest.raises(SeedInvalidError) as info:
        store.load(write_seed(base_nodes(), edges), now=T1)
    assert any(p.rule == "E2" for p in info.value.problems)
    assert store.row_counts() == {"nodes": 0, "edges": 0, "evidence": 0}


def test_store_persists_across_connections(tmp_path: Path, write_seed: SeedWriter) -> None:
    path = tmp_path / "ripple.duckdb"
    with Store(path) as first:
        first.load(write_seed(base_nodes(), base_edges()), now=T1)
    with Store(path) as second:
        assert len(second.snapshot(date(2026, 2, 1)).edges) == len(base_edges())


def test_read_only_store_reads_snapshots(tmp_path: Path, write_seed: SeedWriter) -> None:
    path = tmp_path / "ripple.duckdb"
    with Store(path) as writer:
        writer.load(write_seed(base_nodes(), base_edges()), now=T1)
    with Store(path, read_only=True) as reader:
        assert len(reader.snapshot(date(2026, 2, 1)).edges) == len(base_edges())


def test_read_only_store_requires_existing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Store(tmp_path / "missing.duckdb", read_only=True)


def test_evidence_verified_flag_round_trips(store: Store, write_seed: SeedWriter) -> None:
    edges = base_edges()
    edges[0]["evidence"] = [
        {
            "url": "https://example.com/a",
            "note": "checked",
            "accessed": date(2026, 1, 1),
            "verified": True,
        }
    ]
    store.load(write_seed(base_nodes(), edges), now=T1)
    by_src = {(e.src, e.dst): e for e in store.snapshot(date(2026, 2, 1)).edges}
    assert by_src[("theme/vol", "product/a")].evidence[0].verified is True
    assert by_src[("company/x", "product/a")].evidence[0].verified is False


def test_store_adds_verified_column_to_old_database(tmp_path: Path, write_seed: SeedWriter) -> None:
    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE evidence (edge_row_id BIGINT NOT NULL, position INTEGER NOT NULL, "
        "url VARCHAR, note VARCHAR, accessed DATE)"
    )
    con.close()
    with Store(path) as store:
        store.load(write_seed(base_nodes(), base_edges()), now=T1)
        assert store.snapshot(date(2026, 2, 1)).edges[0].evidence[0].verified is False


def test_evidence_span_round_trips(store: Store, write_seed: SeedWriter) -> None:
    edges = base_edges()
    edges[0]["evidence"] = [
        {
            "url": "https://example.com/10k.htm",
            "note": "own words",
            "accessed": date(2026, 1, 1),
            "span": {"section": "1", "start": 100, "end": 180},
        }
    ]
    store.load(write_seed(base_nodes(), edges), now=T1)
    by_src = {(e.src, e.dst): e for e in store.snapshot(date(2026, 2, 1)).edges}
    span = by_src[("theme/vol", "product/a")].evidence[0].span
    assert span is not None and (span.section, span.start, span.end) == ("1", 100, 180)
    assert by_src[("company/x", "product/a")].evidence[0].span is None


def test_adding_span_field_keeps_old_content_hashes(tmp_path: Path) -> None:
    from ripple.model import Edge
    from ripple.store import edge_hash

    edge = Edge(**base_edges()[0])
    old_style = edge.model_dump(mode="json")
    for item in old_style["evidence"]:
        item.pop("span")
    import hashlib
    import json

    expected = hashlib.sha256(json.dumps(old_style, sort_keys=True).encode()).hexdigest()
    assert edge_hash(edge) == expected
