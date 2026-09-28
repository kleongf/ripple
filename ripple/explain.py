"""Paths and edges with their evidence, as compact records (Phase 3, M31).

One serializer for `explain_link`, `get_evidence`, `company_profile` and `ripple explain
--json`, so the CLI and the MCP tools print the same facts. Every record states how weak it is:
`weight_source`, confidence, whether the edge propagates, the source layer that won (D63) and
the rows that lost, and each evidence item's verification ruling.
"""

from datetime import date
from typing import Any

from ripple.graph import Graph, build_graph
from ripple.model import DEFAULT_MIN_CONFIDENCE, Edge, Evidence, demand_flow
from ripple.paths import Path, top_paths
from ripple.score import DEFAULT_MAX_HOPS
from ripple.store import Snapshot, Store

UNVERIFIED_NOTE = (
    "verified is false until a person has read the source and agreed with the note; an "
    "unverified item is a lead to a source, not a checked fact."
)
EDGE_NOTE = (
    "weight is a share in (0, 1]: revenue share on PRODUCES, SUPPLIES and SUBSIDIARY_OF, demand "
    "share on DRIVES and REQUIRES; sign is in polarity. weight_source bucket means a guessed "
    "size class, not a measured number. Edges with propagates false are stored as evidence but "
    "carry no shock."
)
PATH_NOTE = (
    "contribution is the signed share of a unit shock on the first node that reaches the last "
    "node along this path; the sum over all paths is the exposure. Cite edge IDs; open any of "
    "them with get_evidence."
)


def envelope(as_of: date, notes: list[str], **fields: Any) -> dict[str, Any]:
    """The shape every tool returns: `as_of` first, the payload, then notes (M30)."""
    return {"as_of": as_of.isoformat(), **fields, "notes": notes}


def evidence_record(item: Evidence) -> dict[str, Any]:
    record: dict[str, Any] = {
        "url": item.url,
        "accessed": item.accessed.isoformat() if item.accessed else None,
        "note": item.note,
        "verified": item.verified,
    }
    if item.span is not None:
        record["span"] = item.span.model_dump()
    return record


def edge_record(
    edge: Edge,
    snapshot: Snapshot,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    evidence: bool = True,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "edge": edge.id,
        "src": edge.src,
        "rel": edge.rel,
        "dst": edge.dst,
        "weight": round(edge.weight, 4),
        "weight_source": edge.weight_source,
        "polarity": edge.polarity,
        "confidence": round(edge.confidence, 3),
        "propagates": demand_flow(edge) is not None and edge.confidence >= min_confidence,
        "valid_from": edge.valid_from.isoformat(),
        "valid_to": edge.valid_to.isoformat() if edge.valid_to else None,
        "source": snapshot.sources.get(edge.id, "seed"),
    }
    if evidence:
        record["evidence"] = [evidence_record(item) for item in edge.evidence]
        record["alternatives"] = [
            {
                "source": source,
                "weight": round(alt.weight, 4),
                "weight_source": alt.weight_source,
                "confidence": round(alt.confidence, 3),
                "valid_from": alt.valid_from.isoformat(),
                "evidence": [evidence_record(item) for item in alt.evidence],
            }
            for source, alt in snapshot.alternatives.get(edge.id, [])
        ]
    return record


def path_record(
    path: Path, graph: Graph, snapshot: Snapshot, evidence: bool = True
) -> dict[str, Any]:
    return {
        "contribution": round(path.contribution, 4),
        "confidence": round(path.confidence, 3),
        "weakest_confidence": round(path.weakest_confidence, 3),
        "guessed_weights": sum(e.weight_source == "bucket" for e in path.edge_records),
        "nodes": [{"id": n, "label": graph.nodes[n].label} for n in path.nodes],
        "edges": [
            edge_record(e, snapshot, graph.min_confidence, evidence) for e in path.edge_records
        ],
    }


def explain_link(
    store: Store,
    from_id: str,
    to_id: str,
    as_of: date,
    k: int = 3,
    max_hops: int = DEFAULT_MAX_HOPS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """The k strongest demand-flow paths from one node to another, each edge with evidence."""
    snapshot = store.snapshot(as_of)
    graph = build_graph(snapshot, min_confidence=min_confidence)
    for node_id in (from_id, to_id):
        if node_id not in graph.nodes:
            raise ValueError(f"unknown node {node_id}")
    paths = top_paths(graph, from_id, to_id, k=k, max_hops=max_hops)
    return envelope(
        as_of,
        [PATH_NOTE, EDGE_NOTE, UNVERIFIED_NOTE],
        min_confidence=min_confidence,
        max_hops=max_hops,
        from_node={"id": from_id, "label": graph.nodes[from_id].label},
        to_node={"id": to_id, "label": graph.nodes[to_id].label},
        paths=[path_record(p, graph, snapshot) for p in paths],
    )


def get_evidence(
    store: Store,
    edge_id: str,
    as_of: date,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """One edge as the store knew it on `as_of`, with evidence and the rows that lost."""
    snapshot = store.snapshot(as_of)
    edge = next((e for e in snapshot.edges if e.id == edge_id), None)
    if edge is None:
        raise ValueError(f"no edge {edge_id} valid on {as_of}")
    return envelope(
        as_of,
        [EDGE_NOTE, UNVERIFIED_NOTE],
        min_confidence=min_confidence,
        src_label=snapshot.nodes[edge.src].label if edge.src in snapshot.nodes else edge.src,
        dst_label=snapshot.nodes[edge.dst].label if edge.dst in snapshot.nodes else edge.dst,
        **edge_record(edge, snapshot, min_confidence),
    )
