"""MCP server over stdio: the six PLAN §10 tools, each a thin wrapper on the library.

Every tool is read-only and returns the same envelope (docs/phase-3.md, M30): `as_of` first,
`min_confidence` wherever the graph is read, rounded numbers, and a `notes` list saying how to
read the result.
"""

import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ripple import explain, profile, signal
from ripple.attention import OBVIOUS_PERCENTILE
from ripple.explain import envelope
from ripple.model import DEFAULT_MIN_CONFIDENCE, today_utc
from ripple.score import DEFAULT_MAX_HOPS, OBVIOUS_HOPS, score
from ripple.search import search_entities as search
from ripple.store import Store

DEFAULT_DB = Path("data/ripple.duckdb")
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

INSTRUCTIONS = (
    "Ripple ranks public companies by their exposure to a trend (a theme) by walking a "
    "value-chain graph of products and companies built from filings and hand-sourced evidence. "
    "Resolve names to IDs with search_entities. trending_themes says which themes' news "
    "coverage burst recently. find_exposed ranks companies exposed to a theme or product, each "
    "with the paths that explain it; explain_link gives the paths between any two nodes; "
    "company_profile shows one company's revenue mix, neighbours and exposure per theme; "
    "get_evidence opens one edge's sources. Cite edge IDs and evidence URLs for every claim, "
    "say when a weight is a bucket guess or evidence is unverified, never add exposures across "
    "themes, and never multiply a burst by an exposure. Every result is as of its as_of date."
)

AsOf = Annotated[
    date | None, Field(description="Answer with what the store knew on this date; default today.")
]
MinConfidence = Annotated[
    float, Field(ge=0, le=1, description="Edges below this confidence do not propagate.")
]
MaxHops = Annotated[int, Field(ge=1, le=8, description="Longest path considered.")]


def create_server(db: Path) -> MCPServer:
    mcp = MCPServer("ripple", instructions=INSTRUCTIONS)

    def run[T](query: Callable[[Store], T], hint: str) -> T:
        """Open the store read-only for one call and turn a library ValueError into a
        ToolError that names the tool to call next."""
        try:
            store = Store(db, read_only=True)
        except FileNotFoundError as exc:
            raise ToolError(f"No Ripple store at {db}. Run `ripple load` first.") from exc
        with store:
            try:
                return query(store)
            except ValueError as exc:
                raise ToolError(f"{exc}. {hint}") from exc

    @mcp.tool(annotations=READ_ONLY)
    def search_entities(
        query: Annotated[str, Field(description="Name, alias, ticker or ID fragment.")],
        type: Annotated[
            Literal["company", "product", "material", "theme", "region"] | None,
            Field(description="Only return nodes of this type."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        as_of: AsOf = None,
    ) -> dict[str, Any]:
        """Find Ripple node IDs (companies, products, materials, themes) by fuzzy name match.

        Use this first to turn a name like "AI compute" or "ASML" into an ID such as
        theme/ai-compute-demand or company/asml. Results are best match first.
        """
        day = as_of or today_utc()

        def query_store(store: Store) -> dict[str, Any]:
            nodes = store.snapshot(day).nodes
            matches = search(nodes, query, type=type, limit=limit)
            return envelope(day, [], results=[m.to_dict() for m in matches])

        return run(query_store, "")

    @mcp.tool(annotations=READ_ONLY)
    def find_exposed(
        node_id: Annotated[
            str,
            Field(description="A theme ID, or a product or material ID, from search_entities."),
        ],
        direction: Annotated[
            Literal["up", "down"], Field(description="Whether demand is growing or shrinking.")
        ] = "up",
        max_hops: MaxHops = DEFAULT_MAX_HOPS,
        hide_obvious: Annotated[
            bool,
            Field(description="Hide companies the news already pairs with high attention."),
        ] = False,
        obvious_hops: Annotated[
            int,
            Field(
                ge=1,
                le=8,
                description=(
                    "Fallback for hide_obvious when a company has no coverage series: hide it if "
                    "this close or closer."
                ),
            ),
        ] = OBVIOUS_HOPS,
        obvious_percentile: Annotated[
            float,
            Field(
                ge=0,
                le=1,
                description=(
                    "With hide_obvious, hide companies at or above this attention percentile "
                    "among the exposed companies."
                ),
            ),
        ] = OBVIOUS_PERCENTILE,
        as_of: AsOf = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        min_confidence: MinConfidence = DEFAULT_MIN_CONFIDENCE,
    ) -> dict[str, Any]:
        """Rank companies by exposure to a demand shock on a theme or product, with the paths
        that explain each one.

        Resolve the ID with search_entities first. Exposure is the fractional revenue
        impact per unit of shock: 0.16 means a 10% rise in the theme moves about 1.6%
        of the company's revenue. Negative exposure means the company loses. Each result
        carries up to three paths (node IDs and edge IDs); cite them, and open any edge with
        get_evidence. guessed_weights counts bucketed (not measured) weights on the top path,
        path_confidence is the product of edge confidences, and weakest_confidence is the
        least certain edge on it. Themes are separate lenses: never add exposures
        across themes. A product shock reaches producers and upstream inputs, not customers.

        attention is the company's share of all news coverage over the last 90 days, and
        novelty is exposure discounted by its rank among the exposed companies, so a
        high novelty means real exposure the news has not already paired with the theme. Both
        are null when no coverage series is held, which means unknown attention, not none, so
        do not read a null as novel. Attention is company-wide rather than theme-paired, so a
        company famous for something unrelated can look crowded.
        """
        return run(
            lambda store: score(
                store,
                node_id,
                as_of=as_of or today_utc(),
                direction=direction,
                max_hops=max_hops,
                hide_obvious=hide_obvious,
                obvious_hops=obvious_hops,
                obvious_percentile=obvious_percentile,
                limit=limit,
                min_confidence=min_confidence,
            ).to_dict(),
            "Use search_entities with type='theme' or type='product' to find IDs.",
        )

    @mcp.tool(annotations=READ_ONLY)
    def explain_link(
        from_id: Annotated[str, Field(description="Where demand starts: a theme or product.")],
        to_id: Annotated[str, Field(description="Where it arrives, usually a company.")],
        k: Annotated[int, Field(ge=1, le=10, description="How many paths.")] = 3,
        max_hops: MaxHops = DEFAULT_MAX_HOPS,
        as_of: AsOf = None,
        min_confidence: MinConfidence = DEFAULT_MIN_CONFIDENCE,
    ) -> dict[str, Any]:
        """The strongest demand-flow paths between two nodes, every edge with its weight,
        weight_source, confidence, source layer and evidence (URL, access date, note, and
        whether a person has verified it).

        contribution is the signed share of a unit shock on from_id that reaches to_id along
        the path; the paths' contributions sum towards the exposure find_exposed reports. An
        empty paths list means no demand flows from from_id to to_id within max_hops.
        """
        return run(
            lambda store: explain.explain_link(
                store,
                from_id,
                to_id,
                as_of=as_of or today_utc(),
                k=k,
                max_hops=max_hops,
                min_confidence=min_confidence,
            ),
            "Use search_entities to find node IDs.",
        )

    @mcp.tool(annotations=READ_ONLY)
    def get_evidence(
        edge_id: Annotated[str, Field(description="An edge ID such as e-1a2b3c4d5e.")],
        as_of: AsOf = None,
        min_confidence: MinConfidence = DEFAULT_MIN_CONFIDENCE,
    ) -> dict[str, Any]:
        """The documents behind one edge: each evidence item's URL, access date, note in the
        maintainer's own words, span offsets where the claim was found in a filing, and whether
        a person has verified it. Also the edge's weight, weight_source, confidence, the source
        layer that won, and the rows from other layers that lost (alternatives).

        Edge IDs appear in find_exposed, explain_link and company_profile results. An edge
        with propagates false is stored as evidence but carries no shock.
        """
        return run(
            lambda store: explain.get_evidence(
                store, edge_id, as_of=as_of or today_utc(), min_confidence=min_confidence
            ),
            "Edge IDs come from find_exposed, explain_link or company_profile results.",
        )

    @mcp.tool(annotations=READ_ONLY)
    def company_profile(
        company_id: Annotated[str, Field(description="A company ID from search_entities.")],
        as_of: AsOf = None,
        min_confidence: MinConfidence = DEFAULT_MIN_CONFIDENCE,
    ) -> dict[str, Any]:
        """One company as the graph sees it: revenue mix by product (PRODUCES edges with
        weights and weight_source), customers and suppliers, parent and subsidiaries, revenue
        by region, and its exposure to every theme with its rank among that theme's exposed
        companies and the top path.

        Exposures are listed per theme and must never be summed across themes. mapped_share
        is how much of revenue the graph maps to products; the rest is outside the graph.
        attention is null when no coverage series is held, meaning unknown.
        """
        return run(
            lambda store: profile.company_profile(
                store, company_id, as_of=as_of or today_utc(), min_confidence=min_confidence
            ),
            "Use search_entities with type='company' to find company IDs.",
        )

    @mcp.tool(annotations=READ_ONLY)
    def trending_themes(
        window: Annotated[
            int, Field(ge=7, le=365, description="Bursts that ended in this many days.")
        ] = 90,
        min_surprise: Annotated[
            float,
            Field(
                ge=0,
                description="Burst threshold: -log10 p after the overdispersion correction.",
            ),
        ] = signal.MIN_SURPRISE,
        as_of: AsOf = None,
    ) -> dict[str, Any]:
        """Themes whose news coverage burst recently, strongest first, from stored GDELT
        coverage counts. No articles are returned and no news source is queried live.

        A burst means the theme is intensifying; drives lists the products it pushes up
        (polarity 1) or down (-1). ratio is coverage against the theme's own normal; surprise
        is how improbable the day was. Both are units of surprise, not demand: never multiply
        them by an exposure. tone_flag possible_reversal means the spike came with unusually
        negative tone, so the theme may be reversing. unmeasured lists themes with no coverage
        series, which means unknown, not quiet.
        """
        return run(
            lambda store: signal.trending_themes(
                store, as_of or today_utc(), window=window, min_surprise=min_surprise
            ),
            "",
        )

    return mcp


def main() -> None:
    create_server(Path(os.environ.get("RIPPLE_DB", DEFAULT_DB))).run()


if __name__ == "__main__":
    main()
