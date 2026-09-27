from pathlib import Path

from ripple.load import load_seed
from tests.conftest import SeedWriter, base_edges, base_nodes, edge


def test_loads_nodes_and_edges_from_nested_files(write_seed: SeedWriter) -> None:
    seed = load_seed(write_seed(base_nodes(), base_edges()))
    assert len(seed.nodes) == len(base_nodes())
    assert len(seed.edges) == len(base_edges())
    assert seed.problems == []
    assert seed.nodes[0].file.name == "all.yaml"
    assert seed.nodes[0].file.parent.name == "nodes"


def test_ignores_non_yaml_files(write_seed: SeedWriter) -> None:
    directory = write_seed(base_nodes(), base_edges())
    (directory / "TODO.md").write_text("- not yaml: [")
    assert load_seed(directory).problems == []


def test_reports_yaml_syntax_error(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("- id: [unclosed\n")
    problems = load_seed(tmp_path).problems
    assert [p.rule for p in problems] == ["E0"]
    assert problems[0].file.name == "bad.yaml"


def test_reports_file_that_is_not_a_list(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("id: company/x\n")
    assert [p.rule for p in load_seed(tmp_path).problems] == ["E0"]


def test_empty_file_is_fine(tmp_path: Path) -> None:
    (tmp_path / "empty.yaml").write_text("")
    seed = load_seed(tmp_path)
    assert seed.problems == []
    assert seed.nodes == []


def test_rejects_recorded_at_in_yaml(write_seed: SeedWriter) -> None:
    edges = [edge("company/x", "PRODUCES", "product/a", recorded_at="2026-01-01")]
    problems = load_seed(write_seed(base_nodes(), edges)).problems
    assert [p.rule for p in problems] == ["E0"]
    assert "recorded_at" in problems[0].message
    assert problems[0].record == "company/x PRODUCES product/a"


def test_rejects_hand_written_edge_id(write_seed: SeedWriter) -> None:
    edges = [edge("company/x", "PRODUCES", "product/a", id="e-0001")]
    assert [p.rule for p in load_seed(write_seed(base_nodes(), edges)).problems] == ["E0"]


def test_rejects_unknown_relation(write_seed: SeedWriter) -> None:
    edges = [edge("company/x", "MAKES", "product/a")]
    assert [p.rule for p in load_seed(write_seed(base_nodes(), edges)).problems] == ["E0"]
