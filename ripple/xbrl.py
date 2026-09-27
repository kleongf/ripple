"""Revenue facts from XBRL instance documents (Phase 1, M11).

Reads the facts SEC filings tag: total revenue for the latest fiscal year, and revenue broken
down along one axis at a time (product or service lines, business segments, geography,
markets) plus customer concentration. Works for US-GAAP and IFRS filings, because the
instance format is the same and only concept and axis names differ.
"""

import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations

from ripple.edgar import CachedFiling, read_cached

NS_INSTANCE = "http://www.xbrl.org/2003/instance"
NS_DIMENSIONS = "http://xbrl.org/2006/xbrldi"
NS_LINK = "http://www.xbrl.org/2003/linkbase"
NS_XLINK = "http://www.w3.org/1999/xlink"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
LABEL_ROLE = "http://www.xbrl.org/2003/role/label"

# Total-revenue concepts, most specific first.
REVENUE_CONCEPTS = (
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "ifrs-full:Revenue",
    "ifrs-full:RevenueFromContractsWithCustomers",
)
AXIS_KINDS = {
    "srt:ProductOrServiceAxis": "product",
    "ifrs-full:ProductsAndServicesAxis": "product",
    "us-gaap:StatementBusinessSegmentsAxis": "segment",
    "ifrs-full:SegmentsAxis": "segment",
    "srt:StatementGeographicalAxis": "geography",
    "ifrs-full:GeographicalAreasAxis": "geography",
    "ifrs-full:MarketsOfCustomersAxis": "market",
}
CUSTOMER_AXIS = "srt:MajorCustomersAxis"
CONSOLIDATION_AXIS = "srt:ConsolidationItemsAxis"
OPERATING_SEGMENTS = "us-gaap:OperatingSegmentsMember"
CONCENTRATION = "us-gaap:ConcentrationRiskPercentage1"
BENCHMARK_AXIS = "us-gaap:ConcentrationRiskByBenchmarkAxis"
REVENUE_BENCHMARKS = {
    "us-gaap:RevenueFromContractWithCustomerProductBenchmarkMember",
    "us-gaap:SalesRevenueNetMember",
    "us-gaap:RevenueBenchmarkMember",
}
ANNUAL_DAYS = range(350, 381)
COMPLETE_TOLERANCE = 0.005  # a breakdown is complete when its lines sum to the total within 0.5%
AGGREGATE_TOLERANCE = 0.001  # a line is an aggregate when other lines sum to it within 0.1%
MAX_AGGREGATE_SEARCH = 20  # lines; beyond this, aggregates are not searched for

Dims = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Fact:
    concept: str
    value: float
    start: date | None  # None for instant facts
    end: date
    dims: Dims
    unit: str = ""  # e.g. "USD" or "TWD" for monetary facts, "pure" for ratios


@dataclass(frozen=True)
class RevenueLine:
    member: str
    label: str
    value: float
    share: float


@dataclass(frozen=True)
class Breakdown:
    kind: str
    axis: str
    lines: list[RevenueLine]
    # True when the lines sum to total revenue, so they partition it.
    complete: bool
    # Members removed because other lines add up to them (for example ProductMember).
    dropped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RevenueFacts:
    concept: str
    start: date
    end: date
    total: float
    currency: str
    breakdowns: dict[str, Breakdown]
    customers: list[RevenueLine]


def parse_instance(data: bytes) -> list[Fact]:
    """Numeric facts with their period and explicit dimensions. Text and nil facts are skipped."""
    prefixes: dict[str, str] = {}
    root = None
    for event, item in ET.iterparse(io.BytesIO(data), events=("start-ns", "start")):
        if event == "start-ns":
            prefix, uri = item  # type: ignore[misc]
            prefixes.setdefault(uri, prefix)
        elif root is None:
            root = item
    if root is None:
        return []
    ET.ElementTree(root)  # finish parsing (iterparse built the whole tree)
    contexts = _contexts(root)
    units = _units(root)
    facts: list[Fact] = []
    seen: set[tuple[str, str]] = set()
    for element in root:
        context_id = element.get("contextRef")
        if context_id is None or context_id not in contexts:
            continue
        if element.get(f"{{{NS_XSI}}}nil") == "true":
            continue
        try:
            value = float((element.text or "").strip())
        except ValueError:
            continue
        concept = _qname(element.tag, prefixes)
        if (concept, context_id) in seen:
            continue
        seen.add((concept, context_id))
        start, end, dims = contexts[context_id]
        facts.append(
            Fact(concept, value, start, end, dims, units.get(element.get("unitRef", ""), ""))
        )
    return facts


def _contexts(root: ET.Element) -> dict[str, tuple[date | None, date, Dims]]:
    contexts = {}
    for context in root.iter(f"{{{NS_INSTANCE}}}context"):
        period = context.find(f"{{{NS_INSTANCE}}}period")
        if period is None:
            continue
        start = period.findtext(f"{{{NS_INSTANCE}}}startDate")
        end = period.findtext(f"{{{NS_INSTANCE}}}endDate") or period.findtext(
            f"{{{NS_INSTANCE}}}instant"
        )
        if not end:
            continue
        dims = tuple(
            sorted(
                (member.get("dimension", ""), (member.text or "").strip())
                for member in context.iter(f"{{{NS_DIMENSIONS}}}explicitMember")
            )
        )
        contexts[context.get("id", "")] = (
            date.fromisoformat(start.strip()) if start else None,
            date.fromisoformat(end.strip()),
            dims,
        )
    return contexts


def _units(root: ET.Element) -> dict[str, str]:
    units = {}
    for unit in root.iter(f"{{{NS_INSTANCE}}}unit"):
        measure = unit.findtext(f"{{{NS_INSTANCE}}}measure") or ""
        units[unit.get("id", "")] = measure.strip().split(":")[-1]
    return units


def _qname(tag: str, prefixes: dict[str, str]) -> str:
    if tag.startswith("{"):
        uri, local = tag[1:].split("}", 1)
        return f"{prefixes.get(uri, uri)}:{local}"
    return tag


def parse_labels(data: bytes) -> dict[str, str]:
    """Standard labels by concept QName (prefix:Name), without the ' [Member]' suffix."""
    root = ET.fromstring(data)
    locators: dict[str, str] = {}
    resources: dict[str, str] = {}
    arcs: list[tuple[str, str]] = []
    for element in root.iter():
        kind = element.get(f"{{{NS_XLINK}}}type")
        label = element.get(f"{{{NS_XLINK}}}label", "")
        if kind == "locator":
            fragment = element.get(f"{{{NS_XLINK}}}href", "").rsplit("#", 1)[-1]
            prefix, _, name = fragment.partition("_")
            locators[label] = f"{prefix}:{name}"
        elif kind == "resource" and element.get(f"{{{NS_XLINK}}}role") == LABEL_ROLE:
            resources[label] = (element.text or "").strip()
        elif kind == "arc":
            arcs.append(
                (element.get(f"{{{NS_XLINK}}}from", ""), element.get(f"{{{NS_XLINK}}}to", ""))
            )
    labels = {}
    for source, target in arcs:
        if source in locators and target in resources:
            labels[locators[source]] = _strip_suffix(resources[target])
    return labels


def _strip_suffix(label: str) -> str:
    return re.sub(r"\s*\[(member|axis|domain)\]$", "", label, flags=re.IGNORECASE)


def member_label(member: str, labels: dict[str, str]) -> str:
    if member in labels:
        return labels[member]
    name = member.split(":", 1)[-1].removesuffix("Member")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)


def revenue_facts(facts: Iterable[Fact], labels: dict[str, str]) -> RevenueFacts | None:
    """Total revenue for the latest fiscal year and its one-axis breakdowns, or None."""
    facts = list(facts)
    annual = [f for f in facts if f.start is not None and (f.end - f.start).days in ANNUAL_DAYS]
    totals = [f for f in annual if not f.dims and f.concept in REVENUE_CONCEPTS]
    if not totals:
        return None
    end = max(f.end for f in totals)
    concept = min((f.concept for f in totals if f.end == end), key=REVENUE_CONCEPTS.index)
    total_fact = next(f for f in totals if f.end == end and f.concept == concept)
    period = [f for f in annual if f.start == total_fact.start and f.end == end]
    total = total_fact.value

    by_axis: dict[str, dict[str, float]] = {}
    customers: dict[str, float] = {}
    for fact in period:
        if fact.concept == concept and fact.dims:
            axis, member = _single_axis(fact.dims)
            if axis in AXIS_KINDS:
                by_axis.setdefault(axis, {})[member] = fact.value
            elif axis == CUSTOMER_AXIS:
                customers.setdefault(member, fact.value / total)
    for fact in _concentrations(facts, end):
        customers[fact[0]] = fact[1]  # a stated percentage beats a derived one

    breakdowns: dict[str, Breakdown] = {}
    for axis, values in by_axis.items():
        kind = AXIS_KINDS[axis]
        breakdown = _breakdown(kind, axis, values, total, labels)
        current = breakdowns.get(kind)
        if current is None or len(breakdown.lines) > len(current.lines):
            breakdowns[kind] = breakdown
    return RevenueFacts(
        concept=concept,
        start=total_fact.start,  # type: ignore[arg-type]
        end=end,
        total=total,
        currency=total_fact.unit,
        breakdowns=breakdowns,
        customers=[
            RevenueLine(m, member_label(m, labels), share * total, share)
            for m, share in sorted(customers.items(), key=lambda item: -item[1])
        ],
    )


def _single_axis(dims: Dims) -> tuple[str, str]:
    """The one breakdown axis of a fact, ignoring a ConsolidationItemsAxis of operating
    segments. Returns ("", "") when the fact is cut along several axes."""
    rest = [(a, m) for a, m in dims if not (a == CONSOLIDATION_AXIS and m == OPERATING_SEGMENTS)]
    return rest[0] if len(rest) == 1 else ("", "")


def _concentrations(facts: list[Fact], end: date) -> list[tuple[str, float]]:
    found = []
    for fact in facts:
        if fact.concept != CONCENTRATION or fact.end != end:
            continue
        dims = dict(fact.dims)
        if CUSTOMER_AXIS in dims and dims.get(BENCHMARK_AXIS) in REVENUE_BENCHMARKS:
            found.append((dims[CUSTOMER_AXIS], fact.value))
    return found


def _breakdown(
    kind: str, axis: str, values: dict[str, float], total: float, labels: dict[str, str]
) -> Breakdown:
    members = dict(values)
    dropped: list[str] = []
    if not _sums_to(members.values(), total, COMPLETE_TOLERANCE) and all(
        v > 0 for v in members.values()
    ):
        # Drop the largest aggregates first, and stop once the rest partition the total, so a
        # detailed line that happens to equal a sum of others is kept.
        for member, value in sorted(values.items(), key=lambda item: -item[1]):
            others = [v for m, v in members.items() if m != member]
            if len(others) <= MAX_AGGREGATE_SEARCH and _subset_sums_to(others, value):
                dropped.append(member)
                del members[member]
                if _sums_to(members.values(), total, COMPLETE_TOLERANCE):
                    break
        if not _sums_to(members.values(), total, COMPLETE_TOLERANCE):
            # Alternative breakdowns tagged on one axis (Vertiv: Product/Service and
            # ProductExcludingSpares/ServicesAndSpares): keep the largest partition.
            partition = _best_partition(members, total)
            if partition is not None:
                dropped += sorted(m for m in members if m not in partition)
                members = {m: members[m] for m in partition}
    lines = [
        RevenueLine(member, member_label(member, labels), value, value / total)
        for member, value in sorted(members.items(), key=lambda item: -item[1])
    ]
    return Breakdown(
        kind=kind,
        axis=axis,
        lines=lines,
        complete=_sums_to(members.values(), total, COMPLETE_TOLERANCE),
        dropped=dropped,
    )


def _sums_to(values: Iterable[float], target: float, tolerance: float) -> bool:
    return abs(sum(values) - target) <= tolerance * abs(target)


def _best_partition(members: dict[str, float], total: float) -> set[str] | None:
    """The subset of members with the most lines that sums to the total. Ties go to the subset
    with more company-specific members (a generic us-gaap or srt member is less specific)."""
    if len(members) > MAX_AGGREGATE_SEARCH:
        return None
    names = sorted(members)
    for size in range(len(names), 1, -1):
        matches = [
            subset
            for subset in combinations(names, size)
            if _sums_to((members[m] for m in subset), total, COMPLETE_TOLERANCE)
        ]
        if matches:
            best = max(
                matches, key=lambda s: sum(not m.startswith(("us-gaap:", "srt:")) for m in s)
            )
            return set(best)
    return None


def _subset_sums_to(values: list[float], target: float) -> bool:
    """Whether two or more of the values add up to the target (within AGGREGATE_TOLERANCE)."""
    for size in range(2, len(values) + 1):
        for subset in combinations(values, size):
            if _sums_to(subset, target, AGGREGATE_TOLERANCE):
                return True
    return False


def load_revenue(cached: CachedFiling) -> RevenueFacts | None:
    """Revenue facts from a cached filing (M10), or None without an XBRL instance."""
    if "instance" not in cached.files:
        return None
    labels = parse_labels(read_cached(cached.files["labels"])) if "labels" in cached.files else {}
    return revenue_facts(parse_instance(read_cached(cached.files["instance"])), labels)
