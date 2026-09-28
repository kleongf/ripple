"""Check the pre-registered event set against detected bursts (Phase 2, M29).

`tests/golden/events.yaml` was written before any coverage was fetched (D101). This module only
reads it: nothing here may change a threshold or a query in response to a miss, because the
whole value of the file is that it was fixed in advance.

A hit means the expected theme burst within `tolerance_days` of the event date. Events whose
date is marked `approximate` are reported separately, so a date recalled wrongly is not counted
as a failure of the detector.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from ripple import signal
from ripple.store import Store

DEFAULT_EVENTS = Path("tests/golden/events.yaml")


@dataclass(frozen=True)
class Event:
    id: str
    label: str
    day: date
    theme: str
    direction: str
    date_confidence: str
    reversal: bool
    why: str

    @property
    def approximate(self) -> bool:
        return self.date_confidence != "exact"


@dataclass(frozen=True)
class EventResult:
    event: Event
    hit: bool
    # The burst that covered the date, when there was one.
    burst: signal.Burst | None
    # Days from the event to the nearest burst, when one exists but missed the window.
    nearest_gap: int | None
    # Whether the tone reversal flag fired on the matching burst.
    flagged: bool
    # Another event whose burst is the same one this event matched, or is nearest to. Two events
    # days apart in one theme merge into a single burst window, so one is scored a hit and the
    # other a miss; naming the twin makes that visible instead of silent (D110).
    shares_burst_with: str | None = None

    @property
    def reversal_correct(self) -> bool | None:
        """For a reversal event, whether the flag fired. None when there was no hit."""
        if not self.hit:
            return None
        return self.flagged if self.event.reversal else not self.flagged


@dataclass(frozen=True)
class EventReport:
    results: list[EventResult]
    tolerance_days: int
    min_hit_rate: float

    @property
    def exact(self) -> list[EventResult]:
        return [r for r in self.results if not r.event.approximate]

    @property
    def hit_rate(self) -> float:
        return _rate([r.hit for r in self.results])

    @property
    def exact_hit_rate(self) -> float:
        return _rate([r.hit for r in self.exact])

    @property
    def met(self) -> bool:
        return self.hit_rate >= self.min_hit_rate

    @property
    def reversals(self) -> list[EventResult]:
        return [r for r in self.results if r.event.reversal]

    @property
    def flag_precision(self) -> float | None:
        """Of the bursts that matched an event, how often the flag matched the truth."""
        judged = [r.reversal_correct for r in self.results if r.reversal_correct is not None]
        return _rate(judged) if judged else None


def _rate(flags: list[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0


def load_events(path: Path = DEFAULT_EVENTS) -> tuple[list[Event], int, float]:
    spec = yaml.safe_load(path.read_text())
    events = [
        Event(
            id=row["id"],
            label=row["label"],
            day=row["date"],
            theme=row["theme"],
            direction=row["direction"],
            date_confidence=row.get("date_confidence", "exact"),
            reversal=bool(row.get("reversal", False)),
            why=row.get("why", ""),
        )
        for row in spec["events"]
    ]
    return events, int(spec.get("tolerance_days", 3)), float(spec.get("min_hit_rate", 0.8))


def check_events(
    store: Store,
    path: Path = DEFAULT_EVENTS,
    min_surprise: float = signal.MIN_SURPRISE,
) -> EventReport:
    events, tolerance, min_rate = load_events(path)
    by_theme: dict[str, list[signal.Burst]] = {}
    for event in events:
        if event.theme not in by_theme:
            rows = store.coverage(event.theme)
            by_theme[event.theme] = signal.bursts(event.theme, rows, min_surprise=min_surprise)

    results = []
    for event in events:
        found = by_theme[event.theme]
        # The burst containing the event day, else the nearest one within the tolerance. Taking
        # the first in date order attributed DeepSeek (2025-01-27) to the Stargate burst three
        # days earlier, and with it that burst's tone flag, although DeepSeek has its own (D130).
        covering = [b for b in found if b.covers(event.day, tolerance)]
        match = min(covering, key=lambda b: (_gap(b, event.day), b.start), default=None)
        gap = None
        nearest = match
        if match is None and found:
            nearest = min(found, key=lambda b: _gap(b, event.day))
            gap = _gap(nearest, event.day)
        results.append(
            EventResult(
                event=event,
                hit=match is not None,
                burst=match,
                nearest_gap=gap,
                flagged=match is not None and match.tone_flag == signal.REVERSAL,
                shares_burst_with=_twin(event, nearest, events, tolerance),
            )
        )
    return EventReport(results=results, tolerance_days=tolerance, min_hit_rate=min_rate)


def _twin(
    event: Event, burst: signal.Burst | None, events: list[Event], tolerance: int
) -> str | None:
    """Another event in the same theme whose window is the same burst (D110)."""
    if burst is None:
        return None
    for other in events:
        if other.id == event.id or other.theme != event.theme:
            continue
        if burst.covers(other.day, tolerance):
            return other.id
    return None


def _gap(burst: signal.Burst, day: date) -> int:
    if burst.start <= day <= burst.end:
        return 0
    return min(abs((burst.start - day).days), abs((burst.end - day).days))


def false_positives(
    store: Store,
    themes: list[str],
    path: Path = DEFAULT_EVENTS,
    min_surprise: float = signal.MIN_SURPRISE,
) -> dict[str, list[signal.Burst]]:
    """Bursts that match no pre-registered event, per theme.

    These are not necessarily wrong: the event set is a sample of what happened, not a census.
    The count is reported so a detector that fires constantly is visible.
    """
    events, tolerance, _ = load_events(path)
    out: dict[str, list[signal.Burst]] = {}
    for theme in themes:
        rows = store.coverage(theme)
        found = signal.bursts(theme, rows, min_surprise=min_surprise)
        dates = [e.day for e in events if e.theme == theme]
        out[theme] = [b for b in found if not any(b.covers(d, tolerance) for d in dates)]
    return out
