"""Attention and novelty (Phase 2, M27).

The product is non-obvious exposure. A ranking that puts Nvidia first on AI compute says nothing
a reader did not know; the value is the company five hops out that nobody is writing about. The
mechanism Cohen and Frazzini describe (PLAN section 1) is limited investor attention, so the
thing to measure is whether anyone is looking.

`A(c)` is a company's share of all news coverage over a trailing window, from one GDELT query
per company (D91). It is deliberately **not** theme-paired: that costs one request per company
instead of one per company per theme. The accepted price is that a company famous for something
unrelated looks crowded. `ripple exposed --verify-attention` checks the top few with paired
queries when a lead matters.

`novelty = |exposure| x (1 - percentile of A(c) among the theme's exposed companies)`, keeping
the exposure's sign. The percentile is taken inside the exposed set, so the factor stays
theme-relative even though `A` is not.

A company with no coverage row has **unknown** attention, not zero. Treating missing data as
zero would make an unmeasured company look maximally novel, which is the opposite of the truth.
Those companies keep `None` and fall back to the hop-count rule (D92).
"""

from dataclasses import dataclass
from datetime import date, timedelta

from ripple.news import NewsClient
from ripple.store import Store

# Trailing window for attention, matching the 90 days PLAN section 8 specifies.
WINDOW_DAYS = 90
# A series needs at least this many days in the window to be usable.
MIN_DAYS = 30
# With hide_obvious, a company at or above this attention percentile counts as obvious (D92).
OBVIOUS_PERCENTILE = 0.8
# Joint articles needed before a paired share means anything.
MIN_PAIRED_ARTICLES = 5
# How many of the top-ranked companies --verify-attention checks.
VERIFY_TOP = 10


@dataclass(frozen=True)
class Attention:
    company: str
    # Share of all monitored coverage that mentioned the company, over the window.
    share: float
    days: int
    matched: int


def company_attention(
    store: Store,
    companies: list[str],
    as_of: date,
    window: int = WINDOW_DAYS,
    known_at: date | None = None,
) -> dict[str, Attention]:
    """Coverage share per company over the window ending on `as_of`.

    `known_at` bounds what the store had learned, so a backtest sees the attention that was
    measurable on the day rather than a later backfill.
    """
    first = as_of - timedelta(days=window)
    out: dict[str, Attention] = {}
    for company in companies:
        rows = store.coverage(company, start=first, end=as_of, known_at=known_at)
        if len(rows) < MIN_DAYS:
            continue
        matched = sum(r.matched for r in rows)
        norm = sum(r.norm for r in rows)
        if norm <= 0:
            continue
        out[company] = Attention(
            company=company, share=matched / norm, days=len(rows), matched=matched
        )
    return out


def percentiles(shares: dict[str, float]) -> dict[str, float]:
    """Rank each value in [0, 1] within the group: 0 is the least covered, 1 the most.

    Ties share a midrank, so two equally covered companies get the same percentile.
    """
    if not shares:
        return {}
    if len(shares) == 1:
        return {next(iter(shares)): 0.0}
    ordered = sorted(shares.values())
    total = len(ordered) - 1
    out: dict[str, float] = {}
    for company, value in shares.items():
        below = sum(1 for v in ordered if v < value)
        ties = sum(1 for v in ordered if v == value)
        midrank = below + (ties - 1) / 2
        out[company] = midrank / total
    return out


def novelty(exposure: float, percentile: float) -> float:
    """Exposure discounted by how much attention the company already has, sign kept."""
    return exposure * (1.0 - percentile)


@dataclass(frozen=True)
class PairedCheck:
    company: str
    # Share of the theme's own articles that also mentioned the company.
    paired_share: float
    theme_articles: int
    both_articles: int
    # The company-wide share this is checking, for the side-by-side.
    company_share: float | None

    @property
    def enough_data(self) -> bool:
        """Below a handful of joint articles the share is noise, not a measurement."""
        return self.both_articles >= MIN_PAIRED_ARTICLES


def verify_attention(
    client: NewsClient,
    theme_query: str,
    companies: list[tuple[str, str]],
    as_of: date,
    window: int = WINDOW_DAYS,
    company_shares: dict[str, float] | None = None,
) -> list[PairedCheck]:
    """Theme-paired coverage for a few companies, as an opt-in check on the top of a ranking.

    This is what company-wide attention cannot tell you: whether a company is famous *for this
    theme* or famous for something else (D91). It costs one request per company plus one for the
    theme, so it is only ever run on a handful of names.

    `companies` is (node ID, GDELT query) pairs.
    """
    first = as_of - timedelta(days=window)
    theme_total = sum(p.matched for p in client.volume(theme_query, first, as_of))
    out = []
    for company, query in companies:
        both = sum(p.matched for p in client.volume(f"{theme_query} {query}", first, as_of))
        out.append(
            PairedCheck(
                company=company,
                paired_share=both / theme_total if theme_total else 0.0,
                theme_articles=theme_total,
                both_articles=both,
                company_share=(company_shares or {}).get(company),
            )
        )
    return out


def is_obvious(
    percentile: float | None,
    hops: int | None,
    obvious_percentile: float = OBVIOUS_PERCENTILE,
    obvious_hops: int = 3,
) -> bool:
    """Whether a company counts as already-obvious for this theme.

    Attention decides when it is known; otherwise the Phase 0 hop-count placeholder still
    applies, so a company with no coverage series is not silently treated as novel (D92).
    """
    if percentile is not None:
        return percentile >= obvious_percentile
    return hops is not None and hops <= obvious_hops
