"""Seed data models: Node, Edge, Evidence, and the demand-flow mapping."""

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

NodeType = Literal["company", "product", "material", "theme", "region"]
Relation = Literal[
    "DRIVES",
    "REQUIRES",
    "PRODUCES",
    "SUPPLIES",
    "SUBSTITUTES",
    "COMPETES_WITH",
    "SUBSIDIARY_OF",
    "EXPOSED_TO",
    "CONSTRAINS",
]
WeightSource = Literal["bucket", "filing", "manual"]
ThemeKind = Literal["volume", "share_shift"]
THEME_KINDS: tuple[str, ...] = ("volume", "share_shift")


def today_utc() -> date:
    """Load times are UTC, so 'today' must be the UTC date or a fresh load would be invisible."""
    return datetime.now(UTC).date()


# Edges below this confidence are stored but do not propagate (D11).
DEFAULT_MIN_CONFIDENCE = 0.5

# Named bucket values for weights nobody has measured (docs/phase-0.md, weight buckets).
BUCKETS: dict[str, dict[str, float]] = {
    "revenue": {"minor": 0.05, "meaningful": 0.25, "core": 0.6, "pure": 0.9},
    "demand": {"minor": 0.1, "major": 0.4, "dominant": 0.8},
}
_RELATION_QUANTITY = {
    "PRODUCES": "revenue",
    "SUPPLIES": "revenue",
    "SUBSIDIARY_OF": "revenue",
    "DRIVES": "demand",
    "REQUIRES": "demand",
}


def bucket_quantity(rel: str) -> str | None:
    """Which bucket table a relation's weight comes from, if any."""
    return _RELATION_QUANTITY.get(rel)


def bucket_range(quantity: str, value: float) -> tuple[float, float] | None:
    """The span of true values a bucket stands for: geometric midpoints to its neighbours,
    mirrored in log space at the bottom and capped at a share of 1 at the top."""
    values = sorted(BUCKETS[quantity].values())
    matches = [i for i, v in enumerate(values) if math.isclose(v, value)]
    if not matches:
        return None
    i = matches[0]
    high = math.sqrt(values[i] * values[i + 1]) if i + 1 < len(values) else 1.0
    low = math.sqrt(values[i - 1] * values[i]) if i > 0 else value * value / high
    return low, min(high, 1.0)


class Span(BaseModel):
    """Where in the cached filing text a claim was found (D72): offsets, never the text."""

    model_config = ConfigDict(extra="forbid")

    section: str
    start: int
    end: int


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional here so the validator can report a missing one as rule E5.
    url: str | None = None
    note: str = ""
    accessed: date | None = None
    # Set to true once a human has read the source and agrees with the note.
    verified: bool = False
    span: Span | None = None


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: NodeType
    label: str
    aliases: list[str] = []
    group: str | None = None
    ticker: str | None = None
    country: str | None = None
    ids: dict[str, str | None] = {}
    # Required on themes; a plain str so the validator can report bad values as rule E13.
    kind: str | None = None
    # GDELT DOC query that measures this theme's news coverage (docs/phase-2.md, D87). Part of
    # the theme's definition, so the bitemporal store versions it: changing the query changes
    # every series it produced. Themes only; company queries are built from label and aliases.
    query: str | None = None


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    rel: Relation
    dst: str
    weight: float
    weight_source: WeightSource
    polarity: int
    confidence: float
    valid_from: date
    valid_to: date | None = None
    evidence: list[Evidence] = []

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.rel, self.dst)

    @property
    def id(self) -> str:
        """Derived from (src, rel, dst), never written by hand (D12)."""
        digest = hashlib.sha256("|".join(self.key).encode()).hexdigest()
        return f"e-{digest[:10]}"

    @property
    def label(self) -> str:
        return " ".join(self.key)

    def active_on(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or self.valid_to > day)


def demand_flow(edge: Edge) -> tuple[str, str] | None:
    """Map a stored edge to the direction demand flows, or None if it is not traversed."""
    match edge.rel:
        case "DRIVES" | "REQUIRES" | "SUBSIDIARY_OF":
            return (edge.src, edge.dst)
        case "PRODUCES" | "SUPPLIES":
            return (edge.dst, edge.src)
        case _:
            return None


@dataclass(frozen=True)
class Located[T]:
    """A parsed record, the file it came from, and its source (D40)."""

    value: T
    file: Path
    source: str = "seed"


# Sources in the store (docs/phase-1.md, D40). For edges, evidence decides: a human-verified or
# reviewed row first, then filings, then the hand seed, then LLM extraction. For nodes the
# curated sources come first, since they carry the readable labels.
EDGE_SOURCE_ORDER = ("review", "xbrl", "seed", "llm")
NODE_SOURCE_ORDER = ("review", "seed", "xbrl", "llm")
SOURCES = EDGE_SOURCE_ORDER


SAME_PERIOD_DAYS = 90  # facts whose valid_from differ by less count as the same period


def rank_rows[T: "Edge"](rows: list[tuple[str, T]]) -> list[tuple[str, T]]:
    """Order (source, edge) rows for one key, best first (D40, D63):
    1. human-trusted rows (source `review`, or verified evidence);
    2. sourced numbers (`filing`, `manual`) before bucket guesses;
    3. the newest fact, when it is more than SAME_PERIOD_DAYS newer;
    4. the source order `xbrl`, `seed`, `llm`.
    """
    remaining = list(rows)
    ranked: list[tuple[str, T]] = []
    while remaining:
        best = min(_row_class(source, edge) for source, edge in remaining)
        group = [row for row in remaining if _row_class(*row) == best]
        newest = max(edge.valid_from for _, edge in group)
        current = [row for row in group if (newest - row[1].valid_from).days <= SAME_PERIOD_DAYS]
        choice = min(
            current,
            key=lambda row: (_order(EDGE_SOURCE_ORDER, row[0]), -row[1].valid_from.toordinal()),
        )
        ranked.append(choice)
        remaining.remove(choice)
    return ranked


def _row_class(source: str, edge: "Edge") -> tuple[int, int]:
    trusted = source == "review" or any(item.verified for item in edge.evidence)
    return (0 if trusted else 1, 1 if edge.weight_source == "bucket" else 0)


def node_precedence(source: str) -> int:
    return _order(NODE_SOURCE_ORDER, source)


def _order(order: tuple[str, ...], source: str) -> int:
    return order.index(source) if source in order else len(order)


def consolidate[T: "Edge"](
    rows: list[tuple[str, T]],
) -> tuple[list[tuple[str, T]], dict[tuple[str, str, str], list[tuple[str, T]]]]:
    """One row per edge key, chosen by precedence, plus the rows that lost, in order.
    `rows` are (source, edge) pairs valid at one moment."""
    by_key: dict[tuple[str, str, str], list[tuple[str, T]]] = {}
    for source, edge in rows:
        by_key.setdefault(edge.key, []).append((source, edge))
    winners = []
    losers = {}
    for key, group in by_key.items():
        ranked = rank_rows(group)
        winners.append(ranked[0])
        if len(ranked) > 1:
            losers[key] = ranked[1:]
    return winners, losers


@dataclass(frozen=True)
class Problem:
    severity: Literal["error", "warning"]
    rule: str
    file: Path | None
    record: str
    message: str

    def __str__(self) -> str:
        where = f"{self.file} " if self.file else ""
        return f"{self.severity.upper()} [{self.rule}] {where}{self.record}: {self.message}"
