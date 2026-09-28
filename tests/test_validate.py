from datetime import date
from pathlib import Path

from ripple.load import load_seed
from ripple.validate import Problem, validate
from tests.conftest import Record, SeedWriter, base_edges, base_nodes, edge, node

TODAY = date(2026, 9, 26)


def check(write_seed: SeedWriter, nodes: list[Record], edges: list[Record]) -> list[Problem]:
    return validate(load_seed(write_seed(nodes, edges)), today=TODAY)


def rules(problems: list[Problem]) -> set[str]:
    return {p.rule for p in problems}


def errors(problems: list[Problem]) -> set[str]:
    return {p.rule for p in problems if p.severity == "error"}


def replace_edge(edges: list[Record], src: str, dst: str, **changes: object) -> list[Record]:
    for e in edges:
        if e["src"] == src and e["dst"] == dst:
            e.update(changes)
    return edges


def test_base_graph_is_clean(write_seed: SeedWriter) -> None:
    assert check(write_seed, base_nodes(), base_edges()) == []


def test_schema_problems_from_loader_are_included(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/x", "PRODUCES", "product/b", recorded_at="2026-01-01")]
    assert {"E0"} == errors(check(write_seed, base_nodes(), edges))


def test_problem_names_file_and_record(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", weight=1.5)
    [problem] = [p for p in check(write_seed, base_nodes(), edges) if p.rule == "E4"]
    assert problem.file == Path(write_seed(base_nodes(), edges)) / "edges" / "all.yaml"
    assert problem.record == "company/x PRODUCES product/a"
    assert "E4" in str(problem) and "all.yaml" in str(problem)


# Error rules


def test_e1_duplicate_node_id(write_seed: SeedWriter) -> None:
    nodes = [*base_nodes(), node("product/a")]
    assert {"E1"} == errors(check(write_seed, nodes, base_edges()))


def test_e1_prefix_must_match_type(write_seed: SeedWriter) -> None:
    nodes = [*base_nodes(), node("product/d", type="company")]
    assert {"E1"} == errors(check(write_seed, nodes, base_edges()))


def test_e1_id_must_be_lowercase_slug(write_seed: SeedWriter) -> None:
    nodes = [*base_nodes(), node("product/Bad_Slug")]
    assert {"E1"} == errors(check(write_seed, nodes, base_edges()))


def test_e2_endpoint_must_exist(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/ghost", "PRODUCES", "product/a", 0.1)]
    assert {"E2"} == errors(check(write_seed, base_nodes(), edges))


def test_e3_relation_must_fit_endpoint_types(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("product/b", "PRODUCES", "product/c", 0.1)]
    assert {"E3"} == errors(check(write_seed, base_nodes(), edges))


def test_e3_relations_without_phase_0_node_types(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/x", "EXPOSED_TO", "product/a", 0.1)]
    assert {"E3"} == errors(check(write_seed, base_nodes(), edges))


def test_e4_weight_out_of_range(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", weight=0)
    assert {"E4"} == errors(check(write_seed, base_nodes(), edges))


def test_e4_confidence_out_of_range(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", confidence=1.2)
    assert {"E4"} == errors(check(write_seed, base_nodes(), edges))


def test_e4_polarity_must_be_one_or_minus_one(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", polarity=0)
    assert {"E4"} == errors(check(write_seed, base_nodes(), edges))


def test_e5_evidence_required(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", evidence=[])
    assert {"E5"} == errors(check(write_seed, base_nodes(), edges))


def test_e5_evidence_needs_url_and_accessed(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", evidence=[{"note": "no url"}])
    assert {"E5"} == errors(check(write_seed, base_nodes(), edges))


def test_e6_produces_sum_per_company(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/x", "PRODUCES", "product/b", 0.6)]
    assert {"E6"} == errors(check(write_seed, base_nodes(), edges))


def test_e6_ignores_versions_that_do_not_overlap(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", valid_to=date(2023, 1, 1))
    edges.append(edge("company/x", "PRODUCES", "product/b", 0.6, valid_from=date(2023, 1, 1)))
    assert "E6" not in errors(check(write_seed, base_nodes(), edges))


def test_e7_requires_sum_per_input(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("product/c", "REQUIRES", "product/b", 0.7)]
    assert {"E7"} == errors(check(write_seed, base_nodes(), edges))


def test_e8_duplicate_key_with_overlapping_time(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/z", "PRODUCES", "product/c", 0.05)]
    assert {"E8"} == errors(check(write_seed, base_nodes(), edges))


def test_e8_allows_consecutive_versions(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/z", "product/c", valid_to=date(2023, 1, 1))
    edges.append(edge("company/z", "PRODUCES", "product/c", 0.6, valid_from=date(2023, 1, 1)))
    assert "E8" not in errors(check(write_seed, base_nodes(), edges))


def test_e9_valid_to_after_valid_from(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", valid_to=date(2019, 1, 1))
    assert {"E9"} == errors(check(write_seed, base_nodes(), edges))


def test_e10_volume_theme_drives_same_demand_twice(write_seed: SeedWriter) -> None:
    # theme/vol drives product/a, which REQUIRES product/b: driving b as well double counts.
    edges = [*base_edges(), edge("theme/vol", "DRIVES", "product/b", 0.1)]
    assert {"E10"} == errors(check(write_seed, base_nodes(), edges))


def test_e10_does_not_apply_to_share_shift_themes(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("theme/shift", "DRIVES", "product/b", 0.1)]
    assert "E10" not in errors(check(write_seed, base_nodes(), edges))


def test_e11_share_shift_needs_both_signs(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "theme/shift", "product/c", polarity=1)
    assert {"E11"} == errors(check(write_seed, base_nodes(), edges))


def test_e12_two_relations_on_one_demand_flow_cell(write_seed: SeedWriter) -> None:
    # z SUBSIDIARY_OF y maps z -> y; y SUPPLIES z also maps z -> y.
    edges = [
        *base_edges(),
        edge("company/z", "SUBSIDIARY_OF", "company/y", 0.1),
        edge("company/y", "SUPPLIES", "company/z", 0.1),
    ]
    assert {"E12"} == errors(check(write_seed, base_nodes(), edges))


def test_e13_theme_needs_kind(write_seed: SeedWriter) -> None:
    nodes = [n for n in base_nodes() if n["id"] != "theme/vol"]
    theme = node("theme/vol")
    del theme["kind"]
    assert {"E13"} == errors(check(write_seed, [*nodes, theme], base_edges()))


def test_e13_kind_must_be_known(write_seed: SeedWriter) -> None:
    nodes = [n for n in base_nodes() if n["id"] != "theme/vol"]
    nodes.append(node("theme/vol", kind="supply"))
    assert {"E13"} == errors(check(write_seed, nodes, base_edges()))


def test_e13_kind_only_on_themes(write_seed: SeedWriter) -> None:
    nodes = [*base_nodes(), node("product/d", kind="volume")]
    assert {"E13"} == errors(check(write_seed, nodes, base_edges()))


# Warnings


def test_w1_product_without_producer(write_seed: SeedWriter) -> None:
    edges = [e for e in base_edges() if e["src"] != "company/z"]
    problems = check(write_seed, base_nodes(), edges)
    assert "W1" in rules(problems)
    assert errors(problems) == set()


def test_w2_company_without_produces(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/w", "SUPPLIES", "company/x", 0.2)]
    problems = check(write_seed, [*base_nodes(), node("company/w")], edges)
    assert "W2" in rules(problems)
    assert errors(problems) == set()


def test_w3_unreachable_node(write_seed: SeedWriter) -> None:
    nodes = [*base_nodes(), node("product/orphan"), node("company/o")]
    edges = [*base_edges(), edge("company/o", "PRODUCES", "product/orphan", 0.6)]
    problems = check(write_seed, nodes, edges)
    assert {p.record for p in problems if p.rule == "W3"} == {"product/orphan", "company/o"}


def test_w4_company_reachable_only_below_min_confidence(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/y", "product/b", confidence=0.3)
    problems = check(write_seed, base_nodes(), edges)
    assert [p.record for p in problems if p.rule == "W4"] == ["company/y"]


def test_w5_drives_from_several_themes_sum_above_one(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "theme/shift", "product/a", weight=0.3)
    problems = check(write_seed, base_nodes(), edges)
    assert "W5" in rules(problems)
    assert errors(problems) == set()


def test_w6_evidence_accessed_in_future(write_seed: SeedWriter) -> None:
    future = [{"url": "https://example.com", "note": "n", "accessed": date(2026, 9, 27)}]
    edges = replace_edge(base_edges(), "company/x", "product/a", evidence=future)
    assert "W6" in rules(check(write_seed, base_nodes(), edges))


def test_w7_long_evidence_note(write_seed: SeedWriter) -> None:
    long_note = [{"url": "https://example.com", "note": "x" * 201, "accessed": date(2026, 1, 1)}]
    edges = replace_edge(base_edges(), "company/x", "product/a", evidence=long_note)
    assert "W7" in rules(check(write_seed, base_nodes(), edges))


def test_w8_bucket_weight_must_be_a_bucket_value(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", weight=0.3)
    problems = check(write_seed, base_nodes(), edges)
    assert [p.record for p in problems if p.rule == "W8"] == ["company/x PRODUCES product/a"]
    assert errors(problems) == set()


def test_w8_ignores_filing_weights(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", weight=0.3, weight_source="filing")
    assert "W8" not in rules(check(write_seed, base_nodes(), edges))


def test_w8_accepts_pure_bucket(write_seed: SeedWriter) -> None:
    edges = replace_edge(base_edges(), "company/x", "product/a", weight=0.9)
    assert "W8" not in rules(check(write_seed, base_nodes(), edges))


# Regions (M14)


def test_e3_allows_company_exposed_to_region(write_seed: SeedWriter) -> None:
    nodes = [*base_nodes(), node("region/tw")]
    edges = [*base_edges(), edge("company/x", "EXPOSED_TO", "region/tw", 0.4)]
    problems = check(write_seed, nodes, edges)
    assert errors(problems) == set()
    assert "W3" not in rules(problems)  # regions are not traversed, so not "unreachable"


def test_e3_rejects_product_exposed_to_region(write_seed: SeedWriter) -> None:
    nodes = [*base_nodes(), node("region/tw")]
    edges = [*base_edges(), edge("product/a", "EXPOSED_TO", "region/tw", 0.4)]
    assert {"E3"} == errors(check(write_seed, nodes, edges))


def test_w9_supplies_already_covered_by_the_product_layer(write_seed: SeedWriter) -> None:
    # y makes b, which a requires, and x makes a: demand already flows x -> ... -> y.
    edges = [*base_edges(), edge("company/y", "SUPPLIES", "company/x", 0.25)]
    problems = check(write_seed, base_nodes(), edges)
    assert [p.record for p in problems if p.rule == "W9"] == ["company/y SUPPLIES company/x"]


def test_w9_quiet_when_no_product_path(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/z", "SUPPLIES", "company/x", 0.25)]
    assert "W9" not in rules(check(write_seed, base_nodes(), edges))


def test_w9_quiet_when_already_below_min_confidence(write_seed: SeedWriter) -> None:
    edges = [*base_edges(), edge("company/y", "SUPPLIES", "company/x", 0.25, confidence=0.3)]
    assert "W9" not in rules(check(write_seed, base_nodes(), edges))


# Theme queries (Phase 2, M23)


def test_w10_theme_without_a_query(write_seed: SeedWriter) -> None:
    nodes = [n for n in base_nodes() if n["id"] != "theme/vol"]
    nodes.append(node("theme/vol", query=None))
    problems = check(write_seed, nodes, base_edges())
    assert [p.record for p in problems if p.rule == "W10"] == ["theme/vol"]
    assert errors(problems) == set()


def test_w10_ignores_a_blank_query(write_seed: SeedWriter) -> None:
    nodes = [n for n in base_nodes() if n["id"] != "theme/vol"]
    nodes.append(node("theme/vol", query="   "))
    assert "W10" in rules(check(write_seed, nodes, base_edges()))


def test_w10_silent_when_every_theme_has_a_query(write_seed: SeedWriter) -> None:
    assert "W10" not in rules(check(write_seed, base_nodes(), base_edges()))
