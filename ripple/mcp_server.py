"""MCP server over stdio: search_entities and find_exposed. A thin wrapper on the library."""

import os
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ripple.model import DEFAULT_MIN_CONFIDENCE, today_utc
from ripple.score import DEFAULT_MAX_HOPS, OBVIOUS_HOPS, score
from ripple.search import search_entities as search
from ripple.store import Store

DEFAULT_DB = Path("data/ripple.duckdb")
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

INSTRUCTIONS = (
    "Ripple ranks public companies by their exposure to a trend (a theme) by walking a "
    "hand-built value-chain graph. Resolve names to IDs with search_entities, then call "
    "find_exposed with a theme ID. Cite the returned paths when explaining a result."
)


def create_server(db: Path) -> MCPServer:
    mcp = MCPServer("ripple", instructions=INSTRUCTIONS)

    def open_store() -> Store:
        try:
            return Store(db, read_only=True)
        except FileNotFoundError as exc:
            raise ToolError(f"No Ripple store at {db}. Run `ripple load data/seed` first.") from exc

    @mcp.tool(annotations=READ_ONLY)
    def search_entities(
        query: Annotated[str, Field(description="Name, alias, ticker or ID fragment.")],
        type: Annotated[
            Literal["company", "product", "material", "theme", "region"] | None,
            Field(description="Only return nodes of this type."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Find Ripple node IDs (companies, products, materials, themes) by fuzzy name match.

        Use this first to turn a name like "AI compute" or "ASML" into an ID such as
        theme/ai-compute-demand or company/asml. Results are best match first.
        """
        with open_store() as store:
            nodes = store.snapshot(today_utc()).nodes
        return {"results": [m.to_dict() for m in search(nodes, query, type=type, limit=limit)]}

    @mcp.tool(annotations=READ_ONLY)
    def find_exposed(
        theme_id: Annotated[str, Field(description="A theme ID from search_entities.")],
        direction: Annotated[
            Literal["up", "down"], Field(description="Whether the theme is growing or shrinking.")
        ] = "up",
        max_hops: Annotated[int, Field(ge=1, le=8)] = DEFAULT_MAX_HOPS,
        hide_obvious: Annotated[
            bool,
            Field(description="Hide companies within obvious_hops of the theme."),
        ] = False,
        obvious_hops: Annotated[
            int,
            Field(
                ge=1, le=8, description="With hide_obvious, hide companies this close or closer."
            ),
        ] = OBVIOUS_HOPS,
        as_of: Annotated[
            date | None, Field(description="Score with the graph as known on this date.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        min_confidence: Annotated[
            float, Field(ge=0, le=1, description="Edges below this confidence do not count.")
        ] = DEFAULT_MIN_CONFIDENCE,
    ) -> dict[str, Any]:
        """Rank companies by exposure to a theme shock, with the paths that explain each one.

        Resolve the theme with search_entities first. Exposure is the fractional revenue
        impact per unit of theme shock: 0.16 means a 10% rise in the theme moves about 1.6%
        of the company's revenue. Negative exposure means the company loses. Each result
        carries up to three paths (node IDs and edge IDs); cite them when explaining.
        guessed_weights counts bucketed (not measured) weights on the top path,
        path_confidence is the product of edge confidences, and weakest_confidence is the
        least certain edge on it. Themes are separate lenses: do
        not add exposures across themes.
        """
        with open_store() as store:
            try:
                result = score(
                    store,
                    theme_id,
                    as_of=as_of or today_utc(),
                    direction=direction,
                    max_hops=max_hops,
                    hide_obvious=hide_obvious,
                    obvious_hops=obvious_hops,
                    limit=limit,
                    min_confidence=min_confidence,
                )
            except ValueError as exc:
                raise ToolError(
                    f"{exc}. Use search_entities with type='theme' to find theme IDs."
                ) from exc
        return result.to_dict()

    return mcp


def main() -> None:
    create_server(Path(os.environ.get("RIPPLE_DB", DEFAULT_DB))).run()


if __name__ == "__main__":
    main()
