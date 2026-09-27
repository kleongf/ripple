"""Validation rules for seed data. Rule codes match docs/phase-0.md and docs/phase-1.md
(E1-E13, W1-W9)."""

import re
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path

from ripple.load import Seed
from ripple.model import (
    BUCKETS,
    DEFAULT_MIN_CONFIDENCE,
    THEME_KINDS,
    Edge,
    Located,
    Node,
    Problem,
    bucket_quantity,
    bucket_range,
    demand_flow,
    node_precedence,
    rank_rows,
)

ID_PATTERN = re.compile(r"^[a-z]+/[a-z0-9]+(?:-[a-z0-9]+)*$")
PRODUCT_TYPES = frozenset({"product", "material"})
COMPANY = frozenset({"company"})

# Allowed (source types, destination types) per relation. CONSTRAINS needs policy nodes, which
# do not exist yet. Region nodes (Phase 1, D43) are stored but not traversed.
ALLOWED_ENDPOINTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "DRIVES": (frozenset({"theme"}), PRODUCT_TYPES),
    "REQUIRES": (PRODUCT_TYPES, PRODUCT_TYPES),
    "PRODUCES": (COMPANY, PRODUCT_TYPES),
    "SUPPLIES": (COMPANY, COMPANY),
    "SUBSTITUTES": (PRODUCT_TYPES, PRODUCT_TYPES),
    "COMPETES_WITH": (COMPANY, COMPANY),
    "SUBSIDIARY_OF": (COMPANY, COMPANY),
    "EXPOSED_TO": (COMPANY, frozenset({"region"})),
}
PRODUCT_PATH_HOPS = 3  # W9: REQUIRES steps from a customer's product to a supplier's

SHARE_LIMIT = 1.0 + 1e-9
MAX_NOTE_LENGTH = 200

LocatedEdge = Located[Edge]


def validate(
    seed: Seed, today: date, min_confidence: float = DEFAULT_MIN_CONFIDENCE
) -> list[Problem]:
    """Return every error and warning, errors first."""
    nodes = _first_by_id(seed.nodes)
    problems = [
        *seed.problems,
        *_check_nodes(seed.nodes),
        *_check_edges(seed.edges, nodes),
        *_check_share_shift(seed.edges, nodes),
        *_check_over_time(seed.edges, nodes),
        *_check_structure_warnings(seed.edges, nodes, min_confidence),
        *_check_evidence_warnings(seed.edges, today),
        *_check_supplies_overlap(seed.edges, min_confidence),
    ]
    unique = list(dict.fromkeys(problems))
    return [p for p in unique if p.severity == "error"] + [
        p for p in unique if p.severity == "warning"
    ]


def _first_by_id(nodes: list[Located[Node]]) -> dict[str, Located[Node]]:
    """One node per ID: the first from the most trusted source (D40)."""
    by_id: dict[str, Located[Node]] = {}
    for located in sorted(nodes, key=lambda n: node_precedence(n.source)):
        by_id.setdefault(located.value.id, located)
    return by_id


def _error(rule: str, file: Path | None, record: str, message: str) -> Problem:
    return Problem("error", rule, file, record, message)


def _warning(rule: str, file: Path | None, record: str, message: str) -> Problem:
    return Problem("warning", rule, file, record, message)


def _node_file(nodes: dict[str, Located[Node]], node_id: str) -> Path | None:
    located = nodes.get(node_id)
    return located.file if located else None


# Per-node rules: E1, E13.


def _check_nodes(nodes: list[Located[Node]]) -> Iterator[Problem]:
    seen: set[tuple[str, str]] = set()
    for located in nodes:
        node, file = located.value, located.file
        if (located.source, node.id) in seen:
            yield _error("E1", file, node.id, "duplicate node ID")
        seen.add((located.source, node.id))
        if not ID_PATTERN.match(node.id):
            yield _error("E1", file, node.id, "ID must be <type>/<slug>, lowercase with hyphens")
        elif node.id.split("/")[0] != node.type:
            yield _error("E1", file, node.id, f"ID prefix does not match type '{node.type}'")

        if node.type == "theme" and node.kind not in THEME_KINDS:
            yield _error("E13", file, node.id, f"theme kind must be one of {THEME_KINDS}")
        elif node.type != "theme" and node.kind is not None:
            yield _error("E13", file, node.id, "only themes have a kind")


# Per-edge rules: E2, E3, E4, E5, E8, E9.


def _check_edges(edges: list[LocatedEdge], nodes: dict[str, Located[Node]]) -> Iterator[Problem]:
    for located in edges:
        edge, file = located.value, located.file
        yield from _check_endpoints(edge, file, nodes)
        yield from _check_ranges(edge, file)
        yield from _check_evidence(edge, file)
        if edge.valid_to is not None and edge.valid_to <= edge.valid_from:
            yield _error("E9", file, edge.label, "valid_to must be after valid_from")
    yield from _check_duplicate_keys(edges)


def _check_endpoints(edge: Edge, file: Path, nodes: dict[str, Located[Node]]) -> Iterator[Problem]:
    missing = [end for end in (edge.src, edge.dst) if end not in nodes]
    for end in missing:
        yield _error("E2", file, edge.label, f"endpoint {end} does not exist")
    if missing:
        return
    allowed = ALLOWED_ENDPOINTS.get(edge.rel)
    if allowed is None:
        yield _error("E3", file, edge.label, f"{edge.rel} is not usable in Phase 0")
        return
    src_type, dst_type = nodes[edge.src].value.type, nodes[edge.dst].value.type
    if src_type not in allowed[0] or dst_type not in allowed[1]:
        yield _error("E3", file, edge.label, f"{edge.rel} does not allow {src_type} -> {dst_type}")


def _check_ranges(edge: Edge, file: Path) -> Iterator[Problem]:
    if not 0 < edge.weight <= 1:
        yield _error("E4", file, edge.label, f"weight {edge.weight} is not in (0, 1]")
    if not 0 < edge.confidence <= 1:
        yield _error("E4", file, edge.label, f"confidence {edge.confidence} is not in (0, 1]")
    if edge.polarity not in (1, -1):
        yield _error("E4", file, edge.label, f"polarity {edge.polarity} is not 1 or -1")
    quantity = bucket_quantity(edge.rel)
    if edge.weight_source == "bucket" and quantity and bucket_range(quantity, edge.weight) is None:
        names = ", ".join(f"{k} {v}" for k, v in BUCKETS[quantity].items())
        message = f"bucket weight {edge.weight} is not a {quantity} bucket ({names})"
        yield _warning("W8", file, edge.label, message)


def _check_evidence(edge: Edge, file: Path) -> Iterator[Problem]:
    if not edge.evidence:
        yield _error("E5", file, edge.label, "no evidence")
    for item in edge.evidence:
        if not item.url or item.accessed is None:
            yield _error("E5", file, edge.label, "evidence item needs url and accessed")


def _check_duplicate_keys(edges: list[LocatedEdge]) -> Iterator[Problem]:
    # Per source: the same key in two sources is expected, and consolidation picks one (D40).
    by_key: dict[tuple[str, str, str, str], list[LocatedEdge]] = defaultdict(list)
    for located in edges:
        by_key[(located.source, *located.value.key)].append(located)
    for group in by_key.values():
        for i, first in enumerate(group):
            for second in group[i + 1 :]:
                if _overlaps(first.value, second.value):
                    yield _error(
                        "E8",
                        second.file,
                        second.value.label,
                        "duplicate edge with overlapping valid time",
                    )


def _overlaps(a: Edge, b: Edge) -> bool:
    a_end = a.valid_to or date.max
    b_end = b.valid_to or date.max
    return a.valid_from < b_end and b.valid_from < a_end


# Theme rule E11.


def _check_share_shift(
    edges: list[LocatedEdge], nodes: dict[str, Located[Node]]
) -> Iterator[Problem]:
    polarities: dict[str, set[int]] = defaultdict(set)
    for located in edges:
        if located.value.rel == "DRIVES":
            polarities[located.value.src].add(located.value.polarity)
    for node_id, located in nodes.items():
        if located.value.kind != "share_shift":
            continue
        signs = polarities[node_id]
        if not ({1, -1} <= signs):
            yield _error(
                "E11",
                located.file,
                node_id,
                "a share_shift theme needs a positive and a negative DRIVES edge",
            )


# Rules that depend on which edges are valid at the same time: E6, E7, E10, E12, W5.
# Sums and edge sets only grow at a valid_from date, so checking each valid_from is enough.


def _check_over_time(
    edges: list[LocatedEdge], nodes: dict[str, Located[Node]]
) -> Iterator[Problem]:
    reported: set[tuple[str, ...]] = set()
    for day in sorted({located.value.valid_from for located in edges}):
        active = _consolidated([located for located in edges if located.value.active_on(day)])
        for key, problem in _checks_at(active, nodes, day):
            if key not in reported:
                reported.add(key)
                yield problem


def _consolidated(active: list[LocatedEdge]) -> list[LocatedEdge]:
    """One edge per key, ranked as the store ranks it (D40, D63)."""
    groups: dict[tuple[str, str, str], list[LocatedEdge]] = defaultdict(list)
    for located in active:
        groups[located.value.key].append(located)
    chosen = []
    for group in groups.values():
        (_, best), *_ = rank_rows([(located.source, located.value) for located in group])
        chosen.append(next(located for located in group if located.value is best))
    return chosen


def _checks_at(
    active: list[LocatedEdge], nodes: dict[str, Located[Node]], day: date
) -> Iterator[tuple[tuple[str, ...], Problem]]:
    for rule, rel, pick in (("E6", "PRODUCES", "src"), ("E7", "REQUIRES", "dst")):
        sums: dict[str, float] = defaultdict(float)
        for located in active:
            if located.value.rel == rel:
                sums[getattr(located.value, pick)] += located.value.weight
        for node_id, total in sums.items():
            if total > SHARE_LIMIT:
                message = f"{rel} weights sum to {total:.2f} on {day}, above 1.0"
                yield (rule, node_id), _error(rule, _node_file(nodes, node_id), node_id, message)

    drives: dict[str, list[LocatedEdge]] = defaultdict(list)
    for located in active:
        if located.value.rel == "DRIVES":
            drives[located.value.dst].append(located)
    for product, group in drives.items():
        total = sum(located.value.weight for located in group)
        if len({located.value.src for located in group}) > 1 and total > SHARE_LIMIT:
            message = (
                f"DRIVES weights from several themes sum to {total:.2f}; allowed, but do not "
                "add exposures across themes (D9)"
            )
            yield ("W5", product), _warning("W5", _node_file(nodes, product), product, message)

    yield from _double_driven(active, nodes)
    yield from _shared_cells(active)


def _double_driven(
    active: list[LocatedEdge], nodes: dict[str, Located[Node]]
) -> Iterator[tuple[tuple[str, ...], Problem]]:
    """E10: a volume theme must not drive a node it already reaches through REQUIRES."""
    requires: dict[str, list[str]] = defaultdict(list)
    targets: dict[str, list[LocatedEdge]] = defaultdict(list)
    for located in active:
        edge = located.value
        if edge.rel == "REQUIRES":
            requires[edge.src].append(edge.dst)
        elif edge.rel == "DRIVES":
            targets[edge.src].append(located)
    for theme, drive_edges in targets.items():
        theme_node = nodes.get(theme)
        if theme_node is None or theme_node.value.kind != "volume":
            continue
        by_target = {located.value.dst: located for located in drive_edges}
        for start in by_target:
            for reached in _reachable(start, requires) & (by_target.keys() - {start}):
                message = (
                    f"drives {reached} directly and also through {start} REQUIRES; "
                    "the same demand would count twice (D7)"
                )
                located = by_target[reached]
                yield ("E10", theme, reached), _error("E10", located.file, theme, message)


def _shared_cells(active: list[LocatedEdge]) -> Iterator[tuple[tuple[str, ...], Problem]]:
    """E12: two different edges must not map to the same demand-flow cell."""
    cells: dict[tuple[str, str], list[LocatedEdge]] = defaultdict(list)
    for located in active:
        cell = demand_flow(located.value)
        if cell is not None:
            cells[cell].append(located)
    for cell, group in cells.items():
        keys = {located.value.key for located in group}
        if len(keys) > 1:
            names = ", ".join(sorted(" ".join(key) for key in keys))
            message = f"edges map to the same demand-flow cell {cell[0]} -> {cell[1]}: {names}"
            last = group[-1]
            yield ("E12", *cell), _error("E12", last.file, last.value.label, message)


def _reachable(start: str, adjacency: dict[str, list[str]]) -> set[str]:
    """Nodes reachable from start in one or more steps."""
    seen: set[str] = set()
    queue = deque(adjacency.get(start, []))
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(adjacency.get(current, []))
    return seen


# Warnings on graph structure: W1, W2, W3, W4.


def _check_structure_warnings(
    edges: list[LocatedEdge], nodes: dict[str, Located[Node]], min_confidence: float
) -> Iterator[Problem]:
    produced = {e.value.dst for e in edges if e.value.rel == "PRODUCES"}
    producers = {e.value.src for e in edges if e.value.rel == "PRODUCES"}
    themes = [node_id for node_id, n in nodes.items() if n.value.type == "theme"]
    reach_all = _reachable_from(themes, (e.value for e in edges))
    reach_confident = _reachable_from(
        themes, (e.value for e in edges if e.value.confidence >= min_confidence)
    )

    for node_id, located in nodes.items():
        node_type = located.value.type
        if node_type in PRODUCT_TYPES and node_id not in produced:
            yield _warning("W1", located.file, node_id, "no company PRODUCES this")
        if node_type == "company" and node_id not in producers:
            yield _warning("W2", located.file, node_id, "company has no PRODUCES edge")
        if node_type in ("theme", "region"):
            continue
        if node_id not in reach_all:
            yield _warning("W3", located.file, node_id, "cannot be reached from any theme")
        elif node_type == "company" and node_id not in reach_confident:
            message = f"reachable only through edges with confidence below {min_confidence}"
            yield _warning("W4", located.file, node_id, message)


def product_path(
    edges: Iterable[Edge], customer: str, supplier: str, max_hops: int = PRODUCT_PATH_HOPS
) -> bool:
    """Whether a product the customer makes requires, within max_hops REQUIRES steps, a product
    the supplier makes. Then a SUPPLIES edge would count the same demand twice (D4)."""
    edges = list(edges)
    made: dict[str, set[str]] = defaultdict(set)
    requires: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge.rel == "PRODUCES":
            made[edge.src].add(edge.dst)
        elif edge.rel == "REQUIRES":
            requires[edge.src].append(edge.dst)
    frontier = set(made[customer])
    seen = set(frontier)
    for _ in range(max_hops):
        frontier = {n for product in frontier for n in requires[product]} - seen
        if frontier & made[supplier]:
            return True
        seen |= frontier
    return False


def _check_supplies_overlap(edges: list[LocatedEdge], min_confidence: float) -> Iterator[Problem]:
    """W9: a propagating SUPPLIES edge already carried by the product layer."""
    plain = [located.value for located in edges]
    for located in edges:
        edge = located.value
        if edge.rel != "SUPPLIES" or edge.confidence < min_confidence:
            continue
        if product_path(plain, customer=edge.dst, supplier=edge.src):
            message = (
                f"{edge.dst} already reaches {edge.src} through products it requires; the "
                "SUPPLIES edge would count that demand twice (D4). Keep it below min_confidence."
            )
            yield _warning("W9", located.file, edge.label, message)


def _reachable_from(starts: list[str], edges: Iterable[Edge]) -> set[str]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        cell = demand_flow(edge)
        if cell is not None:
            adjacency[cell[0]].append(cell[1])
    reached: set[str] = set()
    for start in starts:
        reached |= _reachable(start, adjacency)
    return reached


# Warnings on evidence: W6, W7.


def _check_evidence_warnings(edges: list[LocatedEdge], today: date) -> Iterator[Problem]:
    for located in edges:
        edge, file = located.value, located.file
        for item in edge.evidence:
            if item.accessed is not None and item.accessed > today:
                yield _warning(
                    "W6", file, edge.label, f"evidence accessed {item.accessed} is future"
                )
            if len(item.note) > MAX_NOTE_LENGTH:
                message = f"evidence note is {len(item.note)} characters; was it copied?"
                yield _warning("W7", file, edge.label, message)
