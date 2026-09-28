import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ripple.cli import app
from tests.conftest import SeedWriter, base_edges, base_nodes, edge

runner = CliRunner()


def test_help_runs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "exposure to a theme shock" in result.output


def test_validate_clean_seed_exits_zero(write_seed: SeedWriter) -> None:
    result = runner.invoke(app, ["validate", str(write_seed(base_nodes(), base_edges()))])
    assert result.exit_code == 0, result.output
    assert "0 errors, 0 warnings" in result.output


def test_validate_reports_errors_and_exits_one(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/ghost", "PRODUCES", "product/a", 0.1)]
    result = runner.invoke(app, ["validate", str(write_seed(base_nodes(), edges))])
    assert result.exit_code == 1
    assert "E2" in result.output
    assert "company/ghost" in result.output


def test_validate_warnings_do_not_fail(write_seed: SeedWriter) -> None:
    edges = [e for e in base_edges() if e["src"] != "company/z"]
    result = runner.invoke(app, ["validate", str(write_seed(base_nodes(), edges))])
    assert result.exit_code == 0, result.output
    assert "W1" in result.output


MINI = Path(__file__).parent / "fixtures" / "mini"


@pytest.fixture
def loaded_db(tmp_path: Path) -> Path:
    db = tmp_path / "cli.duckdb"
    result = runner.invoke(app, ["load", str(MINI), "--db", str(db)])
    assert result.exit_code == 0, result.output
    return db


def test_load_reports_counts(tmp_path: Path) -> None:
    db = tmp_path / "cli.duckdb"
    first = runner.invoke(app, ["load", str(MINI), "--db", str(db)])
    assert "inserted 32" in first.output
    second = runner.invoke(app, ["load", str(MINI), "--db", str(db)])
    assert "inserted 0, superseded 0, unchanged 32" in second.output


def test_load_refuses_invalid_seed(tmp_path: Path, write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/ghost", "PRODUCES", "product/a", 0.1)]
    db = tmp_path / "cli.duckdb"
    result = runner.invoke(app, ["load", str(write_seed(base_nodes(), edges)), "--db", str(db)])
    assert result.exit_code == 1
    assert "E2" in result.output


def test_exposed_json(loaded_db: Path) -> None:
    result = runner.invoke(app, ["exposed", "theme/ai-compute", "--db", str(loaded_db), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [r["company"] for r in data["results"]][:2] == ["company/nvidia", "company/parts"]


def test_exposed_table_hide_obvious(loaded_db: Path) -> None:
    result = runner.invoke(
        app,
        [
            "exposed",
            "theme/ai-compute",
            "--db",
            str(loaded_db),
            "--hide-obvious",
            "--obvious-hops",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Parts" in result.output
    assert "NVDA" not in result.output  # Nvidia still appears inside Parts' path


def test_exposed_unknown_theme(loaded_db: Path) -> None:
    result = runner.invoke(app, ["exposed", "theme/nope", "--db", str(loaded_db)])
    assert result.exit_code == 1
    assert "theme/nope" in result.output


def test_explain_prints_path_and_evidence(loaded_db: Path) -> None:
    result = runner.invoke(
        app, ["explain", "theme/ai-compute", "company/asml", "--db", str(loaded_db)]
    )
    assert result.exit_code == 0, result.output
    assert (
        "AI compute ↑ → Accelerators (1.0) → Leading-edge logic (0.4) → EUV (0.8) "
        "→ ASML Holding (rev 0.5)" in result.output
    )
    assert "+0.1600" in result.output
    assert "https://example.com/fixture" in result.output


def test_sensitivity_command(loaded_db: Path) -> None:
    result = runner.invoke(
        app, ["sensitivity", "theme/ai-compute", "--trials", "10", "--db", str(loaded_db)]
    )
    assert result.exit_code == 0, result.output
    assert "median top-6 Spearman" in result.output
    assert "median Kendall tau" in result.output
    assert "top-6 set overlap" in result.output
    assert "bucket ranges" in result.output


def test_validate_counts_unverified_evidence(write_seed: SeedWriter) -> None:
    result = runner.invoke(app, ["validate", str(write_seed(base_nodes(), base_edges()))])
    assert f"0 of {len(base_edges())} evidence items verified" in result.output


def test_signals_fetch_skips_series_held_with_the_current_query(
    loaded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date

    from ripple import news
    from ripple.model import today_utc
    from ripple.store import CoverageRow, Store

    calls: list[str] = []

    def fake_volume(self: news.NewsClient, query: str, start: date, end: date) -> list:
        calls.append(query)
        return [news.VolumePoint(day=end, matched=3, norm=1000)]

    monkeypatch.setattr(news.NewsClient, "volume", fake_volume)
    monkeypatch.setattr(news.NewsClient, "tone", lambda self, q, s, e: {})
    with Store(loaded_db) as store:
        nodes = store.snapshot(today_utc()).nodes
        held = news.company_query(nodes["company/asml"])
        store.add_coverage(
            [
                CoverageRow(
                    "company", "company/asml", date(2026, 9, 1), 1, 10, None, news.query_hash(held)
                )
            ]
        )
    args = [
        "signals",
        "fetch",
        "--company",
        "company/asml",
        "--company",
        "company/tsmc",
        "--to",
        "2026-09-01",
        "--db",
        str(loaded_db),
        "--cache",
        str(tmp_path / "c"),
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "1 series already held" in result.output
    assert len(calls) == 1 and held not in calls
    with Store(loaded_db, read_only=True) as store:
        assert [r.matched for r in store.coverage("company/tsmc")] == [3]


def test_evidence_prints_the_edge_and_its_sources(loaded_db: Path) -> None:
    from ripple.model import Edge

    edge_id = Edge(
        src="company/asml",
        rel="PRODUCES",
        dst="product/euv",
        weight=0.5,
        weight_source="manual",
        polarity=1,
        confidence=1.0,
        valid_from="2020-01-01",
    ).id
    result = runner.invoke(app, ["evidence", edge_id, "--db", str(loaded_db)])
    assert result.exit_code == 0, result.output
    assert "ASML Holding PRODUCES EUV" in result.output
    assert "unverified" in result.output
    as_json = runner.invoke(app, ["evidence", edge_id, "--json", "--db", str(loaded_db)])
    assert json.loads(as_json.output)["edge"] == edge_id


def test_evidence_unknown_edge_fails(loaded_db: Path) -> None:
    result = runner.invoke(app, ["evidence", "e-0000000000", "--db", str(loaded_db)])
    assert result.exit_code == 1


def test_profile_lists_themes_and_warns_against_summing(loaded_db: Path) -> None:
    result = runner.invoke(app, ["profile", "company/vertiv", "--db", str(loaded_db)])
    assert result.exit_code == 0, result.output
    assert "AI compute" in result.output and "Cooling shift" in result.output
    assert "never add them across themes" in result.output
    assert "attention: unknown" in result.output


def test_explain_json_matches_the_tool(loaded_db: Path) -> None:
    args = ["explain", "theme/ai-compute", "company/asml", "--json", "--db", str(loaded_db)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["paths"][0]["contribution"] == 0.16


def test_coverage_append_retries_while_the_store_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date

    import duckdb

    from ripple import cli
    from ripple.store import CoverageRow, Store

    db = tmp_path / "locked.duckdb"
    row = CoverageRow("theme", "theme/t", date(2026, 1, 1), 1, 10, None, "h")
    attempts = {"n": 0}

    def store_locked_once(path: Path, read_only: bool = False) -> Store:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise duckdb.IOException("Could not set lock on file")
        return Store(path, read_only=read_only)

    monkeypatch.setattr(cli, "Store", store_locked_once)
    waits: list[float] = []
    assert cli._append_coverage(db, [row], sleep=waits.append) == 1
    assert waits == [cli.LOCK_WAIT]
    with Store(db, read_only=True) as store:
        assert len(store.coverage("theme/t")) == 1
