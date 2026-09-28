"""Burst detection on synthetic series with known answers (Phase 2, M25).

Each case exists to pin one behaviour of the statistic. They are the burst equivalent of
tests/fixtures/mini/: if a change moves an answer here, the change is wrong or the case needs a
written reason.
"""

from datetime import date, timedelta

import pytest

from ripple import signal
from ripple.store import CoverageRow

DAY0 = date(2025, 1, 1)
NORM = 500_000


def series(counts: list[int], tones: list[float] | None = None) -> list[CoverageRow]:
    return [
        CoverageRow(
            kind="theme",
            key="theme/t",
            day=DAY0 + timedelta(days=i),
            matched=count,
            norm=NORM,
            tone=None if tones is None else tones[i],
            query_hash="h",
        )
        for i, count in enumerate(counts)
    ]


def weekday_counts(n: int, weekday: int, weekend: int) -> list[int]:
    """Raw counts that dip at weekends, with the denominator dipping in step."""
    return [weekend if (DAY0 + timedelta(days=i)).weekday() >= 5 else weekday for i in range(n)]


# --- the five cases the plan names ----------------------------------------------------


def test_flat_series_has_no_burst() -> None:
    assert signal.bursts("theme/t", series([40] * 200)) == []


def test_a_sustained_step_is_one_burst_with_the_right_window() -> None:
    rows = series([40] * 150 + [400] * 10 + [40] * 40)
    found = signal.bursts("theme/t", rows)
    assert len(found) == 1
    burst = found[0]
    assert burst.start == DAY0 + timedelta(days=150)
    # The step raises its own baseline once it is more than EXCLUDE_RECENT days old, so the
    # burst ends before the elevated level does. That is the intended behaviour: a new normal
    # is not news.
    assert burst.end <= DAY0 + timedelta(days=159)
    assert burst.peak_ratio == pytest.approx(10.0, rel=0.05)


def test_a_one_day_spike_fails_persistence() -> None:
    rows = series([40] * 150 + [400] + [40] * 40)
    assert signal.bursts("theme/t", rows) == []


def test_a_one_day_spike_is_still_visible_in_the_day_statistic() -> None:
    """Persistence suppresses the burst, not the measurement."""
    rows = series([40] * 150 + [400] + [40] * 40)
    spike = next(s for s in signal.day_stats(rows) if s.day == DAY0 + timedelta(days=150))
    assert spike.surprise > signal.MIN_SURPRISE
    assert spike.ratio == pytest.approx(10.0, rel=0.05)


def test_a_thin_theme_does_not_burst_on_one_extra_article() -> None:
    """1 -> 2 articles is the case a Gaussian z on counts would call a 5-sigma event."""
    rows = series([1, 0, 1, 2, 0, 1] * 30 + [2, 2, 2])
    assert signal.bursts("theme/t", rows) == []


def test_a_weekend_dip_does_not_burst() -> None:
    """Share, not count, is the input: the denominator dips at weekends too (D89)."""
    rows = [
        CoverageRow(
            "theme",
            "theme/t",
            DAY0 + timedelta(days=i),
            matched=m,
            norm=n,
            tone=None,
            query_hash="h",
        )
        for i, (m, n) in enumerate(
            zip(weekday_counts(200, 40, 12), weekday_counts(200, NORM, NORM * 3 // 10), strict=True)
        )
    ]
    assert signal.bursts("theme/t", rows) == []


# --- the statistic itself --------------------------------------------------------------


def test_short_history_produces_no_statistics() -> None:
    assert signal.day_stats(series([40] * 20)) == []


def test_baseline_excludes_the_most_recent_days() -> None:
    """A burst building over EXCLUDE_RECENT days must not raise its own baseline."""
    rows = series([40] * 150 + [300, 300, 300, 300])
    last = signal.day_stats(rows)[-1]
    # rel, not exact: the baseline carries a half-article of smoothing so it is never zero (D106).
    assert last.baseline == pytest.approx(40 / NORM, rel=0.01)


def test_gaps_are_not_zeros() -> None:
    """A missing day shortens the window instead of dragging the baseline down."""
    rows = [r for r in series([40] * 200) if r.day.day != 15]
    stats = signal.day_stats(rows)
    assert all(s.baseline == pytest.approx(40 / NORM, rel=0.01) for s in stats)


def test_dispersion_is_one_for_a_steady_thin_series() -> None:
    """Where Poisson is the right model, the correction does nothing (D103)."""
    rows = series([1, 2, 1, 2, 1, 2] * 30)
    assert signal.day_stats(rows)[-1].dispersion == pytest.approx(1.0, abs=0.6)


def test_dispersion_rises_for_a_clustered_series() -> None:
    rows = series(([5] * 10 + [200] * 3) * 20)
    assert signal.day_stats(rows)[-1].dispersion > 5


def test_dispersion_makes_a_clustered_series_harder_to_burst() -> None:
    """The same relative jump clears the bar on a steady series and not on a noisy one."""
    steady = series([40] * 150 + [200] * 5)
    noisy = series(([40] * 5 + [5] * 5 + [120] * 5) * 10 + [200] * 5)
    assert signal.bursts("theme/t", steady) != []
    assert signal.bursts("theme/t", noisy) == []


def test_article_floor_blocks_a_tiny_but_relatively_huge_jump() -> None:
    rows = series([0] * 150 + [3, 3, 3])
    assert signal.bursts("theme/t", rows) == []


# --- tone -----------------------------------------------------------------------------


def test_sharply_negative_tone_flags_a_possible_reversal() -> None:
    counts = [40] * 150 + [400] * 5
    tones = [-1.0 - (i % 3) * 0.1 for i in range(150)] + [-6.0] * 5
    found = signal.bursts("theme/t", series(counts, tones))
    assert len(found) == 1
    assert found[0].tone_flag == signal.REVERSAL
    assert found[0].tone_z is not None and found[0].tone_z < -signal.TONE_FLAG_Z


def test_ordinary_tone_does_not_flag() -> None:
    counts = [40] * 150 + [400] * 5
    tones = [-1.0 - (i % 3) * 0.1 for i in range(150)] + [-1.1] * 5
    found = signal.bursts("theme/t", series(counts, tones))
    assert len(found) == 1
    assert found[0].tone_flag is None


def test_tone_flag_never_changes_the_burst_itself() -> None:
    """Tone is a flag, never a sign (D90): the window is identical with and without it."""
    counts = [40] * 150 + [400] * 5
    # The baseline tone has to vary, or there is no scale to standardize against.
    tones = [-1.0 - (i % 3) * 0.1 for i in range(150)] + [-9.0] * 5
    without = signal.bursts("theme/t", series(counts))
    with_tone = signal.bursts("theme/t", series(counts, tones))
    assert (without[0].start, without[0].end) == (with_tone[0].start, with_tone[0].end)
    assert without[0].tone_flag is None
    assert with_tone[0].tone_flag == signal.REVERSAL


def test_absolute_tone_level_is_irrelevant() -> None:
    """A theme that is always negative should not be permanently flagged."""
    counts = [40] * 150 + [400] * 5
    tones = [-8.0 - (i % 3) * 0.1 for i in range(155)]
    found = signal.bursts("theme/t", series(counts, tones))
    assert found[0].tone_flag is None


# --- burst helpers --------------------------------------------------------------------


def test_covers_applies_a_tolerance() -> None:
    burst = signal.Burst(
        key="theme/t",
        start=date(2025, 3, 10),
        end=date(2025, 3, 12),
        peak_day=date(2025, 3, 11),
        peak_surprise=20.0,
        peak_ratio=9.0,
        matched=100,
        tone_flag=None,
        tone_z=None,
    )
    assert burst.covers(date(2025, 3, 11))
    assert not burst.covers(date(2025, 3, 14))
    assert burst.covers(date(2025, 3, 14), tolerance=3)
    assert burst.days == 3


# --- zero-baseline degeneracy (D106) ---------------------------------------------------


def test_a_silent_theme_does_not_make_one_article_infinitely_surprising() -> None:
    """The bug this guards: a median share of exactly 0 used to pin surprise to its cap, so a
    single article on a mostly-silent theme scored higher than a real event on a busy one."""
    rows = series([0] * 150 + [1, 1, 1])
    last = signal.day_stats(rows)[-1]
    assert last.expected >= signal.MIN_EXPECTED
    assert last.ratio < 10
    assert last.surprise < signal.MIN_SURPRISE
    assert signal.bursts("theme/t", rows) == []


def test_ratio_is_always_finite() -> None:
    rows = series([0] * 100 + [50] * 10 + [0] * 40)
    assert all(s.ratio < float("inf") for s in signal.day_stats(rows))


def test_a_real_jump_on_a_silent_theme_still_bursts() -> None:
    """Fixing the degeneracy must not make the detector blind to genuine onsets."""
    rows = series([0] * 150 + [80] * 4)
    found = signal.bursts("theme/t", rows)
    assert len(found) == 1
    assert found[0].peak_ratio > 10


# --- threshold calibration (D107) ------------------------------------------------------


def test_calibration_picks_a_threshold_that_hits_the_target_rate() -> None:
    """Calibration reads only coverage, never event dates, so the event set stays out-of-sample."""
    # Four years of a noisy series with occasional real jumps.
    counts: list[int] = []
    for block in range(48):
        counts += [40] * 25 + ([300] * 5 if block % 6 == 0 else [45] * 5)
    result = signal.calibrate_threshold({"theme/t": series(counts)}, target=4.0)
    assert result.rate <= result.target
    assert result.threshold in signal.CALIBRATION_GRID
    assert result.series == 1


def test_calibration_rate_falls_as_the_threshold_rises() -> None:
    counts: list[int] = []
    for _ in range(30):
        counts += [40] * 20 + [250] * 4
    curve = signal.calibrate_threshold({"theme/t": series(counts)}).curve
    rates = [rate for _, rate in curve]
    assert rates == sorted(rates, reverse=True)


def test_calibration_with_no_series_falls_back_to_the_default() -> None:
    result = signal.calibrate_threshold({})
    assert result.threshold == signal.MIN_SURPRISE
    assert result.series == 0


# --- two events close together (D110, D115) -------------------------------------------
#
# Designed on synthetic series only, before any event date was looked at (D101).

WOBBLE = [38, 42, 40, 44, 36, 41, 39] * 30


def test_a_recent_burst_does_not_hide_a_second_one() -> None:
    """Two identical spikes eight days apart: the first used to raise the dispersion factor
    about a hundredfold, so the second scored under 3 against 245 (D110)."""
    counts = WOBBLE[:150] + [400] * 3 + [40] * 5 + [400] * 3 + [40] * 40
    found = signal.bursts("theme/t", series(counts))
    assert len(found) == 2
    first, second = found
    assert second.peak_surprise == pytest.approx(first.peak_surprise, rel=0.1)


def test_trimming_leaves_chronic_clustering_visible() -> None:
    rows = series(([5] * 10 + [200] * 3) * 20)
    assert signal.day_stats(rows)[-1].dispersion > 5


def test_two_peaks_inside_one_run_are_split_at_the_trough() -> None:
    """Every day stays over the threshold, but surprise falls by more than half between two
    peaks, so the run reads as two events."""
    counts = WOBBLE[:150] + [400, 400, 400, 120, 400, 400, 400] + [40] * 40
    found = signal.bursts("theme/t", series(counts))
    assert len(found) == 2
    assert found[0].end == DAY0 + timedelta(days=152)
    assert found[1].start == DAY0 + timedelta(days=153)


def test_a_wobble_inside_one_burst_is_not_split() -> None:
    counts = WOBBLE[:150] + [400, 380, 330, 390, 400] + [40] * 40
    assert len(signal.bursts("theme/t", series(counts))) == 1


def test_a_decaying_burst_is_not_split() -> None:
    counts = WOBBLE[:150] + [400, 300, 200, 150, 120] + [40] * 40
    assert len(signal.bursts("theme/t", series(counts))) == 1


def test_a_split_never_leaves_a_half_shorter_than_persistence() -> None:
    counts = WOBBLE[:150] + [400, 100, 400, 400, 400] + [40] * 40
    for burst in signal.bursts("theme/t", series(counts)):
        assert burst.days >= signal.PERSISTENCE
