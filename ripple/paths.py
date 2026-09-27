"""Explanation paths (Yen's k shortest paths on -log|w|) and minimum hop counts."""

import math
from dataclasses import dataclass
from itertools import islice

import networkx as nx

from ripple.graph import Graph
from ripple.model import Edge

# Upper bound on candidate paths examined per query, so a large graph cannot stall a search
# that keeps finding paths longer than max_hops.
MAX_CANDIDATES = 10_000


@dataclass(frozen=True)
class Path:
    nodes: list[str]
    edge_records: list[Edge]
    # Signed share of the shock that reaches the target along this path, per unit shock.
    contribution: float
    # Product of edge confidences along the path.
    confidence: float

    @property
    def edges(self) -> list[str]:
        return [edge.id for edge in self.edge_records]

    @property
    def weakest_confidence(self) -> float:
        """The least certain edge on the path; unlike the product it does not shrink with length."""
        return min((edge.confidence for edge in self.edge_records), default=1.0)


def top_paths(graph: Graph, source: str, target: str, k: int = 3, max_hops: int = 5) -> list[Path]:
    """Up to k simple paths with at most max_hops edges, strongest |contribution| first."""
    g = graph.digraph
    if source not in g or target not in g:
        return []
    found: list[Path] = []
    candidates = nx.shortest_simple_paths(g, source, target, weight="cost")
    try:
        for nodes in islice(candidates, MAX_CANDIDATES):
            if len(nodes) - 1 > max_hops:
                continue
            found.append(_path(g, nodes))
            if len(found) == k:
                break
    except nx.NetworkXNoPath:
        pass
    return found


def _path(g: nx.DiGraph, nodes: list[str]) -> Path:
    pairs = list(zip(nodes, nodes[1:], strict=False))
    data = [g.edges[u, v] for u, v in pairs]
    return Path(
        nodes=list(nodes),
        edge_records=[d["edge"] for d in data],
        contribution=math.prod(d["weight"] for d in data),
        confidence=math.prod(d["edge"].confidence for d in data),
    )


def min_hops(graph: Graph, source: str, max_hops: int = 5) -> dict[str, int]:
    """Breadth-first hop count from source to every node it reaches within max_hops."""
    if source not in graph.digraph:
        return {}
    lengths = nx.single_source_shortest_path_length(graph.digraph, source, cutoff=max_hops)
    return {node: hops for node, hops in lengths.items() if node != source}
