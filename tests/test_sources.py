"""Several sources in one store: scoped loading and consolidation by precedence (M12, D40)."""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from typer.testing import CliRunner

from ripple.store import SeedInvalidError, Store
from tests.conftest import SeedWriter, base_edges, base_nodes, edge, node

T1 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
LATER = date(2026, 6, 1)
KEY = ("company/x", "PRODUCES", "product/a")


def write_dir(path: Path, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "nodes.yaml").write_text(yaml.safe_dump(nodes, sort_keys=False))
    (path / "edges.yaml").write_text(yaml.safe_dump(edges, sort_keys=False))
    return path


def verified(url: str = "https://example.com/checked") -> list[dict[str, Any]]:
    return [{"url": url, "note": "checked", "accessed": date(2026, 1, 1), "verified": True}]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "ripple.duckdb")


def winner(store: Store, key: tuple[str, str, str] = KEY):
    snap = store.snapshot(LATER)
    [found] = [e for e in snap.edges if e.key == key]
    return snap, found


def test_xbrl_beats_seed(store: Store, write_seed: SeedWriter, tmp_path: Path) -> None:
    xbrl = write_dir(tmp_path / "xbrl", [], [edge(*KEY, 0.42, weight_source="filing")])
    store.load_sources({"seed": write_seed(base_nodes(), base_edges()), "xbrl": xbrl}, now=T1)
    snap, found = winner(store)
    assert found.weight == 0.42
    assert snap.sources[found.id] == "xbrl"
    assert [(s, e.weight) for s, e in snap.alternatives[found.id]] == [("seed", 0.6)]


def test_verified_seed_beats_xbrl(store: Store, write_seed: SeedWriter, tmp_path: Path) -> None:
    edges = base_edges()
    edges[4]["evidence"] = verified()  # company/x PRODUCES product/a
    xbrl = write_dir(tmp_path / "xbrl", [], [edge(*KEY, 0.42, weight_source="filing")])
    store.load_sources({"seed": write_seed(base_nodes(), edges), "xbrl": xbrl}, now=T1)
    snap, found = winner(store)
    assert (found.weight, snap.sources[found.id]) == (0.6, "seed")


def test_review_beats_xbrl(store: Store, write_seed: SeedWriter, tmp_path: Path) -> None:
    sources = {
        "seed": write_seed(base_nodes(), base_edges()),
        "xbrl": write_dir(tmp_path / "xbrl", [], [edge(*KEY, 0.42, weight_source="filing")]),
        "review": write_dir(tmp_path / "review", [], [edge(*KEY, 0.5, weight_source="manual")]),
    }
    store.load_sources(sources, now=T1)
    snap, found = winner(store)
    assert (found.weight, snap.sources[found.id]) == (0.5, "review")
    assert [s for s, _ in snap.alternatives[found.id]] == ["xbrl", "seed"]


def test_llm_loses_to_seed(store: Store, write_seed: SeedWriter, tmp_path: Path) -> None:
    llm = write_dir(tmp_path / "llm", [], [edge(*KEY, 0.25)])
    store.load_sources({"seed": write_seed(base_nodes(), base_edges()), "llm": llm}, now=T1)
    snap, found = winner(store)
    assert (found.weight, snap.sources[found.id]) == (0.6, "seed")


def test_edge_only_in_one_source_has_no_alternatives(
    store: Store, write_seed: SeedWriter, tmp_path: Path
) -> None:
    store.load_sources({"seed": write_seed(base_nodes(), base_edges())}, now=T1)
    snap, found = winner(store)
    assert snap.sources[found.id] == "seed"
    assert found.id not in snap.alternatives


def test_reloading_one_source_leaves_the_others(
    store: Store, write_seed: SeedWriter, tmp_path: Path
) -> None:
    xbrl = write_dir(tmp_path / "xbrl", [], [edge(*KEY, 0.42, weight_source="filing")])
    store.load_sources({"seed": write_seed(base_nodes(), base_edges()), "xbrl": xbrl}, now=T1)
    # The new seed no longer has the edge; the xbrl row must survive the seed reload.
    seed_edges = [e for e in base_edges() if (e["src"], e["rel"], e["dst"]) != KEY]
    report = store.load(write_seed(base_nodes(), seed_edges), now=T2, source="seed")
    assert report.superseded == 1
    snap, found = winner(store)
    assert (found.weight, snap.sources[found.id]) == (0.42, "xbrl")
    assert found.id not in snap.alternatives


def test_sources_are_validated_together(
    store: Store, write_seed: SeedWriter, tmp_path: Path
) -> None:
    # The xbrl edge points at seed nodes: valid only when both are validated together.
    xbrl = write_dir(tmp_path / "xbrl", [], [edge("company/y", "PRODUCES", "product/a", 0.25)])
    store.load_sources({"seed": write_seed(base_nodes(), base_edges()), "xbrl": xbrl}, now=T1)
    with pytest.raises(SeedInvalidError):
        Store(tmp_path / "other.duckdb").load(xbrl, now=T1, source="xbrl")


def test_sums_use_consolidated_edges(store: Store, write_seed: SeedWriter, tmp_path: Path) -> None:
    # seed x->a 0.6 is replaced by xbrl x->a 0.3, so x->b 0.6 keeps x at 0.9 (fine).
    ok = write_dir(
        tmp_path / "ok",
        [],
        [
            edge(*KEY, 0.3, weight_source="filing"),
            edge("company/x", "PRODUCES", "product/b", 0.6, weight_source="filing"),
        ],
    )
    store.load_sources({"seed": write_seed(base_nodes(), base_edges()), "xbrl": ok}, now=T1)
    # With x->b at 0.8 the consolidated sum is 1.1: rule E6.
    bad = write_dir(
        tmp_path / "bad",
        [],
        [
            edge(*KEY, 0.3, weight_source="filing"),
            edge("company/x", "PRODUCES", "product/b", 0.8, weight_source="filing"),
        ],
    )
    with pytest.raises(SeedInvalidError) as info:
        store.load_sources({"seed": write_seed(base_nodes(), base_edges()), "xbrl": bad}, now=T2)
    assert {p.rule for p in info.value.problems} == {"E6"}


def test_node_can_come_from_another_source(
    store: Store, write_seed: SeedWriter, tmp_path: Path
) -> None:
    xbrl = write_dir(
        tmp_path / "xbrl",
        [node("company/new")],
        [edge("company/new", "PRODUCES", "product/c", 0.25, weight_source="filing")],
    )
    store.load_sources({"seed": write_seed(base_nodes(), base_edges()), "xbrl": xbrl}, now=T1)
    assert "company/new" in store.snapshot(LATER).nodes


def test_seed_label_wins_for_a_node_in_two_sources(
    store: Store, write_seed: SeedWriter, tmp_path: Path
) -> None:
    xbrl = write_dir(tmp_path / "xbrl", [node("company/x", label="X CORP (from filing)")], [])
    store.load_sources({"seed": write_seed(base_nodes(), base_edges()), "xbrl": xbrl}, now=T1)
    assert store.snapshot(LATER).nodes["company/x"].label == "x"


def test_old_database_gets_a_source_column(tmp_path: Path, write_seed: SeedWriter) -> None:
    path = tmp_path / "old.duckdb"
    with Store(path) as old:
        old.load(write_seed(base_nodes(), base_edges()), now=T1)
    con = duckdb.connect(str(path))
    con.execute("ALTER TABLE edges DROP COLUMN source")
    con.execute("ALTER TABLE nodes DROP COLUMN source")
    con.close()
    with Store(path) as reopened:
        snap = reopened.snapshot(LATER)
        assert set(snap.sources.values()) == {"seed"}
        report = reopened.load(write_seed(base_nodes(), base_edges()), now=T2)
        assert (report.inserted, report.superseded) == (0, 0)


# CLI


runner = CliRunner()


def test_load_command_takes_a_source(tmp_path: Path, write_seed: SeedWriter) -> None:
    from ripple.cli import app

    db = tmp_path / "cli.duckdb"
    seed = write_seed(base_nodes(), base_edges())
    assert runner.invoke(app, ["load", str(seed), "--db", str(db)]).exit_code == 0
    xbrl = write_dir(tmp_path / "xbrl", [], [edge(*KEY, 0.42, weight_source="filing")])
    # Loading xbrl alone fails validation (its edge needs seed nodes)...
    alone = runner.invoke(app, ["load", str(xbrl), "--source", "xbrl", "--db", str(db)])
    assert alone.exit_code == 1
    # ...so the default is to load every source directory together.
    sources = tmp_path / "sources.yaml"
    sources.write_text(yaml.safe_dump({"seed": str(seed), "xbrl": str(xbrl)}))
    both = runner.invoke(app, ["load", "--sources", str(sources), "--db", str(db)])
    assert both.exit_code == 0, both.output
    assert "xbrl" in both.output


def test_explain_shows_source_and_alternatives(tmp_path: Path, write_seed: SeedWriter) -> None:
    from ripple.cli import app

    db = tmp_path / "cli.duckdb"
    xbrl = write_dir(tmp_path / "xbrl", [], [edge(*KEY, 0.42, weight_source="filing")])
    with Store(db) as s:
        s.load_sources(
            {"seed": write_seed(base_nodes(), base_edges()), "xbrl": xbrl},
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
    result = runner.invoke(app, ["explain", "theme/vol", "company/x", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "source xbrl" in result.output
    assert "also from seed: weight 0.6 (bucket)" in result.output


def test_newer_fact_beats_an_older_filing(
    store: Store, write_seed: SeedWriter, tmp_path: Path
) -> None:
    # Dell: the FY2026 filing (from 2025-02) must not override the seed's FY2027 version.
    edges = base_edges()
    edges[4]["valid_from"] = date(2026, 2, 1)  # company/x PRODUCES product/a, newer
    edges[4]["weight_source"] = "filing"  # a sourced number, like Dell's FY2027 run-rate
    xbrl = write_dir(
        tmp_path / "xbrl",
        [],
        [edge(*KEY, 0.42, weight_source="filing", valid_from=date(2025, 2, 1))],
    )
    store.load_sources({"seed": write_seed(base_nodes(), edges), "xbrl": xbrl}, now=T1)
    snap, found = winner(store)
    assert (found.weight, snap.sources[found.id]) == (0.6, "seed")
    # Before the seed version starts, only the filing is valid.
    older = store.snapshot(date(2025, 6, 1), known_at=LATER)
    [found] = [e for e in older.edges if e.key == KEY]
    assert found.weight == 0.42


def test_a_bucket_never_beats_a_sourced_number(
    store: Store, write_seed: SeedWriter, tmp_path: Path
) -> None:
    # AMD: the seed guess (bucket, dated 2025-01-01) is "newer" than the FY2025 filing
    # (2024-12-29) only because hand-written dates are approximate.
    edges = base_edges()
    edges[4]["valid_from"] = date(2026, 2, 1)
    xbrl = write_dir(
        tmp_path / "xbrl",
        [],
        [edge(*KEY, 0.42, weight_source="manual", valid_from=date(2025, 2, 1))],
    )
    store.load_sources({"seed": write_seed(base_nodes(), edges), "xbrl": xbrl}, now=T1)
    snap, found = winner(store)
    assert (found.weight, snap.sources[found.id]) == (0.42, "xbrl")


def test_facts_within_90_days_count_as_the_same_period(
    store: Store, write_seed: SeedWriter, tmp_path: Path
) -> None:
    # Nvidia: seed 2025-02-01 vs FY2026 filing 2025-01-27 is the same fiscal year, so the
    # source order decides.
    edges = base_edges()
    edges[4]["valid_from"] = date(2025, 2, 1)
    edges[4]["weight_source"] = "filing"
    xbrl = write_dir(
        tmp_path / "xbrl",
        [],
        [edge(*KEY, 0.42, weight_source="manual", valid_from=date(2025, 1, 27))],
    )
    store.load_sources({"seed": write_seed(base_nodes(), edges), "xbrl": xbrl}, now=T1)
    snap, found = winner(store)
    assert (found.weight, snap.sources[found.id]) == (0.42, "xbrl")
