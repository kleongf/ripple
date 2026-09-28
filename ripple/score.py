"""Rank companies by exposure to a theme shock, with explanation paths and flags."""

import math
import random
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from scipy import stats

from ripple import attention as attention_module
from ripple.graph import Graph, build_graph
from ripple.model import DEFAULT_MIN_CONFIDENCE, bucket_quantity, bucket_range
from ripple.paths import Path, min_hops, top_paths
from ripple.propagate import Direction, Shock, propagate, shock_vector
from ripple.store import Store

DEFAULT_MAX_HOPS = 5
# min_hops at or below this counts as obvious until the attention metric exists (D5, D23).
OBVIOUS_HOPS = 3
ZERO = 1e-12
# Nodes a demand shock can start from. A theme is the usual lens; a product or material shock
# asks who is exposed to demand for that one node (phase-3.md, M30). Companies and regions are
# outputs of the propagation, not inputs to it.
SHOCKABLE = ("theme", "product", "material")
NOTES = (
    "Exposure is the fractional revenue impact per unit of theme shock. Guessed weights are "
    "buckets, not measured values. Do not add exposures across themes. Attention is the "
    "company's share of all news coverage over the last 90 days, not coverage paired with this "
    "theme, so a company famous for something else can look crowded. Novelty is exposure "
    "discounted by the attention percentile among this theme's exposed companies; null means no "
    "coverage series is held, which is unknown attention rather than none."
)

PRODUCT_NOTE = (
    "This is a shock on one product or material, not a theme: it reaches the node's producers, "
    "the inputs it requires and their producers, and suppliers of those producers. It does not "
    "reach the node's customers, because demand does not flow that way."
)


@dataclass(frozen=True)
class CompanyScore:
    company: str
    label: str
    ticker: str | None
    exposure: float
    min_hops: int | None
    # Bucket-sourced weights on the top path.
    guessed_weights: int
    # Product of confidences on the top path.
    path_confidence: float
    # Lowest edge confidence on the top path.
    weakest_confidence: float
    paths: list[Path]
    # Share of all news coverage mentioning the company over the attention window (M27, D91).
    # None when no coverage series is held: unknown, not zero.
    attention: float | None = None
    # Where that share sits among this theme's exposed companies, 0 least covered.
    attention_percentile: float | None = None
    # |exposure| x (1 - percentile), sign kept. None when attention is unknown.
    novelty: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "label": self.label,
            "ticker": self.ticker,
            "exposure": round(self.exposure, 4),
            "min_hops": self.min_hops,
            "guessed_weights": self.guessed_weights,
            "path_confidence": round(self.path_confidence, 3),
            "weakest_confidence": round(self.weakest_confidence, 3),
            "attention": None if self.attention is None else round(self.attention, 8),
            "attention_percentile": (
                None if self.attention_percentile is None else round(self.attention_percentile, 3)
            ),
            "novelty": None if self.novelty is None else round(self.novelty, 4),
            "paths": [
                {
                    "contribution": round(p.contribution, 4),
                    "confidence": round(p.confidence, 3),
                    "weakest_confidence": round(p.weakest_confidence, 3),
                    "nodes": p.nodes,
                    "edges": p.edges,
                }
                for p in self.paths
            ],
        }


@dataclass(frozen=True)
class ScoreResult:
    # The shocked node: a theme, or a product or material (M30).
    theme: str
    theme_kind: str | None
    direction: Direction
    as_of: date
    min_confidence: float
    max_hops: int
    results: list[CompanyScore]
    shock_type: str = "theme"

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "theme": self.theme,
            "shock_type": self.shock_type,
            "theme_kind": self.theme_kind,
            "direction": self.direction,
            "min_confidence": self.min_confidence,
            "max_hops": self.max_hops,
            "results": [r.to_dict() for r in self.results],
            "notes": [NOTES] + ([] if self.shock_type == "theme" else [PRODUCT_NOTE]),
        }


def score(
    store: Store,
    theme: str,
    as_of: date,
    direction: Direction = "up",
    max_hops: int = DEFAULT_MAX_HOPS,
    hide_obvious: bool = False,
    limit: int | None = 20,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    k: int = 3,
    obvious_hops: int = OBVIOUS_HOPS,
    use_attention: bool = True,
    obvious_percentile: float = attention_module.OBVIOUS_PERCENTILE,
    known_at: date | None = None,
    attention_as_of: date | None = None,
) -> ScoreResult:
    """Rank companies by exposure, with attention and novelty where coverage is held.

    `use_attention=False` keeps the Phase 0 behaviour, which the golden ranking tests rely on.
    `attention_as_of` measures attention on another day than the graph's `as_of`: the backward
    test ranks every past burst with today's graph, because a past snapshot is nearly empty,
    and takes attention from the 90 days before the burst (docs/phase-4.md, D137).
    """
    graph = build_graph(store.snapshot(as_of), min_confidence=min_confidence)
    attention = None
    if use_attention:
        exposed = list(company_exposures(graph, Shock(theme, 1.0, direction), max_hops))
        attention = attention_module.company_attention(
            store, exposed, attention_as_of or as_of, known_at=known_at
        )
    return score_graph(
        graph,
        theme,
        direction,
        max_hops,
        hide_obvious,
        limit,
        k,
        obvious_hops,
        attention=attention,
        obvious_percentile=obvious_percentile,
    )


def score_graph(
    graph: Graph,
    theme: str,
    direction: Direction = "up",
    max_hops: int = DEFAULT_MAX_HOPS,
    hide_obvious: bool = False,
    limit: int | None = 20,
    k: int = 3,
    obvious_hops: int = OBVIOUS_HOPS,
    attention: dict[str, attention_module.Attention] | None = None,
    obvious_percentile: float = attention_module.OBVIOUS_PERCENTILE,
) -> ScoreResult:
    shock = Shock(theme, 1.0, direction)
    exposures = company_exposures(graph, shock, max_hops)
    hops = min_hops(graph, theme, max_hops)
    ranked = sorted(exposures.items(), key=lambda item: (-abs(item[1]), item[0]))

    # Percentiles are taken across every exposed company, before any limit or filter, so the
    # factor does not depend on how many rows the caller asked for.
    shares = {c: a.share for c, a in (attention or {}).items() if c in exposures}
    pcts = attention_module.percentiles(shares)

    results: list[CompanyScore] = []
    for company, value in ranked:
        company_hops = hops.get(company)
        percentile = pcts.get(company)
        if hide_obvious and attention_module.is_obvious(
            percentile, company_hops, obvious_percentile, obvious_hops
        ):
            continue
        if limit is not None and len(results) >= limit:
            break
        results.append(
            _company_score(
                graph,
                shock,
                company,
                value,
                company_hops,
                max_hops,
                k,
                shares.get(company),
                percentile,
            )
        )

    return ScoreResult(
        theme=theme,
        theme_kind=graph.nodes[theme].kind,
        shock_type=graph.nodes[theme].type,
        direction=direction,
        as_of=graph.as_of,
        min_confidence=graph.min_confidence,
        max_hops=max_hops,
        results=results,
    )


def company_exposures(graph: Graph, shock: Shock, max_hops: int) -> dict[str, float]:
    """Nonzero exposure of every company to the shock."""
    node = graph.nodes.get(shock.theme)
    if node is None:
        raise ValueError(f"unknown theme {shock.theme}")
    if node.type not in SHOCKABLE:
        raise ValueError(f"{shock.theme} is a {node.type}; shock a theme, product or material")
    total = propagate(graph.W, shock_vector(graph, shock), max_hops)
    return {
        node_id: float(total[i])
        for node_id, i in graph.index.items()
        if graph.nodes[node_id].type == "company" and abs(total[i]) > ZERO
    }


def _company_score(
    graph: Graph,
    shock: Shock,
    company: str,
    exposure: float,
    hops: int | None,
    max_hops: int,
    k: int,
    attention: float | None = None,
    percentile: float | None = None,
) -> CompanyScore:
    paths = [
        Path(p.nodes, p.edge_records, p.contribution * shock.signed, p.confidence)
        for p in top_paths(graph, shock.theme, company, k=k, max_hops=max_hops)
    ]
    top = paths[0] if paths else None
    node = graph.nodes[company]
    return CompanyScore(
        company=company,
        label=node.label,
        ticker=node.ticker,
        exposure=exposure,
        min_hops=hops,
        guessed_weights=(sum(e.weight_source == "bucket" for e in top.edge_records) if top else 0),
        path_confidence=top.confidence if top else 0.0,
        weakest_confidence=top.weakest_confidence if top else 0.0,
        paths=paths,
        attention=attention,
        attention_percentile=percentile,
        novelty=(None if percentile is None else attention_module.novelty(exposure, percentile)),
    )


# Sensitivity check (M9, D17).


JitterMode = Literal["bucket", "flat"]


@dataclass(frozen=True)
class Sensitivity:
    theme: str
    trials: int
    mode: JitterMode
    jitter: float
    # Size of the top group the Spearman correlation and overlap are measured on.
    top_n: int
    median_spearman_top: float
    median_kendall_all: float
    # How many of the baseline top_n stay in the trial top_n (tier stability).
    median_top_overlap: float
    # (company, mean absolute rank change across trials), least stable first.
    least_stable: list[tuple[str, float]]


def jitter_factors(
    graph: Graph, rng: random.Random, mode: JitterMode = "bucket", jitter: float = 0.5
) -> dict[tuple[int, int], float]:
    """A random factor for every bucket-sourced cell of W.

    In bucket mode the new weight is drawn log-uniformly from the range the bucket stands for
    (`model.bucket_range`), so a share never exceeds 1. Weights that are not a bucket value, and
    flat mode, use a factor in [1 - jitter, 1 + jitter]. Filing and manual weights are kept.
    """
    factors: dict[tuple[int, int], float] = {}
    for cell, edge in graph.edges.items():
        if edge.weight_source != "bucket":
            continue
        quantity = bucket_quantity(edge.rel)
        span = bucket_range(quantity, edge.weight) if mode == "bucket" and quantity else None
        if span is None:
            factors[cell] = rng.uniform(1 - jitter, 1 + jitter)
        else:
            drawn = math.exp(rng.uniform(math.log(span[0]), math.log(span[1])))
            factors[cell] = drawn / edge.weight
    return factors


def sensitivity(
    graph: Graph,
    theme: str,
    trials: int = 200,
    jitter: float = 0.5,
    top_n: int = 10,
    max_hops: int = DEFAULT_MAX_HOPS,
    seed: int = 0,
    mode: JitterMode = "bucket",
) -> Sensitivity:
    """Rerun the ranking with bucket-sourced weights redrawn (see `jitter_factors`)."""
    shock = Shock(theme)
    baseline = company_exposures(graph, shock, max_hops)
    ranked = sorted(baseline, key=lambda c: (-abs(baseline[c]), c))
    top = ranked[:top_n]
    base_rank = {c: rank for rank, c in enumerate(ranked)}
    rng = random.Random(seed)

    spearman: list[float] = []
    kendall: list[float] = []
    overlap: list[int] = []
    shifts: dict[str, list[int]] = {c: [] for c in ranked}
    for _ in range(trials):
        factors = jitter_factors(graph, rng, mode, jitter)
        trial = company_exposures(graph.with_scaled_weights(factors), shock, max_hops)
        size = {c: abs(trial.get(c, 0.0)) for c in ranked}
        order = sorted(ranked, key=lambda c: (-size[c], c))
        spearman.append(_rank_correlation(stats.spearmanr, baseline, size, top))
        kendall.append(_rank_correlation(stats.kendalltau, baseline, size, ranked))
        overlap.append(len(set(top) & set(order[: len(top)])))
        for rank, c in enumerate(order):
            shifts[c].append(abs(rank - base_rank[c]))

    mean_shift = {c: statistics.fmean(v) if v else 0.0 for c, v in shifts.items()}
    return Sensitivity(
        theme=theme,
        trials=trials,
        mode=mode,
        jitter=jitter,
        top_n=len(top),
        median_spearman_top=statistics.median(spearman) if spearman else 1.0,
        median_kendall_all=statistics.median(kendall) if kendall else 1.0,
        median_top_overlap=statistics.median(overlap) if overlap else float(len(top)),
        least_stable=sorted(mean_shift.items(), key=lambda item: (-item[1], item[0]))[:5],
    )


def _rank_correlation(
    fn: Any, baseline: dict[str, float], trial: dict[str, float], companies: list[str]
) -> float:
    a = [abs(baseline[c]) for c in companies]
    b = [trial[c] for c in companies]
    if len(companies) < 2 or len(set(a)) == 1 or len(set(b)) == 1:
        # Correlation is undefined for a constant list; identical orderings count as stable.
        return 1.0 if _order(a) == _order(b) else 0.0
    return float(fn(a, b)[0])


def _order(values: list[float]) -> list[int]:
    return sorted(range(len(values)), key=lambda i: -values[i])
