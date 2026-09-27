"""Revenue lines to product nodes, and the review queue (M13). Codex is replaced by a fake."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ripple.codex import CodexResult
from ripple.load import load_seed
from ripple.mapping import (
    CompanyMapping,
    accept_mapping,
    choose_breakdown,
    edges_from_mapping,
    load_mapping,
    map_company,
    mapping_prompt,
    mapping_schema,
    save_mapping,
)
from ripple.model import Node
from ripple.xbrl import Breakdown, RevenueFacts, RevenueLine
from tests.test_xbrl import cache_fixture

TAXONOMY = [
    Node(
        id="product/inspection-metrology",
        type="product",
        label="Inspection and metrology",
        aliases=["process control"],
    ),
    Node(id="product/euv-lithography", type="product", label="EUV lithography systems"),
    Node(id="material/abf-film", type="material", label="ABF film"),
]


def line(member: str, label: str, share: float) -> RevenueLine:
    return RevenueLine(member, label, share * 1000, share)


def facts(product_complete: bool = True, segment: bool = True) -> RevenueFacts:
    product = Breakdown(
        "product",
        "srt:ProductOrServiceAxis",
        [
            line("ex:WaferMember", "Wafer Inspection", 0.5),
            line("ex:PattMember", "Patterning", 0.3),
            line("us-gaap:ServiceMember", "Service", 0.2),
        ],
        complete=product_complete,
    )
    breakdowns = {"product": product}
    if segment:
        breakdowns["segment"] = Breakdown(
            "segment",
            "us-gaap:StatementBusinessSegmentsAxis",
            [line("ex:PcMember", "Process Control", 1.0)],
            complete=True,
        )
    return RevenueFacts(
        "us-gaap:Revenues", date(2025, 7, 1), date(2026, 6, 30), 1000.0, "USD", breakdowns, []
    )


class FakeRunner:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, prompt: str, schema: dict[str, Any], instructions: str) -> CodexResult:
        self.calls.append((prompt, schema))
        return CodexResult(self.data, {"input_tokens": 7000, "output_tokens": 300})


ANSWER = {
    "lines": [
        {
            "member": "ex:WaferMember",
            "assignments": [{"product": "product/inspection-metrology", "fraction": 1.0}],
            "new_product": None,
            "note": "Wafer inspection is process control.",
        },
        {
            "member": "ex:PattMember",
            "assignments": [{"product": "product/inspection-metrology", "fraction": 0.8}],
            "new_product": "Reticle inspection",
            "note": "Mostly metrology.",
        },
        {
            "member": "us-gaap:ServiceMember",
            "assignments": [],
            "new_product": None,
            "note": "Service follows the installed base.",
        },
    ]
}


def make_mapping(answer: dict[str, Any] = ANSWER) -> tuple[CompanyMapping, FakeRunner]:
    runner = FakeRunner(answer)
    mapping = map_company(
        node="company/kla",
        label="KLA",
        facts=facts(),
        taxonomy=TAXONOMY,
        runner=runner,
        filing_url="https://www.sec.gov/kla-10k.htm",
        form="10-K",
    )
    return mapping, runner


# Choosing a breakdown


def test_complete_product_breakdown_is_preferred() -> None:
    chosen = choose_breakdown(facts())
    assert chosen is not None and chosen.kind == "product"


def test_complete_segment_beats_partial_product() -> None:
    chosen = choose_breakdown(facts(product_complete=False))
    assert chosen is not None and chosen.kind == "segment"


def test_partial_product_when_nothing_is_complete() -> None:
    chosen = choose_breakdown(facts(product_complete=False, segment=False))
    assert chosen is not None and chosen.kind == "product"


# Prompt and schema


def test_schema_limits_members_and_products() -> None:
    schema = mapping_schema(choose_breakdown(facts()), TAXONOMY)  # type: ignore[arg-type]
    item = schema["properties"]["lines"]["items"]
    assert item["properties"]["member"]["enum"] == [
        "ex:WaferMember",
        "ex:PattMember",
        "us-gaap:ServiceMember",
    ]
    products = item["properties"]["assignments"]["items"]["properties"]["product"]["enum"]
    assert products == [n.id for n in TAXONOMY]
    assert item["additionalProperties"] is False
    assert item["properties"]["new_product"]["type"] == ["string", "null"]


def test_prompt_lists_lines_and_taxonomy() -> None:
    prompt = mapping_prompt("KLA", "company/kla", facts(), choose_breakdown(facts()), TAXONOMY)  # type: ignore[arg-type]
    for text in (
        "KLA",
        "Wafer Inspection",
        "50.0%",
        "product/inspection-metrology",
        "process control",
        "2025-07-01",
    ):
        assert text in prompt


# Mapping a company


def test_map_company_records_lines_and_usage() -> None:
    mapping, runner = make_mapping()
    assert len(runner.calls) == 1
    assert mapping.status == "pending"
    assert mapping.kind == "product"
    assert [(ln.member, ln.share) for ln in mapping.lines] == [
        ("ex:WaferMember", 0.5),
        ("ex:PattMember", 0.3),
        ("us-gaap:ServiceMember", 0.2),
    ]
    assert mapping.lines[1].new_product == "Reticle inspection"
    assert mapping.usage["input_tokens"] == 7000


def test_fractions_are_clipped_and_normalized() -> None:
    answer = {
        "lines": [
            {
                "member": "ex:WaferMember",
                "assignments": [
                    {"product": "product/inspection-metrology", "fraction": 1.4},
                    {"product": "product/euv-lithography", "fraction": 0.6},
                ],
                "new_product": None,
                "note": "odd",
            }
        ]
    }
    mapping, _ = make_mapping(answer)
    fractions = {a.product: a.fraction for a in mapping.lines[0].assignments}
    assert sum(fractions.values()) == pytest.approx(1.0)
    assert fractions["product/inspection-metrology"] == pytest.approx(1 / 1.6)


def test_unknown_members_in_the_answer_are_ignored() -> None:
    answer = {
        "lines": [
            {
                "member": "ex:MadeUpMember",
                "assignments": [{"product": "product/euv-lithography", "fraction": 1.0}],
                "new_product": None,
                "note": "x",
            }
        ]
    }
    mapping, _ = make_mapping(answer)
    assert all(not ln.assignments for ln in mapping.lines)


# Edges


def test_edges_sum_lines_per_product() -> None:
    mapping, _ = make_mapping()
    [edge] = edges_from_mapping(mapping, accessed=date(2026, 9, 27))
    assert (edge["src"], edge["rel"], edge["dst"]) == (
        "company/kla",
        "PRODUCES",
        "product/inspection-metrology",
    )
    assert edge["weight"] == pytest.approx(0.5 + 0.3 * 0.8)
    # A split line makes the number a judgment on top of the filing.
    assert edge["weight_source"] == "manual"
    assert edge["confidence"] == 0.7
    assert edge["valid_from"] == date(2025, 7, 1)
    [evidence] = edge["evidence"]
    assert evidence["url"] == "https://www.sec.gov/kla-10k.htm"
    assert "Wafer Inspection 50.0%" in evidence["note"] and len(evidence["note"]) <= 200


def test_whole_lines_give_a_filing_weight() -> None:
    answer = {
        "lines": [
            {
                "member": "ex:WaferMember",
                "assignments": [{"product": "product/inspection-metrology", "fraction": 1.0}],
                "new_product": None,
                "note": "x",
            }
        ]
    }
    mapping, _ = make_mapping(answer)
    [edge] = edges_from_mapping(mapping, accessed=date(2026, 9, 27))
    assert (edge["weight"], edge["weight_source"], edge["confidence"]) == (0.5, "filing", 0.9)


# Queue files and acceptance


def test_mapping_round_trips_through_yaml(tmp_path: Path) -> None:
    mapping, _ = make_mapping()
    path = tmp_path / "kla.yaml"
    save_mapping(path, mapping)
    assert load_mapping(path) == mapping


def test_accept_writes_loadable_xbrl_edges(tmp_path: Path) -> None:
    mapping, _ = make_mapping()
    queue = tmp_path / "queue" / "kla.yaml"
    save_mapping(queue, mapping)
    out = accept_mapping(queue, tmp_path / "xbrl", reviewer="tester", accessed=date(2026, 9, 27))
    assert load_mapping(queue).status == "accepted"
    assert load_mapping(queue).reviewer == "tester"
    seed = load_seed(out.parent.parent, source="xbrl")
    assert seed.problems == []
    assert [e.value.dst for e in seed.edges] == ["product/inspection-metrology"]


# CLI


runner = CliRunner()


def test_map_and_review_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_seed
) -> None:
    from ripple import mapping as mapping_module
    from ripple.cli import app
    from ripple.edgar import UniverseEntry, save_universe
    from tests.conftest import base_edges, base_nodes, node

    cache_fixture(tmp_path / "raw")
    universe = tmp_path / "universe.yaml"
    save_universe(universe, [UniverseEntry("company/x", "EX", 1, "10-K", True)])
    nodes = [*base_nodes(), node("product/inspection-metrology")]
    seed = write_seed(nodes, base_edges())
    fake = FakeRunner(
        {
            "lines": [
                {
                    "member": "ex:WaferInspectionMember",
                    "assignments": [{"product": "product/inspection-metrology", "fraction": 1.0}],
                    "new_product": None,
                    "note": "inspection",
                }
            ]
        }
    )
    monkeypatch.setattr(mapping_module, "make_runner", lambda effort: fake)
    common = [
        "--universe",
        str(universe),
        "--cache",
        str(tmp_path / "raw"),
        "--queue",
        str(tmp_path / "queue"),
    ]

    mapped = runner.invoke(app, ["map", "--seed", str(seed), *common])
    assert mapped.exit_code == 0, mapped.output
    assert "company/x" in mapped.output

    listed = runner.invoke(app, ["review", "list", "--queue", str(tmp_path / "queue")])
    assert "company/x" in listed.output and "pending" in listed.output

    shown = runner.invoke(app, ["review", "show", "company/x", "--queue", str(tmp_path / "queue")])
    assert "Wafer Inspection" in shown.output and "product/inspection-metrology" in shown.output

    accepted = runner.invoke(
        app,
        [
            "review",
            "accept",
            "company/x",
            "--queue",
            str(tmp_path / "queue"),
            "--out",
            str(tmp_path / "xbrl"),
            "--reviewer",
            "tester",
        ],
    )
    assert accepted.exit_code == 0, accepted.output
    assert (tmp_path / "xbrl" / "edges" / "x.yaml").exists()


def test_unknown_products_in_the_answer_are_dropped() -> None:
    answer = {
        "lines": [
            {
                "member": "ex:WaferMember",
                "assignments": [
                    {"product": "product/not-in-taxonomy", "fraction": 0.5},
                    {"product": "product/inspection-metrology", "fraction": 0.5},
                ],
                "new_product": None,
                "note": "x",
            }
        ]
    }
    mapping, _ = make_mapping(answer)
    assert [a.product for a in mapping.lines[0].assignments] == ["product/inspection-metrology"]
