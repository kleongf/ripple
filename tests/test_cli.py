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
