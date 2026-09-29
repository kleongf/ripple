from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp import Client

from ripple.mcp_server import create_server
from ripple.store import Store

MINI = Path(__file__).parent / "fixtures" / "mini"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "mcp.duckdb"
    with Store(path) as store:
        store.load(MINI, now=datetime(2026, 9, 1, tzinfo=UTC))
    return path


@pytest.fixture
async def client(db: Path) -> AsyncIterator[Client]:
    async with Client(create_server(db), raise_exceptions=True) as c:
        yield c


TOOLS = {
    "search_entities",
    "find_exposed",
    "explain_link",
    "get_evidence",
    "company_profile",
    "trending_themes",
}


async def test_lists_the_six_plan_tools_all_read_only(client: Client) -> None:
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint
    description = tools["find_exposed"].description or ""
    assert "search_entities" in description
    assert "per unit" in description
    assert "across themes" in description


async def call(client: Client, name: str, args: dict) -> dict:
    result = await client.call_tool(name, args)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def test_every_tool_returns_the_envelope(client: Client) -> None:
    """as_of first and notes last on every tool, and min_confidence wherever the graph is read."""
    edge = (await call(client, "find_exposed", {"node_id": "theme/ai-compute"}))["results"][0]
    calls = {
        "search_entities": {"query": "asml"},
        "find_exposed": {"node_id": "theme/ai-compute"},
        "explain_link": {"from_id": "theme/ai-compute", "to_id": "company/asml"},
        "get_evidence": {"edge_id": edge["paths"][0]["edges"][0]},
        "company_profile": {"company_id": "company/vertiv"},
        "trending_themes": {},
    }
    assert set(calls) == TOOLS
    for name, args in calls.items():
        data = await call(client, name, {**args, "as_of": "2026-09-26"})
        keys = list(data)
        assert keys[0] == "as_of" and data["as_of"] == "2026-09-26", name
        assert keys[-1] == "notes" and isinstance(data["notes"], list), name
        if name in {"find_exposed", "explain_link", "get_evidence", "company_profile"}:
            assert "min_confidence" in data, name


async def test_search_entities(client: Client) -> None:
    result = await client.call_tool("search_entities", {"query": "asml"})
    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["results"][0]["id"] == "company/asml"


async def test_search_entities_type_filter(client: Client) -> None:
    result = await client.call_tool("search_entities", {"query": "compute", "type": "theme"})
    assert result.structured_content is not None
    assert [r["id"] for r in result.structured_content["results"]] == ["theme/ai-compute"]


async def test_find_exposed(client: Client) -> None:
    result = await client.call_tool(
        "find_exposed",
        {
            "node_id": "theme/ai-compute",
            "as_of": "2026-09-26",
            "hide_obvious": True,
            "obvious_hops": 2,
        },
    )
    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert data["theme_kind"] == "volume"
    assert [r["company"] for r in data["results"]] == [
        "company/parts",
        "company/tsmc",
        "company/asml",
        "company/vertiv",
    ]
    asml = data["results"][2]
    assert asml["exposure"] == 0.16
    assert len(asml["paths"][0]["edges"]) == 4


async def test_find_exposed_unknown_theme_is_tool_error(client: Client) -> None:
    result = await client.call_tool("find_exposed", {"node_id": "theme/nope"})
    assert result.is_error
    assert "search_entities" in result.content[0].text  # type: ignore[union-attr]


async def test_missing_database_is_tool_error(tmp_path: Path) -> None:
    async with Client(create_server(tmp_path / "missing.duckdb")) as c:
        result = await c.call_tool("search_entities", {"query": "asml"})
    assert result.is_error
    assert "ripple load" in result.content[0].text  # type: ignore[union-attr]


async def test_find_exposed_on_a_product(client: Client) -> None:
    data = await call(client, "find_exposed", {"node_id": "product/leading-edge-logic"})
    # By hand: TSMC 0.6; ASML 0.8 x 0.5 = 0.4; Multi 0.8 x 0.2 = 0.16. Nvidia is a customer of
    # leading-edge logic, not a supplier, so a shock on it does not reach Nvidia.
    assert {r["company"]: r["exposure"] for r in data["results"]} == {
        "company/tsmc": 0.6,
        "company/asml": 0.4,
        "company/multi": 0.16,
    }
    assert data["shock_type"] == "product"
    assert any("customers" in note for note in data["notes"])


async def test_find_exposed_rejects_a_company(client: Client) -> None:
    result = await client.call_tool("find_exposed", {"node_id": "company/asml"})
    assert result.is_error


async def test_explain_link_carries_evidence_on_every_edge(client: Client) -> None:
    data = await call(
        client, "explain_link", {"from_id": "theme/ai-compute", "to_id": "company/asml"}
    )
    (path,) = data["paths"]
    assert path["contribution"] == 0.16
    assert [n["id"] for n in path["nodes"]] == [
        "theme/ai-compute",
        "product/accelerators",
        "product/leading-edge-logic",
        "product/euv",
        "company/asml",
    ]
    for edge in path["edges"]:
        assert edge["evidence"] and edge["evidence"][0]["url"]
        assert edge["evidence"][0]["verified"] is False
        assert edge["propagates"] is True


async def test_explain_link_with_no_path_is_empty_not_an_error(client: Client) -> None:
    data = await call(
        client, "explain_link", {"from_id": "theme/cooling-shift", "to_id": "company/asml"}
    )
    assert data["paths"] == []


async def test_get_evidence_on_a_sub_threshold_edge(client: Client) -> None:
    """Rumor's edge sits at confidence 0.3: returned with its evidence, marked as not
    propagating, so a caller can see why the company is missing from a ranking."""
    from ripple.model import Edge

    rumor = Edge(
        src="company/rumor",
        rel="PRODUCES",
        dst="product/accelerators",
        weight=0.5,
        weight_source="manual",
        polarity=1,
        confidence=0.3,
        valid_from="2020-01-01",
    )
    data = await call(client, "get_evidence", {"edge_id": rumor.id})
    assert data["edge"] == rumor.id
    assert data["propagates"] is False
    assert data["src_label"] == "Rumor"


async def test_get_evidence_unknown_edge_is_tool_error(client: Client) -> None:
    result = await client.call_tool("get_evidence", {"edge_id": "e-0000000000"})
    assert result.is_error
    assert "find_exposed" in result.content[0].text  # type: ignore[union-attr]


async def test_company_profile_lists_themes_separately(client: Client) -> None:
    data = await call(client, "company_profile", {"company_id": "company/vertiv"})
    assert [(r["product"], r["weight"]) for r in data["revenue_mix"]] == [
        ("product/liquid-cooling", 0.3)
    ]
    assert data["mapped_share"] == 0.3
    # By hand: AI compute 0.5 x 0.9 x 0.3 = 0.135; cooling shift 0.3 x 0.3 = 0.09. Listed, never
    # summed.
    assert [(t["theme"], t["exposure"]) for t in data["themes"]] == [
        ("theme/ai-compute", 0.135),
        ("theme/cooling-shift", 0.09),
    ]
    assert data["attention"] is None


async def test_company_profile_of_a_product_is_tool_error(client: Client) -> None:
    result = await client.call_tool("company_profile", {"company_id": "product/euv"})
    assert result.is_error


async def test_trending_themes_without_coverage_lists_every_theme_as_unmeasured(
    client: Client,
) -> None:
    data = await call(client, "trending_themes", {})
    assert data["bursts"] == []
    assert data["unmeasured"] == ["theme/ai-compute", "theme/cooling-shift"]


async def test_find_exposed_can_add_priced_in(client: Client) -> None:
    data = await call(client, "find_exposed", {"node_id": "theme/ai-compute", "priced_in": True})
    assert all("priced_in" in r for r in data["results"])
    # The mini fixture holds no prices, so every value is unknown rather than zero.
    assert {r["priced_in"] for r in data["results"]} == {None}
    assert any("priced_in" in note for note in data["notes"])
    plain = await call(client, "find_exposed", {"node_id": "theme/ai-compute"})
    assert not any("priced_in" in r for r in plain["results"])
    product = await call(
        client, "find_exposed", {"node_id": "product/accelerators", "priced_in": True}
    )
    assert not any("priced_in" in r for r in product["results"])
