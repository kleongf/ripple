"""Evidence verification: a ledger of human rulings, applied at load (Phase 3, M33, D113).

Every evidence item in the store gets one ruling: `verified`, `wrong-note`, `dead-link` or
`unsupported`. Rulings live in `data/verified.yaml`, keyed by edge ID, URL and a hash of the
note, because the generated `xbrl` source is rewritten by `ripple review accept` and cannot
hold a flag. Keying on the note means a ruling lapses when the note changes: a regenerated
edge with new arithmetic has to be checked again.

The ledger is append-only in spirit: a later ruling for the same key replaces an earlier one,
and nothing is deleted. Only `verified` changes the store (it sets `Evidence.verified`, which
D63 ranks as human-trusted); the other rulings are a to-do list of things to fix at the source.

For `xbrl` evidence the arithmetic can be redone from the cached filings (`recompute`), so a
person confirms one filing rather than re-reading every line.
"""

import dataclasses
import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import yaml

from ripple import edgar, mapping, xbrl, xbrl_edges
from ripple.load import Seed
from ripple.model import Edge, Located, Node, today_utc
from ripple.store import Snapshot

DEFAULT_LEDGER = Path("data/verified.yaml")
Ruling = Literal["verified", "wrong-note", "dead-link", "unsupported"]
RULINGS: tuple[str, ...] = ("verified", "wrong-note", "dead-link", "unsupported")
LEDGER_HEADER = (
    "# Evidence rulings (docs/phase-3.md, M33, D113). Written by `ripple verify`; a later\n"
    "# entry for the same (edge, url, note_hash) replaces an earlier one. Only `verified`\n"
    "# changes the store; the other rulings are fixes owed at the source.\n"
)
# How far a recomputed share may sit from the stored one and still match (rounding to 4 places).
SHARE_TOLERANCE = 5e-4


def note_hash(note: str) -> str:
    return hashlib.sha256(note.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Entry:
    edge: str
    url: str
    note_hash: str
    ruling: Ruling
    date: date
    by: str = "user"
    reason: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.edge, self.url, self.note_hash)


def load_ledger(path: Path = DEFAULT_LEDGER) -> dict[tuple[str, str, str], Entry]:
    """The current ruling per key; later entries replace earlier ones."""
    if not path.exists():
        return {}
    rows = yaml.safe_load(path.read_text()) or []
    current: dict[tuple[str, str, str], Entry] = {}
    for row in rows:
        if row.get("ruling") not in RULINGS:
            raise ValueError(f"{path}: unknown ruling {row.get('ruling')!r} for {row.get('edge')}")
        entry = Entry(**row)
        current[entry.key] = entry
    return current


def append_rulings(entries: Iterable[Entry], path: Path = DEFAULT_LEDGER) -> int:
    """Add rulings to the end of the ledger file."""
    new = [dataclasses.asdict(e) for e in entries]
    if not new:
        return 0
    for row in new:
        if not row["reason"]:
            del row["reason"]
    existing = path.read_text() if path.exists() else LEDGER_HEADER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing.rstrip("\n") + "\n" + yaml.safe_dump(new, sort_keys=False))
    return len(new)


def ruling_for(
    ledger: dict[tuple[str, str, str], Entry], edge: Edge, url: str | None, note: str
) -> Entry | None:
    if url is None:
        return None
    return ledger.get((edge.id, url, note_hash(note)))


def apply_rulings(seed: Seed, ledger: dict[tuple[str, str, str], Entry]) -> Seed:
    """The seed with `verified` set on every evidence item whose current ruling says so.

    A ruling never unsets a `verified: true` written in source YAML; it only adds."""
    if not ledger:
        return seed
    edges: list[Located[Edge]] = []
    for located in seed.edges:
        edge = located.value
        evidence = [
            item.model_copy(update={"verified": True})
            if not item.verified
            and (entry := ruling_for(ledger, edge, item.url, item.note)) is not None
            and entry.ruling == "verified"
            else item
            for item in edge.evidence
        ]
        if evidence != edge.evidence:
            located = dataclasses.replace(
                located, value=edge.model_copy(update={"evidence": evidence})
            )
        edges.append(located)
    return dataclasses.replace(seed, edges=edges)


def started(seed: Seed, ledger: dict[tuple[str, str, str], Entry]) -> bool:
    """Whether any ruling in the ledger applies to this seed, so W11 is worth reporting."""
    return any(
        ruling_for(ledger, located.value, item.url, item.note) is not None
        for located in seed.edges
        for item in located.value.evidence
    )


# --- what is left to rule on ------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One evidence item on one edge row, as the store holds it."""

    edge: Edge
    source: str
    # True when this row won its edge key; False for a row in `alternatives`.
    winner: bool
    url: str
    note: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.edge.id, self.url, note_hash(self.note))


def items(snapshot: Snapshot) -> list[Item]:
    """Every evidence item on every edge row, winners and the rows that lost.

    A losing row still needs a ruling: verifying it can make it win (D63)."""
    out: list[Item] = []
    rows = [(snapshot.sources.get(e.id, "seed"), e, True) for e in snapshot.edges]
    for losers in snapshot.alternatives.values():
        rows += [(source, e, False) for source, e in losers]
    for source, edge, winner in rows:
        for evidence in edge.evidence:
            if evidence.url:
                out.append(Item(edge, source, winner, evidence.url, evidence.note))
    return out


@dataclass(frozen=True)
class Status:
    total: int
    ruled: int
    verified: int
    failed: int
    by_source: dict[str, tuple[int, int]]  # source -> (items, ruled)
    documents_left: int


def status(snapshot: Snapshot, ledger: dict[tuple[str, str, str], Entry]) -> Status:
    all_items = items(snapshot)
    rulings = [ledger.get(i.key) for i in all_items]
    by_source: dict[str, tuple[int, int]] = {}
    for item, entry in zip(all_items, rulings, strict=True):
        count, ruled = by_source.get(item.source, (0, 0))
        by_source[item.source] = (count + 1, ruled + (entry is not None))
    left = {i.url for i, e in zip(all_items, rulings, strict=True) if e is None}
    return Status(
        total=len(all_items),
        ruled=sum(e is not None for e in rulings),
        verified=sum(e is not None and e.ruling == "verified" for e in rulings),
        failed=sum(e is not None and e.ruling != "verified" for e in rulings),
        by_source=by_source,
        documents_left=len(left),
    )


def unruled_by_document(
    snapshot: Snapshot,
    ledger: dict[tuple[str, str, str], Entry],
    source: str | None = None,
) -> dict[str, list[Item]]:
    """Unruled items grouped by URL, filing-layer rows first within a document (D113), and
    documents ordered by how many items they settle."""
    groups: dict[str, list[Item]] = {}
    for item in items(snapshot):
        if item.key in ledger or (source is not None and item.source != source):
            continue
        groups.setdefault(item.url, []).append(item)
    order = {"xbrl": 0, "review": 0, "seed": 1, "llm": 2}
    for group in groups.values():
        group.sort(key=lambda i: (order.get(i.source, 3), i.edge.label))
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])))


# --- recomputing xbrl arithmetic from the cached filings ---------------------------------


@dataclass(frozen=True)
class Recomputed:
    item: Item
    match: bool
    detail: str


def recompute(
    group: list[Item],
    universe: list[edgar.UniverseEntry],
    cache: Path = edgar.DEFAULT_CACHE,
    queue: Path = mapping.DEFAULT_QUEUE,
    companies: list[Node] | None = None,
) -> list[Recomputed]:
    """Redo the arithmetic behind `xbrl` evidence items from the cached XBRL instance.

    PRODUCES: every line share in the accepted mapping must match the cached filing, and the
    edge weight and note must be what the mapping implies. EXPOSED_TO and SUPPLIES: the edge is
    regenerated from the cached facts and compared. Other items are skipped (not in the list).
    """
    ciks = {e.node: e.cik for e in universe if e.cik is not None}
    out: list[Recomputed] = []
    facts_by_node: dict[str, tuple[xbrl.RevenueFacts | None, edgar.CachedFiling | None]] = {}
    for item in group:
        if item.source != "xbrl":
            continue
        node = item.edge.src
        if node not in facts_by_node:
            cik = ciks.get(node)
            cached = edgar.latest_cached(cache, cik) if cik is not None else None
            facts_by_node[node] = (xbrl.load_revenue(cached) if cached else None, cached)
        facts, cached = facts_by_node[node]
        if facts is None or cached is None:
            out.append(Recomputed(item, False, "no cached XBRL filing to recompute from"))
            continue
        if cached.filing.url != item.url:
            out.append(Recomputed(item, False, f"cached filing is {cached.filing.url}"))
            continue
        match item.edge.rel:
            case "PRODUCES":
                out.append(_recompute_produces(item, facts, queue))
            case "EXPOSED_TO":
                _, records = xbrl_edges.region_edges(node, facts, cached.filing, today_utc())
                out.append(_compare(item, records))
            case "SUPPLIES":
                records, _ = xbrl_edges.customer_edges(
                    node,
                    facts,
                    cached.filing,
                    companies or [],
                    has_product_path=lambda *_: False,
                    accessed=today_utc(),
                )
                # The product-path check only lowers confidence and adds a clause to the note;
                # compare the weight and the fact behind it.
                out.append(_compare(item, records, note_prefix=True))
            case _:
                out.append(Recomputed(item, False, f"no recompute for {item.edge.rel}"))
    return out


def _recompute_produces(item: Item, facts: xbrl.RevenueFacts, queue: Path) -> Recomputed:
    path = mapping.queue_path(queue, item.edge.src)
    if not path.exists():
        return Recomputed(item, False, "no accepted mapping in the review queue")
    proposal = mapping.load_mapping(path)
    breakdown = facts.breakdowns.get(proposal.kind)
    if breakdown is None:
        return Recomputed(item, False, f"cached filing has no {proposal.kind} breakdown")
    shares = {line.member: line.share for line in breakdown.lines}
    for line in proposal.lines:
        if line.member not in shares:
            return Recomputed(item, False, f"{line.member} is not in the cached breakdown")
        if abs(shares[line.member] - line.share) > SHARE_TOLERANCE:
            return Recomputed(
                item,
                False,
                f"{line.label}: mapping says {line.share:.4f}, filing says "
                f"{shares[line.member]:.4f}",
            )
    return _compare(item, mapping.edges_from_mapping(proposal, today_utc()))


def _compare(item: Item, records: list[dict], note_prefix: bool = False) -> Recomputed:
    record = next((r for r in records if r["dst"] == item.edge.dst), None)
    if record is None:
        return Recomputed(item, False, "not regenerated from the cached filing")
    if abs(record["weight"] - item.edge.weight) > SHARE_TOLERANCE:
        return Recomputed(
            item, False, f"weight {item.edge.weight} stored, {record['weight']} recomputed"
        )
    note = record["evidence"][0]["note"]
    same = item.note.startswith(note.split(";")[0]) if note_prefix else note == item.note
    if not same:
        return Recomputed(item, False, f"note differs: recomputed {note!r}")
    return Recomputed(item, True, "weight and note recomputed from the cached filing")


# --- link check: a pre-pass for the person ruling, never a ruling itself -------------------

# One request per distinct URL, paced per host; SEC asks for at most 10 a second and CLAUDE.md
# keeps EDGAR at 5, so every host gets the stricter pace.
LINK_INTERVAL = 0.25
LINK_TIMEOUT = 20.0


@dataclass(frozen=True)
class Link:
    url: str
    status: int | None
    final_url: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400

    @property
    def moved(self) -> bool:
        return self.ok and self.final_url is not None and self.final_url != self.url


def check_links(
    urls: Iterable[str],
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Link]:
    """Request each URL once and record the status and where it ended up. Stores no content,
    and rules on nothing: a live page may still not support its note, which only a person can
    judge (D113)."""
    results: list[Link] = []
    seen: set[str] = set()
    for url in sorted(set(urls)):
        # Sorted, so one host's URLs run consecutively and each after the first waits its turn.
        host = urlsplit(url).netloc
        if host in seen:
            sleep(LINK_INTERVAL)
        seen.add(host)
        try:
            response = client.get(url)
            results.append(Link(url, response.status_code, str(response.url), None))
        except httpx.HTTPError as exc:
            results.append(Link(url, None, None, f"{type(exc).__name__}: {exc}"[:200]))
    return results


def link_client(user_agent: str) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent}, follow_redirects=True, timeout=LINK_TIMEOUT
    )
