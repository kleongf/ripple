"""Bitemporal DuckDB store. Loading never overwrites or deletes; it supersedes (D6, D13)."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import duckdb

from ripple.load import load_sources
from ripple.model import Edge, Evidence, Node, Problem, Span, consolidate, node_precedence
from ripple.validate import validate

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS edge_row_seq;
CREATE TABLE IF NOT EXISTS nodes (
    id VARCHAR NOT NULL,
    type VARCHAR NOT NULL,
    label VARCHAR NOT NULL,
    data JSON NOT NULL,
    content_hash VARCHAR NOT NULL,
    recorded_at TIMESTAMP NOT NULL,
    superseded_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS edges (
    row_id BIGINT PRIMARY KEY DEFAULT nextval('edge_row_seq'),
    edge_id VARCHAR NOT NULL,
    src VARCHAR NOT NULL,
    rel VARCHAR NOT NULL,
    dst VARCHAR NOT NULL,
    weight DOUBLE NOT NULL,
    weight_source VARCHAR NOT NULL,
    polarity INTEGER NOT NULL,
    confidence DOUBLE NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    content_hash VARCHAR NOT NULL,
    recorded_at TIMESTAMP NOT NULL,
    superseded_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS evidence (
    edge_row_id BIGINT NOT NULL,
    position INTEGER NOT NULL,
    url VARCHAR,
    note VARCHAR,
    accessed DATE
);
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS span VARCHAR;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'seed';
ALTER TABLE edges ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'seed';
"""

EDGE_COLUMNS = (
    "row_id, src, rel, dst, weight, weight_source, polarity, confidence, valid_from, valid_to, "
    "source"
)

# A stored edge row is identified by its key plus valid_from, so the seed may hold
# consecutive versions of one edge.
RowKey = tuple[str, str, str, date]


class SeedInvalidError(Exception):
    def __init__(self, problems: list[Problem]) -> None:
        super().__init__(f"seed has {len(problems)} validation errors")
        self.problems = problems


@dataclass(frozen=True)
class LoadReport:
    inserted: int
    superseded: int
    unchanged: int


@dataclass(frozen=True)
class Snapshot:
    as_of: date
    nodes: dict[str, Node]
    # One edge per key, chosen by source precedence (D40).
    edges: list[Edge]
    # Edge ID to the source of the chosen row.
    sources: dict[str, str] = field(default_factory=dict)
    # Edge ID to the rows that lost, most trusted first, as (source, edge).
    alternatives: dict[str, list[tuple[str, Edge]]] = field(default_factory=dict)


class Store:
    def __init__(self, path: Path | str, read_only: bool = False) -> None:
        """Open or create a store. Read-only connections let readers (the MCP server) run
        while another process loads, since DuckDB allows one writer at a time."""
        path = Path(path)
        if read_only:
            if not path.exists():
                raise FileNotFoundError(f"no store at {path}; run `ripple load` first")
            self._con = duckdb.connect(str(path), read_only=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(path))
        self._con.execute(SCHEMA)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._con.close()

    def load(
        self, directory: Path, now: datetime | None = None, source: str = "seed"
    ) -> LoadReport:
        """Validate one source directory and record it as that source's current state."""
        return self.load_sources({source: directory}, now)

    def load_sources(self, sources: dict[str, Path], now: datetime | None = None) -> LoadReport:
        """Validate source directories together, then record each as that source's current
        state. Rows of sources not given are left untouched (D40)."""
        now = now or datetime.now(UTC)
        seed = load_sources(sources)
        errors = [p for p in validate(seed, today=now.date()) if p.severity == "error"]
        if errors:
            raise SeedInvalidError(errors)
        stamp = _naive_utc(now)
        totals = [0, 0, 0]
        self._con.begin()
        try:
            for source in sources:
                nodes = [n.value for n in seed.nodes if n.source == source]
                edges = [e.value for e in seed.edges if e.source == source]
                for counts in (
                    self._load_nodes(nodes, stamp, source),
                    self._load_edges(edges, stamp, source),
                ):
                    totals = [a + b for a, b in zip(totals, counts, strict=True)]
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise
        return LoadReport(*totals)

    def snapshot(self, as_of: date, known_at: date | None = None) -> Snapshot:
        """The world on `as_of`, as the store knew it at the end of `known_at` (UTC).

        `known_at` defaults to `as_of`, which is what a backtest needs: no later knowledge.
        """
        cutoff = datetime.combine((known_at or as_of) + timedelta(days=1), time())
        known = "recorded_at < $cutoff AND (superseded_at IS NULL OR superseded_at >= $cutoff)"
        node_rows = self._con.execute(
            f"SELECT data, source FROM nodes WHERE {known}", {"cutoff": cutoff}
        ).fetchall()
        nodes: dict[str, Node] = {}
        for data, _ in sorted(node_rows, key=lambda row: node_precedence(row[1])):
            node = Node.model_validate_json(data)
            nodes.setdefault(node.id, node)

        edge_rows = self._con.execute(
            f"""
            SELECT {EDGE_COLUMNS} FROM edges
            WHERE {known}
              AND valid_from <= $as_of AND (valid_to IS NULL OR valid_to > $as_of)
            ORDER BY src, rel, dst
            """,
            {"cutoff": cutoff, "as_of": as_of},
        ).fetchall()
        evidence = self._evidence_for([r[0] for r in edge_rows])
        rows = [(row[-1], _edge_from_row(row, evidence.get(row[0], []))) for row in edge_rows]
        winners, losers = consolidate(rows)
        return Snapshot(
            as_of=as_of,
            nodes=nodes,
            edges=[edge for _, edge in winners],
            sources={edge.id: source for source, edge in winners},
            alternatives={edge.id: losers[edge.key] for _, edge in winners if edge.key in losers},
        )

    def row_counts(self) -> dict[str, int]:
        counts = {}
        for table in ("nodes", "edges", "evidence"):
            row = self._con.execute(f"SELECT count(*) FROM {table}").fetchone()
            counts[table] = row[0] if row else 0
        return counts

    def _load_nodes(self, nodes: list[Node], stamp: datetime, source: str) -> tuple[int, int, int]:
        current = dict(
            self._con.execute(
                "SELECT id, content_hash FROM nodes WHERE superseded_at IS NULL AND source = ?",
                [source],
            ).fetchall()
        )
        inserted = superseded = unchanged = 0
        for node in nodes:
            digest = _hash(node.model_dump(mode="json"))
            old = current.pop(node.id, None)
            if old == digest:
                unchanged += 1
                continue
            if old is not None:
                self._close_node(node.id, stamp, source)
                superseded += 1
            self._con.execute(
                "INSERT INTO nodes (id, type, label, data, content_hash, recorded_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [node.id, node.type, node.label, node.model_dump_json(), digest, stamp, source],
            )
            inserted += 1
        for node_id in current:
            self._close_node(node_id, stamp, source)
            superseded += 1
        return inserted, superseded, unchanged

    def _close_node(self, node_id: str, stamp: datetime, source: str) -> None:
        self._con.execute(
            "UPDATE nodes SET superseded_at = ? "
            "WHERE id = ? AND source = ? AND superseded_at IS NULL",
            [stamp, node_id, source],
        )

    def _load_edges(self, edges: list[Edge], stamp: datetime, source: str) -> tuple[int, int, int]:
        rows = self._con.execute(
            "SELECT src, rel, dst, valid_from, row_id, content_hash FROM edges "
            "WHERE superseded_at IS NULL AND source = ?",
            [source],
        ).fetchall()
        current: dict[RowKey, tuple[int, str]] = {
            (src, rel, dst, valid_from): (row_id, digest)
            for src, rel, dst, valid_from, row_id, digest in rows
        }
        inserted = superseded = unchanged = 0
        for edge in edges:
            digest = edge_hash(edge)
            old = current.pop((*edge.key, edge.valid_from), None)
            if old is not None and old[1] == digest:
                unchanged += 1
                continue
            if old is not None:
                self._close_edge(old[0], stamp)
                superseded += 1
            self._insert_edge(edge, digest, stamp, source)
            inserted += 1
        for row_id, _ in current.values():
            self._close_edge(row_id, stamp)
            superseded += 1
        return inserted, superseded, unchanged

    def _close_edge(self, row_id: int, stamp: datetime) -> None:
        self._con.execute("UPDATE edges SET superseded_at = ? WHERE row_id = ?", [stamp, row_id])

    def _insert_edge(self, edge: Edge, digest: str, stamp: datetime, source: str) -> None:
        result = self._con.execute(
            """
            INSERT INTO edges (edge_id, src, rel, dst, weight, weight_source, polarity,
                               confidence, valid_from, valid_to, content_hash, recorded_at,
                               source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING row_id
            """,
            [
                edge.id,
                edge.src,
                edge.rel,
                edge.dst,
                edge.weight,
                edge.weight_source,
                edge.polarity,
                edge.confidence,
                edge.valid_from,
                edge.valid_to,
                digest,
                stamp,
                source,
            ],
        ).fetchone()
        assert result is not None
        for position, item in enumerate(edge.evidence):
            self._con.execute(
                "INSERT INTO evidence (edge_row_id, position, url, note, accessed, verified, span) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    result[0],
                    position,
                    item.url,
                    item.note,
                    item.accessed,
                    item.verified,
                    item.span.model_dump_json() if item.span else None,
                ],
            )

    def _evidence_for(self, row_ids: list[int]) -> dict[int, list[Evidence]]:
        if not row_ids:
            return {}
        rows = self._con.execute(
            "SELECT edge_row_id, url, note, accessed, verified, span FROM evidence "
            "WHERE list_contains($ids, edge_row_id) ORDER BY edge_row_id, position",
            {"ids": row_ids},
        ).fetchall()
        evidence: dict[int, list[Evidence]] = {}
        for row_id, url, note, accessed, verified, span in rows:
            evidence.setdefault(row_id, []).append(
                Evidence(
                    url=url,
                    note=note or "",
                    accessed=accessed,
                    verified=bool(verified),
                    span=Span.model_validate_json(span) if span else None,
                )
            )
        return evidence


def _edge_from_row(row: tuple[Any, ...], evidence: list[Evidence]) -> Edge:
    _, src, rel, dst, weight, weight_source, polarity, confidence, valid_from, valid_to = row[:10]
    return Edge(
        src=src,
        rel=rel,
        dst=dst,
        weight=weight,
        weight_source=weight_source,
        polarity=polarity,
        confidence=confidence,
        valid_from=valid_from,
        valid_to=valid_to,
        evidence=evidence,
    )


def edge_hash(edge: Edge) -> str:
    """Content hash of an edge. An absent span is left out, so edges written before spans
    existed keep their hash and are not superseded by the field's arrival."""
    data = edge.model_dump(mode="json")
    for item in data["evidence"]:
        if item.get("span") is None:
            item.pop("span", None)
    return _hash(data)


def _hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("load time must be timezone-aware")
    return moment.astimezone(UTC).replace(tzinfo=None)
