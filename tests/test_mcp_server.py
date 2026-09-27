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


async def test_lists_two_tools_with_guidance(client: Client) -> None:
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"search_entities", "find_exposed"}
    description = tools["find_exposed"].description or ""
    assert "search_entities" in description
    assert "per unit" in description
    assert "across themes" in description


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
            "theme_id": "theme/ai-compute",
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
    result = await client.call_tool("find_exposed", {"theme_id": "theme/nope"})
    assert result.is_error
    assert "search_entities" in result.content[0].text  # type: ignore[union-attr]


async def test_missing_database_is_tool_error(tmp_path: Path) -> None:
    async with Client(create_server(tmp_path / "missing.duckdb")) as c:
        result = await c.call_tool("search_entities", {"query": "asml"})
    assert result.is_error
    assert "ripple load" in result.content[0].text  # type: ignore[union-attr]
