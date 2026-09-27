"""Edges read straight from XBRL facts, with no LLM step (Phase 1, M14).

- Geographic revenue becomes `EXPOSED_TO` edges from the company to region nodes. They are
  stored but not traversed (D43).
- Named major customers become `SUPPLIES` edges (supplier = the filer). Anonymous or grouped
  customers are skipped. When the product layer already connects the two companies, the edge
  is stored below the propagation threshold, so the demand is not counted twice (D4, W9).
"""

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from ripple.edgar import Filing
from ripple.model import Node
from ripple.search import search_entities
from ripple.xbrl import RevenueFacts

CATCH_ALL = re.compile(r"\b(other|rest of|non[- ]?u\.?s|outside|excluding|all others?)\b", re.I)
ANONYMOUS = re.compile(
    r"\b(customers?|distributors?|largest|top \w+)\b|^(one|two|three|four|five)\b", re.I
)
SUFFIXES = re.compile(
    r"[,.]?\s+\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|n\.?v|s\.?a|"
    r"ag|se|holdings?|group)\b\.?",
    re.I,
)
MATCH_SCORE = 0.9
COVERED_CONFIDENCE = 0.3  # stored, not propagated (D11)

Record = dict[str, Any]


def region_id(member: str, label: str) -> str | None:
    """`region/<iso>` for country members, `region/<slug>` for named groups, None for
    catch-alls such as "Other countries" or "Rest of Asia"."""
    if CATCH_ALL.search(label):
        return None
    if member.startswith("country:"):
        return f"region/{member.split(':', 1)[1].lower()}"
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return f"region/{slug}" if slug else None


def company_name(label: str) -> str:
    """The name without corporate suffixes: "Dell Inc." -> "Dell"."""
    return SUFFIXES.sub("", label).strip(" ,.")


def is_anonymous(label: str) -> bool:
    return bool(ANONYMOUS.search(label))


def region_edges(
    node: str, facts: RevenueFacts, filing: Filing, accessed: date
) -> tuple[list[Record], list[Record]]:
    """Region nodes and EXPOSED_TO edges from the geography breakdown."""
    breakdown = facts.breakdowns.get("geography")
    if breakdown is None:
        return [], []
    nodes: list[Record] = []
    edges: list[Record] = []
    for line in breakdown.lines:
        rid = region_id(line.member, line.label)
        if rid is None or line.share <= 0:
            continue
        label = _region_label(line.label)
        nodes.append({"id": rid, "type": "region", "label": label})
        note = (
            f"{filing.form} FY{facts.end.year} XBRL: {label} {line.share:.1%} of "
            f"{facts.currency} {facts.total / 1e9:,.1f}bn revenue ({line.member})"
        )
        edges.append(_edge(node, "EXPOSED_TO", rid, line.share, 0.9, facts, filing, note, accessed))
    return nodes, edges


def customer_edges(
    node: str,
    facts: RevenueFacts,
    filing: Filing,
    companies: list[Node],
    has_product_path: Callable[[str, str], bool],
    accessed: date,
) -> tuple[list[Record], dict[str, str]]:
    """SUPPLIES edges to named customers in the graph, and why each other customer was skipped."""
    by_id = {c.id: c for c in companies}
    edges: list[Record] = []
    skipped: dict[str, str] = {}
    for line in facts.customers:
        if is_anonymous(line.label):
            skipped[line.label] = "anonymous"
            continue
        matches = [
            m
            for m in search_entities(by_id, company_name(line.label), type="company", limit=1)
            if m.score >= MATCH_SCORE and m.id != node
        ]
        if not matches:
            skipped[line.label] = "not in the graph"
            continue
        customer = matches[0].id
        covered = has_product_path(customer, node)
        note = (
            f"{filing.form} FY{facts.end.year} XBRL: {line.label} was {line.share:.1%} of revenue"
            + ("; also reached through products, so stored below threshold" if covered else "")
        )
        confidence = COVERED_CONFIDENCE if covered else 0.9
        edges.append(
            _edge(node, "SUPPLIES", customer, line.share, confidence, facts, filing, note, accessed)
        )
    return edges, skipped


def _edge(
    src: str,
    rel: str,
    dst: str,
    share: float,
    confidence: float,
    facts: RevenueFacts,
    filing: Filing,
    note: str,
    accessed: date,
) -> Record:
    return {
        "src": src,
        "rel": rel,
        "dst": dst,
        "weight": round(share, 4),
        "weight_source": "filing",
        "polarity": 1,
        "confidence": confidence,
        "valid_from": facts.start,
        "evidence": [{"url": filing.url, "note": note[:200], "accessed": accessed}],
    }


def _region_label(label: str) -> str:
    # SEC country labels are upper case ("KOREA, REPUBLIC OF"); groups are already readable.
    return label.title() if label.isupper() else label
