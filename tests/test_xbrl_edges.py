"""Region and customer edges straight from XBRL facts (M14)."""

from datetime import date
from pathlib import Path

from ripple.edgar import Filing
from ripple.model import Node
from ripple.xbrl import Breakdown, RevenueFacts, RevenueLine
from ripple.xbrl_edges import customer_edges, is_anonymous, region_edges, region_id

FILING = Filing(1, "0000000001-26-000001", "10-K", date(2026, 2, 1), date(2025, 12, 31), "x.htm")


def rl(member: str, label: str, share: float) -> RevenueLine:
    return RevenueLine(member, label, share * 100, share)


def facts(geo: list[RevenueLine], customers: list[RevenueLine]) -> RevenueFacts:
    breakdowns = {"geography": Breakdown("geography", "srt:StatementGeographicalAxis", geo, True)}
    return RevenueFacts(
        "us-gaap:Revenues",
        date(2025, 1, 1),
        date(2025, 12, 31),
        100.0,
        "USD",
        breakdowns,
        customers,
    )


def test_region_ids() -> None:
    assert region_id("country:TW", "TAIWAN") == "region/tw"
    assert region_id("srt:EuropeMember", "Europe") == "region/europe"
    assert region_id("amat:SoutheastAsiaMember", "Southeast Asia") == "region/southeast-asia"
    assert region_id("nvda:OtherCountriesMember", "Other Countries") is None
    assert region_id("asml:RestOfAsiaMember", "Rest Of Asia") is None
    assert region_id("us-gaap:NonUsMember", "Non-US") is None


def test_region_edges_and_nodes() -> None:
    nodes, edges = region_edges(
        "company/kla",
        facts(
            [
                rl("country:TW", "TAIWAN", 0.35),
                rl("srt:EuropeMember", "Europe", 0.25),
                rl("x:OtherMember", "Other Countries", 0.4),
            ],
            [],
        ),
        FILING,
        accessed=date(2026, 9, 27),
    )
    assert {n["id"]: n["label"] for n in nodes} == {
        "region/tw": "Taiwan",
        "region/europe": "Europe",
    }
    assert all(n["type"] == "region" for n in nodes)
    by_dst = {e["dst"]: e for e in edges}
    assert set(by_dst) == {"region/tw", "region/europe"}
    tw = by_dst["region/tw"]
    assert (tw["src"], tw["rel"], tw["weight"], tw["weight_source"]) == (
        "company/kla",
        "EXPOSED_TO",
        0.35,
        "filing",
    )
    assert tw["valid_from"] == date(2025, 1, 1)
    assert "Taiwan 35.0%" in tw["evidence"][0]["note"]


def test_anonymous_customers() -> None:
    for label in (
        "Customer A",
        "Customer One",
        "Four Customers",
        "Distributor B",
        "United States And Europe Based End Customers",
        "Largest customer",
    ):
        assert is_anonymous(label), label
    for label in ("PACCAR Inc.", "Microsoft", "Apple"):
        assert not is_anonymous(label), label


def test_named_customer_matched_to_a_company_node() -> None:
    companies = [
        Node(
            id="company/nvidia",
            type="company",
            label="Nvidia",
            ticker="NVDA",
            aliases=["NVIDIA Corporation"],
        ),
        Node(id="company/kla", type="company", label="KLA"),
    ]
    edges, skipped = customer_edges(
        "company/sk-hynix",
        facts(
            [],
            [
                rl("x:NvidiaCorporationMember", "NVIDIA Corporation", 0.3),
                rl("x:CustomerAMember", "Customer A", 0.2),
                rl("x:PaccarMember", "PACCAR Inc.", 0.13),
            ],
        ),
        FILING,
        companies,
        has_product_path=lambda customer, supplier: False,
        accessed=date(2026, 9, 27),
    )
    [supplies] = edges
    assert (supplies["src"], supplies["rel"], supplies["dst"], supplies["weight"]) == (
        "company/sk-hynix",
        "SUPPLIES",
        "company/nvidia",
        0.3,
    )
    assert supplies["confidence"] == 0.9
    assert skipped == {"Customer A": "anonymous", "PACCAR Inc.": "not in the graph"}


def test_customer_covered_by_product_layer_is_stored_below_threshold() -> None:
    companies = [Node(id="company/nvidia", type="company", label="Nvidia")]
    edges, _ = customer_edges(
        "company/sk-hynix",
        facts([], [rl("x:NvidiaMember", "Nvidia", 0.3)]),
        FILING,
        companies,
        has_product_path=lambda customer, supplier: True,
        accessed=date(2026, 9, 27),
    )
    assert edges[0]["confidence"] == 0.3  # stored, not propagated (D4, W9)


def test_xbrl_edges_command_writes_loadable_files(tmp_path: Path, write_seed) -> None:
    import yaml
    from typer.testing import CliRunner

    from ripple.cli import app
    from ripple.edgar import UniverseEntry, save_universe
    from ripple.load import load_sources
    from ripple.validate import validate
    from tests.conftest import base_edges, base_nodes
    from tests.test_xbrl import cache_fixture

    cache_fixture(tmp_path / "raw")
    universe = tmp_path / "universe.yaml"
    save_universe(universe, [UniverseEntry("company/x", "EX", 1, "10-K", True)])
    seed = write_seed(base_nodes(), base_edges())
    sources = tmp_path / "sources.yaml"
    sources.write_text(yaml.safe_dump({"seed": str(seed)}))
    out = tmp_path / "xbrl"
    result = CliRunner().invoke(
        app,
        [
            "xbrl-edges",
            "--universe",
            str(universe),
            "--cache",
            str(tmp_path / "raw"),
            "--sources",
            str(sources),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "company/x: 3 regions, 0 customers" in result.output
    assert "Customer A (anonymous)" in result.output
    merged = load_sources({"seed": seed, "xbrl": out})
    problems = [p for p in validate(merged, today=date(2026, 9, 27)) if p.severity == "error"]
    assert problems == []
    assert {e.value.dst for e in merged.edges if e.value.rel == "EXPOSED_TO"} == {
        "region/cn",
        "region/tw",
        "region/north-america",
    }


def test_corporate_suffixes_do_not_block_a_match() -> None:
    companies = [
        Node(id="company/dell", type="company", label="Dell Technologies", aliases=["Dell"])
    ]
    edges, skipped = customer_edges(
        "company/intel",
        facts(
            [],
            [
                rl("x:DellMember", "Dell Inc.", 0.19),
                rl("x:LenovoMember", "Lenovo Group Limited", 0.1),
            ],
        ),
        FILING,
        companies,
        has_product_path=lambda customer, supplier: True,
        accessed=date(2026, 9, 27),
    )
    assert [e["dst"] for e in edges] == ["company/dell"]
    assert skipped == {"Lenovo Group Limited": "not in the graph"}
