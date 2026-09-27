"""Grow the universe by one hop from the seed filers' own filings (Phase 1, M18, D71).

Codex reads each seed filer's Items 1 and 1A (or fallback chunks) and lists the companies the
text names as suppliers, customers, competitors or partners, with the taxonomy products each
makes and a short verbatim quote. The quote is located in the cached text and kept only as
offsets (D72); a mention whose quote cannot be found is dropped. Names are resolved to SEC
filers; those that file an annual report and make a taxonomy product are ranked by how many
seed filers name them, and the top ones join the universe.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any

from ripple.jobs import JobRunner, make_codex_job_runner
from ripple.model import Node
from ripple.xbrl_edges import company_name

ROLES = ("supplier", "customer", "competitor", "partner")
GROWTH_SECTIONS = {"1", "1A", "3D", "4"}
TICKER_NAME_SIMILARITY = 0.6
NAME_SIMILARITY = 0.9
STALE_AFTER = timedelta(days=550)  # an annual report older than this: likely deregistered
# Words a title may add to a name and still mean the same company ("Cisco" -> "Cisco Systems").
GENERIC_WORDS = {
    "co", "com", "company", "corp", "corporation", "de", "group", "holding", "holdings", "inc",
    "incorporated", "international", "limited", "ltd", "platforms", "plc", "systems",
    "technologies", "technology", "the", "uk",
}  # fmt: skip

INSTRUCTIONS = """You extract company relationships from one section of an annual report.
Reply only with JSON matching the schema. Do not call tools.
List every other company the text names as a supplier, customer, competitor or partner of the
filer. For each: the name as written, its US ticker if you know it (else null), the role, the
taxonomy products that company MAKES (IDs from the list; empty if none), and a verbatim quote
of at most 25 words from the text that contains the name.
Skip generic groups (for example "hyperscalers" or "OEMs"), government bodies, the filer
itself, and companies named only as examples of an industry trend."""


@dataclass(frozen=True)
class Mention:
    filer: str
    section: str
    start: int
    end: int
    role: str


@dataclass
class Candidate:
    name: str
    ticker: str | None
    cik: int | None
    title: str | None
    products: set[str] = field(default_factory=set)
    mentions: list[Mention] = field(default_factory=list)

    @property
    def filers(self) -> set[str]:
        return {m.filer for m in self.mentions}


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Offsets of the quote in the text, ignoring case and whitespace differences."""
    words = quote.split()
    if not words:
        return None
    match = re.search(r"\s+".join(re.escape(word) for word in words), text, re.I)
    return (match.start(), match.end()) if match else None


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", company_name(name).lower()).strip()


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long = sorted((a, b), key=len)
    if long.startswith(short + " ") and set(long[len(short) :].split()) <= GENERIC_WORDS:
        return NAME_SIMILARITY
    return SequenceMatcher(None, a, b).ratio()


class NameResolver:
    """Match a company name (and an optional ticker) to an SEC filer."""

    def __init__(self, titles: Iterable[tuple[int, str, str]]) -> None:
        self.rows = list(titles)
        self.by_ticker = {ticker.upper(): (cik, ticker, title) for cik, ticker, title in self.rows}
        self.by_first_word: dict[str, list[tuple[str, tuple[int, str, str]]]] = {}
        for row in self.rows:
            norm = _normalize(row[2])
            if norm:
                self.by_first_word.setdefault(norm.split()[0], []).append((norm, row))

    def resolve(self, name: str, ticker: str | None) -> tuple[int, str, str] | None:
        norm = _normalize(name)
        if ticker and ticker.upper() in self.by_ticker:
            row = self.by_ticker[ticker.upper()]
            # A ticker counts only if its title resembles the name: a wrong or invented
            # ticker must not pull in an unrelated filer.
            if _similar(norm, _normalize(row[2])) >= TICKER_NAME_SIMILARITY:
                return row
        if not norm:
            return None
        best: tuple[float, tuple[int, str, str]] | None = None
        for title_norm, row in self.by_first_word.get(norm.split()[0], []):
            score = _similar(norm, title_norm)
            if score >= NAME_SIMILARITY and (best is None or score > best[0]):
                best = (score, row)
        return best[1] if best else None


def growth_schema(taxonomy: list[Node]) -> dict[str, Any]:
    company = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "ticker", "role", "products", "quote"],
        "properties": {
            "name": {"type": "string"},
            "ticker": {"type": ["string", "null"]},
            "role": {"type": "string", "enum": list(ROLES)},
            "products": {
                "type": "array",
                "items": {"type": "string", "enum": [n.id for n in taxonomy]},
            },
            "quote": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["companies"],
        "properties": {"companies": {"type": "array", "items": company}},
    }


def growth_prompt(
    label: str, node: str, form: str, section: str, text: str, taxonomy: list[Node]
) -> str:
    products = "\n".join(f"- {n.id}: {n.label}" for n in taxonomy)
    return f"""Filer: {label} ({node}), {form}, section {section}.

Product taxonomy (use only these IDs):
{products}

Section text:
{text}
"""


def rank_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda c: (-len(c.filers), -len(c.mentions), c.name))


def select_additions(
    candidates: Iterable[Candidate],
    known_ciks: set[int],
    cap: int,
    annual_form: Callable[[int], str | None],
    rejected: dict[int, str] | None = None,
) -> dict[str, str]:
    """A decision per candidate name: added, or why not. `rejected` maps a CIK to the reason
    a review turned it down."""
    rejected = rejected or {}
    decisions: dict[str, str] = {}
    added = 0
    for candidate in rank_candidates(candidates):
        if candidate.cik is None:
            decisions[candidate.name] = "not an SEC filer"
        elif candidate.cik in known_ciks:
            decisions[candidate.name] = "already in the universe"
        elif candidate.cik in rejected:
            decisions[candidate.name] = f"rejected in review: {rejected[candidate.cik]}"
        elif not candidate.products:
            decisions[candidate.name] = "makes no taxonomy product"
        elif added >= cap:
            decisions[candidate.name] = "over cap"
        elif annual_form(candidate.cik) is None:
            decisions[candidate.name] = "no annual report"
        else:
            decisions[candidate.name] = "added"
            added += 1
    return decisions


def collect_candidates(
    answers: Iterable[tuple[str, str, int, str, dict[str, Any]]],
    resolver: NameResolver,
    taxonomy_ids: set[str],
    filer_ciks: dict[str, int],
) -> tuple[list[Candidate], int]:
    """Merge Codex answers into candidates. `answers` holds (filer, section, section start,
    section text, answer). Returns the candidates and the number of mentions dropped because
    their quote was not in the text."""
    by_key: dict[str, Candidate] = {}
    dropped = 0
    for filer, section, offset, text, answer in answers:
        for company in answer.get("companies", []):
            located = locate_quote(text, company.get("quote", ""))
            if located is None:
                dropped += 1
                continue
            resolved = resolver.resolve(company["name"], company.get("ticker"))
            if resolved is not None and resolved[0] == filer_ciks.get(filer):
                continue  # the filer itself
            key = f"cik:{resolved[0]}" if resolved else f"name:{_normalize(company['name'])}"
            if key not in by_key:
                by_key[key] = Candidate(
                    name=company["name"],
                    ticker=resolved[1] if resolved else company.get("ticker"),
                    cik=resolved[0] if resolved else None,
                    title=resolved[2] if resolved else None,
                )
            candidate = by_key[key]
            candidate.products |= {p for p in company.get("products", []) if p in taxonomy_ids}
            candidate.mentions.append(
                Mention(filer, section, offset + located[0], offset + located[1], company["role"])
            )
    return list(by_key.values()), dropped


def node_id_for(candidate: Candidate, taken: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", company_name(candidate.name).lower()).strip("-")
    node_id = f"company/{slug}"
    return node_id if node_id not in taken else f"{node_id}-{candidate.cik}"


def make_job_runner(db: Any) -> JobRunner:
    return make_codex_job_runner(db)


def annual_form(client_factory: Callable[[], Any], cik: int, as_of: date) -> str | None:
    """The form of the filer's latest annual report, if it is recent enough to use."""
    filing = client_factory().latest_annual_filing(cik)
    if filing is None or as_of - filing.filing_date > STALE_AFTER:
        return None
    return filing.form
