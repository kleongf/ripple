"""Turn a coverage series into bursts (Phase 2, M25).

A day is surprising when far more articles matched the query than the theme's own recent normal
predicts. Three numbers are reported for every day:

- **ratio**: share divided by the trailing median share. What a human reads ("5.2x normal").
- **surprise**: -log10 of the Poisson tail probability of seeing this many articles, given the
  expected count from the baseline. This is the threshold, because it stays meaningful when a
  thin theme gets 0 to 3 articles a day, where a Gaussian z-score on counts is noise.
- **z**: the Gaussian z-score of log share, reported for comparison with PLAN section 7.

Coverage **share** (matched / all articles monitored) is used rather than raw counts, so the
weekend dip in news volume and GDELT's growth in sources both cancel.

Overdispersion (D103): real coverage clusters, so its variance exceeds a Poisson's. The raw
Poisson tail would therefore fire constantly on a busy theme. The surprise is divided by a
dispersion factor estimated from the same trailing window, floored at 1. For a thin theme the
factor is about 1 and nothing changes, which is exactly where Poisson is the right model.

Direction never comes from these numbers. A burst means the theme is intensifying, and the
DRIVES polarity carries who wins (D90). Tone is standardized against the theme's own trailing
tone and only raises a `possible_reversal` flag.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from scipy import stats

from ripple.explain import envelope
from ripple.store import CoverageRow, Store

# Trailing window for the baseline, and the recent days left out of it so a building burst
# cannot raise its own baseline.
BASELINE_DAYS = 90
EXCLUDE_RECENT = 3
# A window needs this many days before any statistic is computed.
MIN_BASELINE_DAYS = 30

# Burst rule.
# -log10 p after the dispersion correction. Calibrated from the coverage series alone at about
# four bursts per theme-year (D109), on 14 of 15 themes, and fixed before any event result was
# seen (phase-3.md, D129). The a priori 6.0 gave no bursts at all on real series (D107).
MIN_SURPRISE = 1.75
MIN_ARTICLES = 5  # never call a burst on a handful of articles
PERSISTENCE = 2  # consecutive days above the threshold

# The largest counts left out of the dispersion estimate (D115). A 90-day window that holds one
# or two recent 3-4 day bursts is about 10% burst days; left in, they inflate the variance for
# the next 90 days and hide any second event (D110). Chronic clustering covers far more of the
# window and still shows.
DISPERSION_TRIM = 0.10
# A burst window is split at an interior trough whose surprise falls to this fraction of the
# smaller peak on either side, when both halves still meet the persistence rule (D115).
SPLIT_FRACTION = 0.5

# Tone deviation, in standard deviations of the theme's own trailing tone, that flags a burst
# as a possible reversal.
TONE_FLAG_Z = 2.0

# Cap on the reported surprise: the Poisson tail underflows to 0 for extreme days.
MAX_SURPRISE = 300.0
# Floor on the expected count. A theme silent on most days has a median share of exactly 0, and
# without a floor every quiet day divides by zero and every handful of articles looks infinitely
# surprising (D106). Half an article is the smallest honest expectation.
MIN_EXPECTED = 0.5
REVERSAL = "possible_reversal"


@dataclass(frozen=True)
class DayStat:
    day: date
    matched: int
    norm: int
    share: float
    # Trailing median share, and the article count it predicts for this day.
    baseline: float
    expected: float
    ratio: float
    surprise: float
    z: float
    # Variance / mean of the trailing counts, floored at 1 (D103).
    dispersion: float
    tone: float | None
    tone_z: float | None

    @property
    def over_threshold(self) -> bool:
        return self.surprise >= MIN_SURPRISE and self.matched >= MIN_ARTICLES


@dataclass(frozen=True)
class Burst:
    key: str
    start: date
    end: date
    peak_day: date
    peak_surprise: float
    peak_ratio: float
    # Articles matching the query across the burst window.
    matched: int
    # Set when the burst's tone is far below the theme's own baseline (D90).
    tone_flag: str | None
    tone_z: float | None

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def covers(self, day: date, tolerance: int = 0) -> bool:
        return self.start - timedelta(days=tolerance) <= day <= self.end + timedelta(days=tolerance)


def day_stats(rows: list[CoverageRow]) -> list[DayStat]:
    """A statistic per day that has a long enough trailing window behind it.

    Days missing from `rows` are gaps, not zeros: the window is defined by dates, so a gap
    shortens the window rather than pulling the baseline down.
    """
    ordered = sorted(rows, key=lambda r: r.day)
    by_day = {r.day: r for r in ordered}
    out: list[DayStat] = []
    for row in ordered:
        window = _window(by_day, row.day)
        if len(window) < MIN_BASELINE_DAYS:
            continue
        out.append(_stat(row, window))
    return out


def _window(by_day: dict[date, CoverageRow], day: date) -> list[CoverageRow]:
    """The baseline window for `day`: the BASELINE_DAYS before it, minus the most recent
    EXCLUDE_RECENT days, so a burst that has been building does not raise its own baseline."""
    last = day - timedelta(days=EXCLUDE_RECENT)
    first = day - timedelta(days=BASELINE_DAYS)
    return [row for d, row in by_day.items() if first <= d <= last]


def _stat(row: CoverageRow, window: list[CoverageRow]) -> DayStat:
    baseline = _baseline(window)
    expected = max(baseline * row.norm, MIN_EXPECTED)
    dispersion = _dispersion(window)
    surprise = _surprise(row.matched, expected, dispersion)
    # Ratio is taken against the effective expectation, so it stays finite on a thin theme.
    ratio = (row.matched / expected) if expected > 0 else 1.0
    tone_z = _tone_z(row.tone, window)
    return DayStat(
        day=row.day,
        matched=row.matched,
        norm=row.norm,
        share=row.share,
        baseline=baseline,
        expected=expected,
        ratio=ratio,
        surprise=surprise,
        z=_log_share_z(row, window),
        dispersion=dispersion,
        tone=row.tone,
        tone_z=tone_z,
    )


def _baseline(window: list[CoverageRow]) -> float:
    """The theme's normal coverage share over the window.

    The median of daily shares is robust to a spike inside the window, which is what we want, but
    it collapses to exactly 0 on a theme that is silent on most days — and then every day with a
    handful of articles looks infinitely surprising (D106).

    So the median is used whenever it is usable, and the pooled rate over the window (plus half an
    article, so it is never zero) only replaces it when the median implies less than MIN_EXPECTED
    articles on a typical day. Taking the larger of the two would defeat the point of the median,
    because one spike inside the window would drag the pooled rate, and with it the baseline, up.
    """
    norm = sum(w.norm for w in window)
    median = statistics.median([w.share for w in window])
    if norm <= 0:
        return median
    typical_norm = norm / len(window)
    if median * typical_norm >= MIN_EXPECTED:
        return median
    return (sum(w.matched for w in window) + 0.5) / norm


def _dispersion(window: list[CoverageRow]) -> float:
    """Variance over mean of the trailing counts, floored at 1 (D103), with the largest
    DISPERSION_TRIM of counts left out (D115).

    A Poisson has variance equal to its mean. Coverage clusters, so the ratio is usually above
    1 for a busy theme and near 1 for a thin one. Without the trim, one recent burst inside the
    window multiplied the factor by a hundred and hid every event for the next 90 days (D110).
    """
    counts = sorted(float(w.matched) for w in window)
    counts = counts[: len(counts) - int(len(counts) * DISPERSION_TRIM)]
    mean = statistics.fmean(counts)
    if mean <= 0 or len(counts) < 2:
        return 1.0
    return max(1.0, statistics.variance(counts) / mean)


def _surprise(matched: int, expected: float, dispersion: float) -> float:
    """-log10 P(X >= matched | Poisson(expected)), divided by the dispersion factor.

    `expected` is floored at MIN_EXPECTED by the caller, so there is no zero-baseline branch:
    that branch used to pin the surprise to its cap and turn a single article into a burst.
    """
    if matched <= 0:
        return 0.0
    tail = float(stats.poisson.sf(matched - 1, expected))
    raw = MAX_SURPRISE if tail <= 0 else min(MAX_SURPRISE, -math.log10(tail))
    return raw / dispersion


def _log_share_z(row: CoverageRow, window: list[CoverageRow]) -> float:
    """Gaussian z of log share. Reported, never thresholded on (PLAN section 7)."""
    logs = [_log_share(w) for w in window]
    if len(logs) < 2:
        return 0.0
    spread = statistics.stdev(logs)
    if spread == 0:
        return 0.0
    return (_log_share(row) - statistics.fmean(logs)) / spread


def _log_share(row: CoverageRow) -> float:
    """Log share with a half-article offset, so a zero day is representable."""
    return math.log((row.matched + 0.5) / row.norm) if row.norm else 0.0


def _tone_z(tone: float | None, window: list[CoverageRow]) -> float | None:
    """How far this day's tone sits from the theme's own trailing tone.

    Absolute tone is useless here: semiconductor coverage is mildly negative all the time. Only
    the deviation from the theme's own normal carries information.
    """
    if tone is None:
        return None
    tones = [w.tone for w in window if w.tone is not None]
    if len(tones) < 2:
        return None
    spread = statistics.stdev(tones)
    if spread == 0:
        return None
    return (tone - statistics.fmean(tones)) / spread


def bursts(
    key: str,
    rows: list[CoverageRow],
    min_surprise: float = MIN_SURPRISE,
    persistence: int = PERSISTENCE,
) -> list[Burst]:
    """Windows where the surprise stayed above the threshold for `persistence` days."""
    return bursts_from_stats(key, day_stats(rows), min_surprise, persistence)


def bursts_from_stats(
    key: str,
    stats_by_day: list[DayStat],
    min_surprise: float = MIN_SURPRISE,
    persistence: int = PERSISTENCE,
) -> list[Burst]:
    """`bursts` on day statistics already computed, so trying many thresholds on one series
    costs one pass of `day_stats` rather than one per threshold."""
    flagged = [s for s in stats_by_day if s.surprise >= min_surprise and s.matched >= MIN_ARTICLES]
    if not flagged:
        return []
    out: list[Burst] = []
    runs = [part for run in _runs(flagged) for part in _split(run, persistence)]
    for run in runs:
        if len(run) < persistence:
            continue
        peak = max(run, key=lambda s: s.surprise)
        tone_z = peak.tone_z
        out.append(
            Burst(
                key=key,
                start=run[0].day,
                end=run[-1].day,
                peak_day=peak.day,
                peak_surprise=peak.surprise,
                peak_ratio=peak.ratio,
                matched=sum(s.matched for s in run),
                tone_flag=REVERSAL if tone_z is not None and tone_z <= -TONE_FLAG_Z else None,
                tone_z=tone_z,
            )
        )
    return out


def _runs(flagged: list[DayStat]) -> list[list[DayStat]]:
    """Group flagged days into consecutive-date runs."""
    runs: list[list[DayStat]] = []
    for stat in flagged:
        if runs and stat.day - runs[-1][-1].day == timedelta(days=1):
            runs[-1].append(stat)
        else:
            runs.append([stat])
    return runs


def _split(run: list[DayStat], persistence: int) -> list[list[DayStat]]:
    """Split a run at its deepest interior trough when the trough falls to SPLIT_FRACTION of the
    smaller peak on either side and both halves still meet `persistence`; recurse on each half.
    Two events a few days apart then read as two bursts, not one (D110, D115)."""
    best: tuple[float, int] | None = None
    for i in range(persistence, len(run) - persistence + 1):
        left = max(s.surprise for s in run[:i])
        right = max(s.surprise for s in run[i:])
        depth = run[i].surprise / min(left, right)
        # The trough day starts the second half, so both halves keep `persistence` days.
        if depth <= SPLIT_FRACTION and (best is None or depth < best[0]):
            best = (depth, i)
    if best is None:
        return [run]
    cut = best[1]
    return _split(run[:cut], persistence) + _split(run[cut:], persistence)


# --- recent bursts across themes (Phase 3, M34) ------------------------------------------

TRENDING_NOTE = (
    "A burst means the theme is intensifying; its DRIVES edges say which products gain "
    "(polarity 1) and which lose (polarity -1). ratio is coverage against the theme's own "
    "trailing normal and surprise is -log10 of the Poisson tail after an overdispersion "
    "correction: units of surprise, not of demand, so never multiply them by an exposure. "
    "tone_flag possible_reversal means coverage spiked with unusually negative tone and the "
    "theme may be reversing, not intensifying; read it before citing the burst. unmeasured "
    "lists themes with no coverage series, which is unknown, not quiet."
)


def trending(
    store: Store,
    as_of: date,
    window: int = 90,
    min_surprise: float = MIN_SURPRISE,
    themes: list[str] | None = None,
) -> tuple[list[Burst], list[str]]:
    """Bursts that ended inside the `window` days up to `as_of`, strongest first, and the themes
    with no coverage at all. Reads coverage as the store knew it on `as_of`."""
    if themes is None:
        nodes = store.snapshot(as_of).nodes
        themes = sorted(n for n, v in nodes.items() if v.type == "theme")
    first = as_of - timedelta(days=window)
    found: list[Burst] = []
    unmeasured: list[str] = []
    for key in themes:
        rows = store.coverage(key, end=as_of, known_at=as_of)
        if not rows:
            unmeasured.append(key)
            continue
        found += [b for b in bursts(key, rows, min_surprise=min_surprise) if b.end >= first]
    found.sort(key=lambda b: -b.peak_surprise)
    return found, unmeasured


def trending_themes(
    store: Store, as_of: date, window: int = 90, min_surprise: float = MIN_SURPRISE
) -> dict[str, Any]:
    """`trending` as a compact record, each burst with its theme's kind and DRIVES edges and the
    query that produced the series (D112). Makes no GDELT request and returns no articles."""
    snapshot = store.snapshot(as_of)
    found, unmeasured = trending(store, as_of, window, min_surprise)
    drives: dict[str, list[dict[str, Any]]] = {}
    for edge in snapshot.edges:
        if edge.rel == "DRIVES":
            drives.setdefault(edge.src, []).append(
                {"product": edge.dst, "polarity": edge.polarity, "edge": edge.id}
            )
    hashes = {b.key: store.coverage(b.key, end=as_of, known_at=as_of)[-1].query_hash for b in found}

    def record(b: Burst) -> dict[str, Any]:
        node = snapshot.nodes.get(b.key)
        return {
            "theme": b.key,
            "label": node.label if node else b.key,
            "kind": node.kind if node else None,
            "start": b.start.isoformat(),
            "end": b.end.isoformat(),
            "peak_day": b.peak_day.isoformat(),
            "surprise": round(b.peak_surprise, 2),
            "ratio": round(b.peak_ratio, 2),
            "matched": b.matched,
            "tone_flag": b.tone_flag,
            "drives": sorted(drives.get(b.key, []), key=lambda d: (-d["polarity"], d["product"])),
            "query_hash": hashes[b.key],
        }

    return envelope(
        as_of,
        [TRENDING_NOTE],
        window_days=window,
        min_surprise=min_surprise,
        bursts=[record(b) for b in found],
        unmeasured=unmeasured,
    )


# --- threshold calibration (D107) -------------------------------------------------------

# Target burst frequency: how often a burst should fire per theme. One a quarter keeps a burst
# rare enough to be worth reading and frequent enough to catch a real move.
TARGET_BURSTS_PER_YEAR = 4.0
# Candidate thresholds searched, coarse to fine.
CALIBRATION_GRID = tuple(round(0.25 * i, 2) for i in range(1, 81))  # 0.25 .. 20.0


@dataclass(frozen=True)
class Calibration:
    threshold: float
    # Bursts per theme per year at that threshold.
    rate: float
    target: float
    series: int
    years: float
    # (threshold, rate) over the whole grid, so the choice can be inspected.
    curve: list[tuple[float, float]]


def calibrate_threshold(
    series: dict[str, list[CoverageRow]],
    target: float = TARGET_BURSTS_PER_YEAR,
    grid: tuple[float, ...] = CALIBRATION_GRID,
    persistence: int = PERSISTENCE,
) -> Calibration:
    """Pick the surprise threshold that makes bursts about as rare as `target` per theme-year.

    This reads only the coverage series. It never sees an event date, so calibrating this way
    leaves the pre-registered event set a genuine out-of-sample test (D101, D107). The judgement
    encoded here is "how often is a burst rare enough to be worth reading", which is independent
    of which events happened to occur.

    Returns the smallest threshold whose rate is at or below the target, with the whole curve.
    """
    usable = {k: v for k, v in series.items() if v}
    if not usable:
        return Calibration(MIN_SURPRISE, 0.0, target, 0, 0.0, [])
    span_days = 0
    for rows in usable.values():
        days = [r.day for r in rows]
        span_days = max(span_days, (max(days) - min(days)).days + 1)
    theme_years = max(len(usable) * span_days / 365.25, 1e-9)

    curve: list[tuple[float, float]] = []
    stats = {key: day_stats(rows) for key, rows in usable.items()}
    for threshold in grid:
        total = sum(
            len(bursts_from_stats(key, days, min_surprise=threshold, persistence=persistence))
            for key, days in stats.items()
        )
        curve.append((threshold, total / theme_years))
    chosen = next((t for t, rate in curve if rate <= target), grid[-1])
    rate = dict(curve)[chosen]
    return Calibration(
        threshold=chosen,
        rate=rate,
        target=target,
        series=len(usable),
        years=span_days / 365.25,
        curve=curve,
    )
