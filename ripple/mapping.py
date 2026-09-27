"""Map XBRL revenue lines onto product nodes, and the review queue (Phase 1, M13).

Codex reads one company's revenue breakdown and the product taxonomy, and says which products
each revenue line sells and what fraction of the line each accounts for. The answer is saved
as a pending mapping in the review queue. Accepting it writes PRODUCES edges into the `xbrl`
source, with weight = sum over lines of (line share x fraction).
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from ripple.codex import CodexResult, CodexRunner, default_config
from ripple.model import Node
from ripple.xbrl import Breakdown, RevenueFacts

DEFAULT_QUEUE = Path("data/review-queue/mappings")
DEFAULT_XBRL = Path("data/xbrl")
MIN_WEIGHT = 0.001  # smaller products are not worth an edge
MAX_NOTE = 200

INSTRUCTIONS = """You map a company's reported revenue lines onto a fixed product taxonomy.
Reply only with JSON matching the schema. Do not call tools.
For each revenue line, list the taxonomy products the line sells and the fraction of the
line's revenue each accounts for (0 to 1). Fractions for one line sum to at most 1; leave the
rest unassigned when it comes from products outside the taxonomy.
Assign a product only when you are confident a meaningful part of the line is that product.
Leave service, spare-parts and support lines unassigned: they follow the installed base, not
new demand. If a line is mainly a product that belongs in an AI data center value chain but is
missing from the taxonomy, name it in new_product; otherwise new_product is null.
note: one short sentence in your own words explaining the mapping."""


class Runner(Protocol):
    def run(self, prompt: str, schema: dict[str, Any], instructions: str) -> CodexResult: ...


@dataclass
class Assignment:
    product: str
    fraction: float


@dataclass
class LineMapping:
    member: str
    label: str
    share: float
    assignments: list[Assignment] = field(default_factory=list)
    new_product: str | None = None
    note: str = ""


@dataclass
class CompanyMapping:
    node: str
    label: str
    form: str
    filing_url: str
    fiscal_start: date
    fiscal_end: date
    currency: str
    total: float
    kind: str
    axis: str
    complete: bool
    lines: list[LineMapping]
    status: str = "pending"  # pending | accepted | rejected
    reviewer: str | None = None
    reject_reason: str | None = None
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    created: date | None = None


def make_runner(effort: str = "medium") -> Runner:
    return CodexRunner(default_config(effort=effort))


def choose_breakdown(facts: RevenueFacts) -> Breakdown | None:
    """A complete product breakdown, else a complete segment one, else a partial one."""
    order = [
        ("product", True),
        ("segment", True),
        ("product", False),
        ("segment", False),
        ("market", True),
    ]
    for kind, complete in order:
        breakdown = facts.breakdowns.get(kind)
        if breakdown is not None and breakdown.complete == complete and breakdown.lines:
            return breakdown
    return None


def mapping_schema(breakdown: Breakdown, taxonomy: list[Node]) -> dict[str, Any]:
    assignment = {
        "type": "object",
        "additionalProperties": False,
        "required": ["product", "fraction"],
        "properties": {
            "product": {"type": "string", "enum": [n.id for n in taxonomy]},
            "fraction": {"type": "number"},
        },
    }
    line = {
        "type": "object",
        "additionalProperties": False,
        "required": ["member", "assignments", "new_product", "note"],
        "properties": {
            "member": {"type": "string", "enum": [rl.member for rl in breakdown.lines]},
            "assignments": {"type": "array", "items": assignment},
            "new_product": {"type": ["string", "null"]},
            "note": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["lines"],
        "properties": {"lines": {"type": "array", "items": line}},
    }


def mapping_prompt(
    label: str, node: str, facts: RevenueFacts, breakdown: Breakdown, taxonomy: list[Node]
) -> str:
    lines = "\n".join(
        f"- {rl.member}: {rl.label}, {rl.share:.1%} of revenue" for rl in breakdown.lines
    )
    context = []
    for kind, other in facts.breakdowns.items():
        if other is breakdown or kind == "geography":
            continue
        shares = ", ".join(f"{rl.label} {rl.share:.1%}" for rl in other.lines)
        context.append(f"- {kind}: {shares}")
    products = "\n".join(
        f"- {n.id}: {n.label}" + (f" (also: {', '.join(n.aliases)})" if n.aliases else "")
        for n in taxonomy
    )
    other_text = "\n".join(context) or "- none"
    total = f"{facts.total / 1e9:,.2f} bn {facts.currency}"
    return f"""Company: {label} ({node})
Fiscal year {facts.start} to {facts.end}, total revenue {total}.

Revenue lines to map ({breakdown.kind}, {breakdown.axis}):
{lines}

Other breakdowns in the same filing, for context only:
{other_text}

Product taxonomy (use only these IDs):
{products}
"""


def map_company(
    node: str,
    label: str,
    facts: RevenueFacts,
    taxonomy: list[Node],
    runner: Runner,
    filing_url: str,
    form: str,
) -> CompanyMapping:
    breakdown = choose_breakdown(facts)
    if breakdown is None:
        raise ValueError(f"{node}: no revenue breakdown to map")
    result = runner.run(
        mapping_prompt(label, node, facts, breakdown, taxonomy),
        mapping_schema(breakdown, taxonomy),
        INSTRUCTIONS,
    )
    answers = {a["member"]: a for a in result.data.get("lines", [])}
    known = {n.id for n in taxonomy}
    lines = []
    for revenue_line in breakdown.lines:
        answer = answers.get(revenue_line.member, {})
        lines.append(
            LineMapping(
                member=revenue_line.member,
                label=revenue_line.label,
                share=round(revenue_line.share, 6),
                assignments=_normalize(answer.get("assignments", []), known),
                new_product=answer.get("new_product") or None,
                note=answer.get("note", ""),
            )
        )
    config = getattr(runner, "config", None)
    return CompanyMapping(
        node=node,
        label=label,
        form=form,
        filing_url=filing_url,
        fiscal_start=facts.start,
        fiscal_end=facts.end,
        currency=facts.currency,
        total=facts.total,
        kind=breakdown.kind,
        axis=breakdown.axis,
        complete=breakdown.complete,
        lines=lines,
        model=getattr(config, "model", ""),
        usage=result.usage,
        created=datetime.now(UTC).date(),
    )


def _normalize(raw: list[dict[str, Any]], known: set[str]) -> list[Assignment]:
    """Clip fractions to [0, 1], drop unknown products, and scale down if they exceed 1."""
    kept = [
        Assignment(a["product"], min(max(float(a["fraction"]), 0.0), 1.0))
        for a in raw
        if a.get("product") in known
    ]
    kept = [a for a in kept if a.fraction > 0]
    total = sum(a.fraction for a in kept)
    if total > 1:
        kept = [Assignment(a.product, a.fraction / total) for a in kept]
    return kept


def edges_from_mapping(mapping: CompanyMapping, accessed: date) -> list[dict[str, Any]]:
    """PRODUCES edge records (seed YAML format) for the xbrl source."""
    parts: dict[str, list[tuple[LineMapping, float]]] = {}
    for line in mapping.lines:
        for assignment in line.assignments:
            parts.setdefault(assignment.product, []).append((line, assignment.fraction))
    edges = []
    for product, contributions in parts.items():
        weight = round(sum(line.share * fraction for line, fraction in contributions), 4)
        if weight < MIN_WEIGHT:
            continue
        whole = all(fraction == 1.0 for _, fraction in contributions)
        edges.append(
            {
                "src": mapping.node,
                "rel": "PRODUCES",
                "dst": product,
                "weight": weight,
                "weight_source": "filing" if whole else "manual",
                "polarity": 1,
                "confidence": 0.9 if whole else 0.7,
                "valid_from": mapping.fiscal_start,
                "evidence": [
                    {
                        "url": mapping.filing_url,
                        "note": _note(mapping, contributions),
                        "accessed": accessed,
                    }
                ],
            }
        )
    return sorted(edges, key=lambda e: -e["weight"])


def _note(mapping: CompanyMapping, contributions: list[tuple[LineMapping, float]]) -> str:
    terms = " + ".join(
        f"{line.label} {line.share:.1%}" + (f" x{fraction:.2f}" if fraction < 1 else "")
        for line, fraction in contributions
    )
    note = (
        f"{mapping.form} FY{mapping.fiscal_end.year} XBRL: {terms} of "
        f"{mapping.currency} {mapping.total / 1e9:,.1f}bn revenue"
    )
    return note if len(note) <= MAX_NOTE else note[: MAX_NOTE - 3] + "..."


# Queue files


def queue_path(queue: Path, node: str) -> Path:
    return queue / f"{node.split('/', 1)[-1]}.yaml"


def save_mapping(path: Path, mapping: CompanyMapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Revenue-line mapping proposed by Codex (docs/phase-1.md, M13). Edit assignments by\n"
        "# hand if needed, then `ripple review accept`.\n"
    )
    path.write_text(header + yaml.safe_dump(asdict(mapping), sort_keys=False))


def load_mapping(path: Path) -> CompanyMapping:
    data = yaml.safe_load(path.read_text())
    data["lines"] = [
        LineMapping(**{**line, "assignments": [Assignment(**a) for a in line["assignments"]]})
        for line in data["lines"]
    ]
    return CompanyMapping(**data)


def accept_mapping(path: Path, out_dir: Path, reviewer: str, accessed: date) -> Path:
    """Write the mapping's edges into the xbrl source and mark it accepted."""
    mapping = load_mapping(path)
    edges = edges_from_mapping(mapping, accessed)
    out = out_dir / "edges" / f"{mapping.node.split('/', 1)[-1]}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Generated from the {mapping.form} XBRL revenue lines of {mapping.node} by "
        f"`ripple review accept` ({reviewer}).\n# Do not edit: edit the queue file instead.\n"
    )
    out.write_text(header + yaml.safe_dump(edges, sort_keys=False))
    mapping.status = "accepted"
    mapping.reviewer = reviewer
    save_mapping(path, mapping)
    return out


def reject_mapping(path: Path, reviewer: str, reason: str) -> None:
    mapping = load_mapping(path)
    mapping.status = "rejected"
    mapping.reviewer = reviewer
    mapping.reject_reason = reason
    save_mapping(path, mapping)
