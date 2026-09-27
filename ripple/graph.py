"""Build the demand-flow graph from a snapshot: node index plus sparse transfer matrix W."""

import math
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property

import networkx as nx
import numpy as np
from scipy import sparse

from ripple.model import DEFAULT_MIN_CONFIDENCE, Edge, Node, demand_flow
from ripple.store import Snapshot


@dataclass
class Graph:
    as_of: date
    min_confidence: float
    nodes: dict[str, Node]
    ids: list[str]
    index: dict[str, int]
    # W[i, j] is the signed share transferred from node i to node j along the demand flow.
    W: sparse.csr_array
    # The stored edge behind each nonzero cell of W.
    edges: dict[tuple[int, int], Edge] = field(default_factory=dict)

    @cached_property
    def digraph(self) -> nx.DiGraph:
        """The same graph for path search, with cost -log|w| so the strongest path is shortest."""
        g = nx.DiGraph()
        g.add_nodes_from(self.ids)
        for (i, j), edge in self.edges.items():
            w = float(self.W[i, j])
            g.add_edge(self.ids[i], self.ids[j], weight=w, cost=-math.log(abs(w)), edge=edge)
        return g

    def with_scaled_weights(self, factors: dict[tuple[int, int], float]) -> "Graph":
        """A copy with some cells of W multiplied by a factor (used by the sensitivity check)."""
        if factors:
            rows, cols = zip(*factors, strict=True)
            delta = sparse.csr_array(
                (np.array(list(factors.values())) - 1.0, (np.array(rows), np.array(cols))),
                shape=self.W.shape,
            )
            W = sparse.csr_array(self.W + self.W.multiply(delta))
        else:
            W = self.W.copy()
        return Graph(
            as_of=self.as_of,
            min_confidence=self.min_confidence,
            nodes=self.nodes,
            ids=self.ids,
            index=self.index,
            W=W,
            edges=self.edges,
        )


def build_graph(snapshot: Snapshot, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> Graph:
    ids = sorted(snapshot.nodes)
    index = {node_id: i for i, node_id in enumerate(ids)}
    cells: dict[tuple[int, int], Edge] = {}
    for edge in snapshot.edges:
        flow = demand_flow(edge)
        if flow is None or edge.confidence < min_confidence:
            continue
        try:
            cell = (index[flow[0]], index[flow[1]])
        except KeyError as exc:
            raise ValueError(
                f"edge {edge.label} has an endpoint missing from the snapshot"
            ) from exc
        if cell in cells:
            # Validation rule E12 should have caught this before loading.
            raise ValueError(f"edges {cells[cell].label} and {edge.label} share one cell of W")
        cells[cell] = edge

    rows = [i for i, _ in cells]
    cols = [j for _, j in cells]
    values = [edge.weight * edge.polarity for edge in cells.values()]
    W = sparse.csr_array(
        (np.array(values, dtype=float), (np.array(rows, dtype=int), np.array(cols, dtype=int))),
        shape=(len(ids), len(ids)),
    )
    return Graph(
        as_of=snapshot.as_of,
        min_confidence=min_confidence,
        nodes=dict(snapshot.nodes),
        ids=ids,
        index=index,
        W=W,
        edges=cells,
    )
