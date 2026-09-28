"""The event-set runner (Phase 2, M29).

These tests check the runner against synthetic coverage, not the real measurement: they prove a
burst at an event's date counts as a hit and one far away does not. The real hit rate needs real
GDELT series and is reported in docs/phase-2-review.md.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from ripple import events, signal
from ripple.store import CoverageRow, Store

REAL_EVENTS = Path(__file__).parent / "golden" / "events.yaml"


def flat_then_burst(
    key: str, start: date, days: int, burst_day: date | None, burst_len: int = 4
) -> list[CoverageRow]:
    """A steady series, optionally with a sustained spike starting on `burst_day`."""
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        spike = burst_day is not None and burst_day <= day < burst_day + timedelta(days=burst_len)
        rows.append(
            CoverageRow(
                kind="theme",
                key=key,
                day=day,
                matched=600 if spike else 60,
                norm=500_000,
                tone=-6.0 if spike else -1.0 - (i % 3) * 0.1,
                query_hash="h",
            )
        )
    return rows


def event_file(tmp_path: Path, **overrides: object) -> Path:
    row = {
        "id": "test-event",
        "label": "Test event",
        "date": date(2024, 6, 1),
        "theme": "theme/t",
        "direction": "up",
        "date_confidence": "exact",
        "why": "synthetic",
    }
    row.update(overrides)  # type: ignore[arg-type]
    path = tmp_path / "events.yaml"
    path.write_text(yaml.safe_dump({"tolerance_days": 3, "min_hit_rate": 0.8, "events": [row]}))
    return path


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "t.duckdb")


# --- the real file parses --------------------------------------------------------------


def test_the_pre_registered_file_loads() -> None:
    loaded, tolerance, min_rate = events.load_events(REAL_EVENTS)
    assert len(loaded) >= 12
    assert tolerance == 3
    assert min_rate == pytest.approx(0.8)
    assert {e.direction for e in loaded} <= {"up", "down"}
    # Every reversal event is a demand-down one: that is what the flag is for (D90).
    assert all(e.direction == "down" for e in loaded if e.reversal)


def test_every_event_names_a_theme_that_exists() -> None:
    loaded, _, _ = events.load_events(REAL_EVENTS)
    themes = {
        node["id"]
        for node in yaml.safe_load(
            (Path(__file__).parent.parent / "data" / "seed" / "nodes" / "themes.yaml").read_text()
        )
    }
    assert {e.theme for e in loaded} <= themes


def test_the_event_set_has_reversal_cases() -> None:
    """Without a demand-down case the tone flag is untested."""
    loaded, _, _ = events.load_events(REAL_EVENTS)
    assert sum(1 for e in loaded if e.reversal) >= 2


# --- the runner ------------------------------------------------------------------------


def test_a_burst_on_the_event_date_is_a_hit(store: Store, tmp_path: Path) -> None:
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, date(2024, 6, 1)))
    report = events.check_events(store, event_file(tmp_path))
    assert report.results[0].hit
    assert report.hit_rate == pytest.approx(1.0)
    assert report.met


def test_a_burst_inside_the_tolerance_is_a_hit(store: Store, tmp_path: Path) -> None:
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, date(2024, 6, 3)))
    assert events.check_events(store, event_file(tmp_path)).results[0].hit


def test_a_burst_outside_the_tolerance_is_a_miss_with_a_gap(store: Store, tmp_path: Path) -> None:
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, date(2024, 7, 15)))
    result = events.check_events(store, event_file(tmp_path)).results[0]
    assert not result.hit
    assert result.nearest_gap is not None and result.nearest_gap > 3


def test_no_burst_at_all_is_a_miss(store: Store, tmp_path: Path) -> None:
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, None))
    result = events.check_events(store, event_file(tmp_path)).results[0]
    assert not result.hit
    assert result.nearest_gap is None
    assert not report_met(store, tmp_path)


def report_met(store: Store, tmp_path: Path) -> bool:
    return events.check_events(store, event_file(tmp_path)).met


def test_no_coverage_at_all_is_a_miss(store: Store, tmp_path: Path) -> None:
    assert not events.check_events(store, event_file(tmp_path)).results[0].hit


# --- reversal flag accounting ----------------------------------------------------------


def test_a_reversal_event_wants_the_flag_to_fire(store: Store, tmp_path: Path) -> None:
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, date(2024, 6, 1)))
    path = event_file(tmp_path, direction="down", reversal=True)
    result = events.check_events(store, path).results[0]
    assert result.hit and result.flagged
    assert result.reversal_correct is True


def test_an_up_event_wants_the_flag_silent(store: Store, tmp_path: Path) -> None:
    """The synthetic burst carries a tone crash, so on an `up` event the flag is a false alarm."""
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, date(2024, 6, 1)))
    result = events.check_events(store, event_file(tmp_path)).results[0]
    assert result.hit and result.flagged
    assert result.reversal_correct is False


def test_flag_precision_is_none_without_hits(store: Store, tmp_path: Path) -> None:
    assert events.check_events(store, event_file(tmp_path)).flag_precision is None


# --- approximate dates and false positives ---------------------------------------------


def test_approximate_dates_are_separated(store: Store, tmp_path: Path) -> None:
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, None))
    report = events.check_events(store, event_file(tmp_path, date_confidence="approximate"))
    assert report.results[0].event.approximate
    assert report.exact == []
    assert report.exact_hit_rate == pytest.approx(0.0)


def test_false_positives_exclude_bursts_that_match_an_event(store: Store, tmp_path: Path) -> None:
    rows = flat_then_burst("theme/t", date(2024, 1, 1), 300, date(2024, 6, 1))
    store.add_coverage(rows)
    extra = events.false_positives(store, ["theme/t"], event_file(tmp_path))
    assert extra["theme/t"] == []


def test_a_burst_matching_nothing_counts_as_a_false_positive(store: Store, tmp_path: Path) -> None:
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, date(2024, 8, 20)))
    extra = events.false_positives(store, ["theme/t"], event_file(tmp_path))
    assert len(extra["theme/t"]) == 1
    assert isinstance(extra["theme/t"][0], signal.Burst)


# --- merged bursts (D110) --------------------------------------------------------------


def two_event_file(tmp_path: Path) -> Path:
    """Two events six days apart in one theme, as Stargate and DeepSeek are."""
    rows = [
        {
            "id": "first",
            "label": "First",
            "date": date(2024, 6, 1),
            "theme": "theme/t",
            "direction": "up",
            "date_confidence": "exact",
            "why": "synthetic",
        },
        {
            "id": "second",
            "label": "Second",
            # Four days after the 4-day burst ends, one past the tolerance. It was 2024-06-07
            # until D115: the old dispersion factor cut the burst's last day, so the burst ended
            # a day early and the 7th already fell outside the tolerance.
            "date": date(2024, 6, 8),
            "theme": "theme/t",
            "direction": "down",
            "reversal": True,
            "date_confidence": "exact",
            "why": "synthetic",
        },
    ]
    path = tmp_path / "events.yaml"
    path.write_text(yaml.safe_dump({"tolerance_days": 3, "min_hit_rate": 0.8, "events": rows}))
    return path


def test_two_close_events_merge_into_one_burst_and_the_twin_is_named(
    store: Store, tmp_path: Path
) -> None:
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, date(2024, 6, 1)))
    report = events.check_events(store, two_event_file(tmp_path))
    first, second = report.results
    assert first.hit and not second.hit
    # The miss is not silent: it names the event whose burst swallowed it.
    assert second.shares_burst_with == "first"
    assert second.nearest_gap is not None


def test_an_isolated_event_has_no_twin(store: Store, tmp_path: Path) -> None:
    store.add_coverage(flat_then_burst("theme/t", date(2024, 1, 1), 300, date(2024, 6, 1)))
    assert events.check_events(store, event_file(tmp_path)).results[0].shares_burst_with is None
