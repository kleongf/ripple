"""Text extraction from filing sections, with span verification (Phase 1, M19, D65, D72).

One Codex call per section returns REQUIRES proposals, disclosed revenue shares, PRODUCES and
SUPPLIES statements (evidence only) and new-product proposals, each with a short verbatim
quote. Verification keeps an item only if its quote is found in the cached section text, the
text around it names the item's endpoints, and a disclosed number appears in the quote. The
quote itself is then discarded: only section offsets are stored (CLAUDE.md, D72).
"""

import hashlib
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ripple.growth import locate_quote
from ripple.model import BUCKETS as SHARE_BUCKETS
from ripple.model import Node
from ripple.search import search_entities
from ripple.xbrl_edges import company_name

BUCKETS = ("minor", "major", "dominant")
BASES = ("product", "end market", "customer", "segment")
WINDOW = 300  # characters around a quote in which endpoints must be named
LLM_CONFIDENCE = 0.5  # REQUIRES links from text (D69)
MAX_EVIDENCE = 3  # evidence entries kept per REQUIRES link
MATCH_SCORE = 0.9
STOP_WORDS = {
    "and", "the", "for", "data", "center", "centers", "system", "systems", "equipment",
    "product", "products", "non", "high", "other", "services",
}  # fmt: skip

INSTRUCTIONS = """You extract structured facts from one section of a company's annual report.
Reply only with JSON matching the schema. Do not call tools. Use only product IDs from the list.
- requires: a product that needs another product as an input or component, as the text states
  or clearly implies ("our GPUs use HBM"). bucket: how much of the input's total demand comes
  from that product (minor < 20%, major 20-60%, dominant > 60%), with a one-line reason in your
  own words.
- disclosed: a share of this company's revenue the text states as a number (by product, end
  market, customer or segment), as a decimal, with its period and the products it concerns.
- produces: products this company makes or sells. supplies: a named supplier-customer pair.
- new_products: products relevant to an AI data center value chain that the list lacks.
Every item needs a verbatim quote of at most 30 words from the text that contains it.
Descriptions and reasons must be in your own words, not copied."""


@dataclass
class RequiresProposal:
    id: str
    product: str
    input: str
    bucket: str
    reason: str
    section: str
    start: int
    end: int
    status: str = "pending"
    reviewer: str | None = None
    judge: bool | None = None  # the judge's verdict, kept after review


@dataclass
class DisclosedNumber:
    id: str
    description: str
    share: float
    period: str
    basis: str
    products: list[str]
    section: str
    start: int
    end: int
    status: str = "pending"
    reviewer: str | None = None
    judge: bool | None = None  # the judge's verdict, kept after review


@dataclass
class ProducesEvidence:
    id: str
    product: str
    section: str
    start: int
    end: int


@dataclass
class SuppliesEvidence:
    id: str
    supplier: str
    customer: str
    product: str | None
    section: str
    start: int
    end: int


@dataclass
class NewProduct:
    id: str
    label: str
    reason: str
    section: str
    start: int
    end: int


@dataclass
class FilerExtraction:
    node: str
    form: str
    requires: list[RequiresProposal] = field(default_factory=list)
    disclosed: list[DisclosedNumber] = field(default_factory=list)
    produces: list[ProducesEvidence] = field(default_factory=list)
    supplies: list[SuppliesEvidence] = field(default_factory=list)
    new_products: list[NewProduct] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    def add(self, other: "FilerExtraction") -> None:
        for name in ("requires", "disclosed", "produces", "supplies", "new_products"):
            getattr(self, name).extend(getattr(other, name))
        for kind, count in other.dropped.items():
            self.dropped[kind] = self.dropped.get(kind, 0) + count


def keywords(node: Node) -> set[str]:
    words = set()
    for phrase in [node.label, *node.aliases]:
        for word in re.findall(r"[a-z0-9]+", phrase.lower()):
            if len(word) >= 3 and word not in STOP_WORDS:
                words.add(word[:-1] if word.endswith("s") and len(word) > 4 else word)
    return words


def mentions_node(window: str, node: Node) -> bool:
    return any(re.search(rf"\b{re.escape(word)}", window, re.I) for word in keywords(node))


def number_in_quote(quote: str, share: float) -> bool:
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(%|percent|per cent)", quote, re.I):
        if abs(float(match.group(1)) - share * 100) <= 0.5:
            return True
    return False


def extraction_schema(taxonomy: list[Node]) -> dict[str, Any]:
    ids = [n.id for n in taxonomy]

    def obj(props: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(props),
                "properties": props,
            },
        }

    quote = {"type": "string"}
    product = {"type": "string", "enum": ids}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["requires", "disclosed", "produces", "supplies", "new_products"],
        "properties": {
            "requires": obj({"product": product, "input": product,
                             "bucket": {"type": "string", "enum": list(BUCKETS)},
                             "reason": {"type": "string"}, "quote": quote}),
            "disclosed": obj({"description": {"type": "string"}, "share": {"type": "number"},
                              "period": {"type": "string"},
                              "basis": {"type": "string", "enum": list(BASES)},
                              "products": {"type": "array", "items": product}, "quote": quote}),
            "produces": obj({"product": product, "quote": quote}),
            "supplies": obj({"supplier": {"type": "string"}, "customer": {"type": "string"},
                             "product": {"type": ["string", "null"]}, "quote": quote}),
            "new_products": obj({"label": {"type": "string"}, "reason": {"type": "string"},
                                 "quote": quote}),
        },
    }  # fmt: skip


def extraction_prompt(
    label: str, node: str, form: str, section: str, text: str, taxonomy: list[Node]
) -> str:
    products = "\n".join(
        f"- {n.id}: {n.label}" + (f" (also: {', '.join(n.aliases)})" if n.aliases else "")
        for n in taxonomy
    )
    return f"""Company: {label} ({node}), {form}, section {section}, text below.

Product taxonomy (use only these IDs):
{products}

Section text:
{text}
"""


def verify_answer(
    node: str,
    form: str,
    section: str,
    section_start: int,
    text: str,
    answer: dict[str, Any],
    taxonomy: list[Node],
    companies: list[Node],
) -> FilerExtraction:
    """Keep the answer's items whose quote and endpoints check out; count the rest as dropped."""
    by_id = {n.id: n for n in taxonomy}
    company_index = {c.id: c for c in companies}
    result = FilerExtraction(node=node, form=form)

    def locate(item: dict[str, Any], kind: str) -> tuple[int, int, str] | None:
        located = locate_quote(text, item.get("quote", ""))
        if located is None:
            result.dropped[kind] = result.dropped.get(kind, 0) + 1
            return None
        window = text[max(0, located[0] - WINDOW) : located[1] + WINDOW]
        return located[0], located[1], window

    def drop(kind: str) -> None:
        result.dropped[kind] = result.dropped.get(kind, 0) + 1

    def make_id(kind: str, *parts: Any) -> str:
        raw = "|".join([node, kind, section, *map(str, parts)])
        return f"{kind[:3]}-{hashlib.sha256(raw.encode()).hexdigest()[:10]}"

    for item in answer.get("requires", []):
        found = locate(item, "requires")
        if found is None:
            continue
        start, end, window = found
        product, needed = by_id.get(item["product"]), by_id.get(item["input"])
        if (
            product is None
            or needed is None
            or product.id == needed.id
            or not mentions_node(window, product)
            or not mentions_node(window, needed)
        ):
            drop("requires")
            continue
        result.requires.append(
            RequiresProposal(
                make_id("requires", product.id, needed.id, start),
                product.id,
                needed.id,
                item["bucket"],
                item.get("reason", ""),
                section,
                section_start + start,
                section_start + end,
            )
        )
    for item in answer.get("disclosed", []):
        found = locate(item, "disclosed")
        if found is None:
            continue
        start, end, _ = found
        quote_text = text[start:end]
        share = float(item["share"])
        if not 0 < share <= 1 or not number_in_quote(quote_text, share):
            drop("disclosed")
            continue
        result.disclosed.append(
            DisclosedNumber(
                make_id("disclosed", share, start),
                item.get("description", ""),
                share,
                item.get("period", ""),
                item.get("basis", "product"),
                [p for p in item.get("products", []) if p in by_id],
                section,
                section_start + start,
                section_start + end,
            )
        )
    for item in answer.get("produces", []):
        found = locate(item, "produces")
        if found is None:
            continue
        start, end, window = found
        product = by_id.get(item["product"])
        if product is None or not mentions_node(window, product):
            drop("produces")
            continue
        result.produces.append(
            ProducesEvidence(
                make_id("produces", product.id, start),
                product.id,
                section,
                section_start + start,
                section_start + end,
            )
        )
    for item in answer.get("supplies", []):
        found = locate(item, "supplies")
        if found is None:
            continue
        start, end, _ = found
        supplier = _resolve(company_index, item.get("supplier", ""))
        customer = _resolve(company_index, item.get("customer", ""))
        if supplier is None or customer is None or supplier == customer:
            drop("supplies")
            continue
        product = item.get("product") if item.get("product") in by_id else None
        result.supplies.append(
            SuppliesEvidence(
                make_id("supplies", supplier, customer, start),
                supplier,
                customer,
                product,
                section,
                section_start + start,
                section_start + end,
            )
        )
    for item in answer.get("new_products", []):
        found = locate(item, "new_products")
        if found is None:
            continue
        start, end, _ = found
        result.new_products.append(
            NewProduct(
                make_id("new", item["label"], start),
                item["label"],
                item.get("reason", ""),
                section,
                section_start + start,
                section_start + end,
            )
        )
    return result  # fmt: skip


def _resolve(companies: dict[str, Node], name: str) -> str | None:
    matches = search_entities(companies, company_name(name), type="company", limit=1)
    return matches[0].id if matches and matches[0].score >= MATCH_SCORE else None


# Files: data/review-queue/extract/<company>.yaml

ITEM_TYPES = {
    "requires": RequiresProposal,
    "disclosed": DisclosedNumber,
    "produces": ProducesEvidence,
    "supplies": SuppliesEvidence,
    "new_products": NewProduct,
}


def keep_review(new: FilerExtraction, old: FilerExtraction) -> None:
    """Carry status, reviewer and judge verdict over from an earlier file, by item ID."""
    for name in ("requires", "disclosed"):
        earlier = {item.id: item for item in getattr(old, name)}
        for item in getattr(new, name):
            if item.id in earlier:
                before = earlier[item.id]
                item.status, item.reviewer, item.judge = (
                    before.status,
                    before.reviewer,
                    before.judge,
                )


def save_extraction(path: Path, extraction: FilerExtraction) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Text extraction (docs/phase-1.md, M19). Offsets point into the cached filing text;\n"
        "# no filing text is stored here (D72).\n"
    )
    path.write_text(header + yaml.safe_dump(asdict(extraction), sort_keys=False))


def load_extraction(path: Path) -> FilerExtraction:
    data = yaml.safe_load(path.read_text())
    for name, kind in ITEM_TYPES.items():
        allowed = {f.name for f in fields(kind)}
        data[name] = [
            kind(**{k: v for k, v in item.items() if k in allowed}) for item in data[name]
        ]
    return FilerExtraction(**data)


# REQUIRES edges for the llm source (M20, D69)


@dataclass(frozen=True)
class FilerSource:
    url: str
    filed: date


def requires_edges(
    extractions: list[FilerExtraction],
    sources: dict[str, FilerSource],
    existing: set[tuple[str, str]],
    input_totals: dict[str, float],
    accessed: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One REQUIRES edge per accepted (product, input) link, at the most common proposed
    bucket (the smaller on a tie) and confidence 0.5. A link already in the graph is skipped,
    and so is one that would push the demand shares into its input over 1 (rule E7)."""
    grouped: dict[tuple[str, str], list[tuple[FilerExtraction, RequiresProposal]]] = {}
    for extraction in extractions:
        for proposal in extraction.requires:
            if proposal.status == "accepted":
                key = (proposal.product, proposal.input)
                grouped.setdefault(key, []).append((extraction, proposal))
    totals = dict(input_totals)
    edges: list[dict[str, Any]] = []
    skipped: list[str] = []
    for (product, needed), found in sorted(grouped.items()):
        label = f"{product} REQUIRES {needed}"
        if (product, needed) in existing:
            skipped.append(f"{label}: already in the graph")
            continue
        counts = Counter(p.bucket for _, p in found)
        bucket = max(
            SHARE_BUCKETS["demand"], key=lambda b: (counts[b], -SHARE_BUCKETS["demand"][b])
        )
        weight = SHARE_BUCKETS["demand"][bucket]
        if totals.get(needed, 0.0) + weight > 1 + 1e-9:
            skipped.append(f"{label}: shares into the input would exceed 1")
            continue
        totals[needed] = totals.get(needed, 0.0) + weight
        evidence = [
            {
                "url": sources[x.node].url,
                "note": f"{x.node} {x.form} item {p.section}: {p.reason}"[:300],
                "accessed": accessed,
                "span": {"section": p.section, "start": p.start, "end": p.end},
            }
            for x, p in found[:MAX_EVIDENCE]
        ]
        edges.append(
            {
                "src": product,
                "rel": "REQUIRES",
                "dst": needed,
                "weight": weight,
                "weight_source": "bucket",
                "polarity": 1,
                "confidence": LLM_CONFIDENCE,
                "valid_from": min(sources[x.node].filed for x, _ in found),
                "evidence": evidence,
            }
        )
    return edges, skipped


def requires_recall(
    seed_links: set[tuple[str, str]], extractions: list[FilerExtraction]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Seed REQUIRES links that extraction proposed (whatever the review said), and those it
    missed: how much of the hand-built structure the text pass rediscovers (D73)."""
    proposed = {(p.product, p.input) for x in extractions for p in x.requires}
    return seed_links & proposed, seed_links - proposed
