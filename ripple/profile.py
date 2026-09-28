"""One company as the graph sees it: revenue mix, neighbours, exposure per theme (Phase 3, M32).

The aggregation behind the `company_profile` tool and `ripple profile`. Exposure to each theme
is listed separately and never summed: themes are separate lenses whose DRIVES shares may add
up past 1 (D9).
"""

from datetime import date
from typing import Any

from ripple import attention as attention_module
from ripple.explain import EDGE_NOTE, UNVERIFIED_NOTE, edge_record, envelope, path_record
from ripple.graph import build_graph
from ripple.model import DEFAULT_MIN_CONFIDENCE
from ripple.paths import top_paths
from ripple.propagate import Shock
from ripple.score import DEFAULT_MAX_HOPS, company_exposures
from ripple.store import Store

PROFILE_NOTE = (
    "revenue_mix lists PRODUCES edges; mapped_share is their sum and the rest of revenue is "
    "outside the graph. themes lists exposure per unit shock to each theme, with the company's "
    "rank among that theme's exposed companies; never add exposures across themes. attention "
    "is null when no coverage series is held, which means unknown, not none."
)


def company_profile(
    store: Store,
    company_id: str,
    as_of: date,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> dict[str, Any]:
    snapshot = store.snapshot(as_of)
    node = snapshot.nodes.get(company_id)
    if node is None:
        raise ValueError(f"unknown node {company_id}")
    if node.type != "company":
        raise ValueError(f"{company_id} is a {node.type}, not a company")
    graph = build_graph(snapshot, min_confidence=min_confidence)

    def label(node_id: str) -> str:
        other = snapshot.nodes.get(node_id)
        return other.label if other else node_id

    def neighbour(edge_id_field: str, other: str, edge: Any) -> dict[str, Any]:
        record = edge_record(edge, snapshot, min_confidence, evidence=False)
        return {edge_id_field: other, "label": label(other), **record}

    mix, customers, suppliers, parents, subsidiaries, regions = [], [], [], [], [], []
    for edge in snapshot.edges:
        if edge.src == company_id:
            match edge.rel:
                case "PRODUCES":
                    mix.append(neighbour("product", edge.dst, edge))
                case "SUPPLIES":
                    customers.append(neighbour("customer", edge.dst, edge))
                case "SUBSIDIARY_OF":
                    parents.append(neighbour("parent", edge.dst, edge))
                case "EXPOSED_TO":
                    regions.append(neighbour("region", edge.dst, edge))
        elif edge.dst == company_id:
            match edge.rel:
                case "SUPPLIES":
                    suppliers.append(neighbour("supplier", edge.src, edge))
                case "SUBSIDIARY_OF":
                    subsidiaries.append(neighbour("subsidiary", edge.src, edge))
    for group in (mix, customers, suppliers, parents, subsidiaries, regions):
        group.sort(key=lambda r: (-r["weight"], r["edge"]))

    themes = []
    for theme_id in sorted(n for n, v in graph.nodes.items() if v.type == "theme"):
        exposures = company_exposures(graph, Shock(theme_id), max_hops)
        if company_id not in exposures:
            continue
        ranked = sorted(exposures, key=lambda c: (-abs(exposures[c]), c))
        paths = top_paths(graph, theme_id, company_id, k=1, max_hops=max_hops)
        themes.append(
            {
                "theme": theme_id,
                "label": graph.nodes[theme_id].label,
                "kind": graph.nodes[theme_id].kind,
                "exposure": round(exposures[company_id], 4),
                "rank": ranked.index(company_id) + 1,
                "of": len(ranked),
                "top_path": (
                    path_record(paths[0], graph, snapshot, evidence=False) if paths else None
                ),
            }
        )
    themes.sort(key=lambda t: (-abs(t["exposure"]), t["theme"]))

    held = attention_module.company_attention(store, [company_id], as_of).get(company_id)
    return envelope(
        as_of,
        [PROFILE_NOTE, EDGE_NOTE, UNVERIFIED_NOTE],
        min_confidence=min_confidence,
        company={
            "id": node.id,
            "label": node.label,
            "ticker": node.ticker,
            "country": node.country,
            "aliases": node.aliases,
        },
        mapped_share=round(sum(r["weight"] for r in mix if r["propagates"]), 4),
        revenue_mix=mix,
        customers=customers,
        suppliers=suppliers,
        parents=parents,
        subsidiaries=subsidiaries,
        regions=regions,
        themes=themes,
        attention=(
            None
            if held is None
            else {"share": round(held.share, 8), "days": held.days, "matched": held.matched}
        ),
    )
