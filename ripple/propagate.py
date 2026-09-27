"""Push a theme shock through the demand-flow matrix."""

from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy import sparse

from ripple.graph import Graph

Direction = Literal["up", "down"]


@dataclass(frozen=True)
class Shock:
    theme: str
    magnitude: float = 1.0
    direction: Direction = "up"

    @property
    def signed(self) -> float:
        return self.magnitude if self.direction == "up" else -self.magnitude


def shock_vector(graph: Graph, shock: Shock) -> np.ndarray:
    if shock.theme not in graph.index:
        raise KeyError(f"unknown theme {shock.theme}")
    x = np.zeros(len(graph.ids))
    x[graph.index[shock.theme]] = shock.signed
    return x


def propagate(W: sparse.csr_array, shock: np.ndarray, hops: int = 5) -> np.ndarray:
    """Sum over path lengths 1..hops of the shock carried along every path (D2)."""
    x = shock.astype(float)
    total = np.zeros(W.shape[0])
    for _ in range(hops):
        x = W.T @ x  # advance every path by one hop
        total += x  # sum over path lengths
    return total  # at company nodes: revenue impact per unit shock
