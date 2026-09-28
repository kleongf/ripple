"""Attention, percentiles and novelty (Phase 2, M27)."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from ripple import attention
from ripple.store import CoverageRow, Store

AS_OF = date(2025, 6, 30)


def coverage(key: str, matched: int, days: int = 90, norm: int = 1_000_000) -> list[CoverageRow]:
    return [
        CoverageRow(
            kind="company",
            key=key,
            day=AS_OF - timedelta(days=i),
            matched=matched,
            norm=norm,
            tone=None,
            query_hash="h",
        )
        for i in range(days)
    ]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "t.duckdb")


# --- percentiles ----------------------------------------------------------------------


def test_percentiles_span_zero_to_one() -> None:
    pcts = attention.percentiles({"a": 1.0, "b": 2.0, "c": 3.0})
    assert pcts == {"a": 0.0, "b": 0.5, "c": 1.0}


def test_least_covered_company_gets_zero_and_keeps_full_novelty() -> None:
    pcts = attention.percentiles({"nvidia": 0.4, "ajinomoto": 0.00001})
    assert pcts["ajinomoto"] == 0.0
    assert attention.novelty(0.16, pcts["ajinomoto"]) == pytest.approx(0.16)
    assert attention.novelty(0.5, pcts["nvidia"]) == pytest.approx(0.0)


def test_ties_share_a_midrank() -> None:
    pcts = attention.percentiles({"a": 1.0, "b": 1.0, "c": 5.0})
    assert pcts["a"] == pcts["b"] == pytest.approx(0.25)
    assert pcts["c"] == 1.0


def test_a_single_company_is_not_called_crowded() -> None:
    assert attention.percentiles({"only": 0.9}) == {"only": 0.0}


def test_empty_group() -> None:
    assert attention.percentiles({}) == {}


def test_novelty_keeps_the_sign_of_a_negative_exposure() -> None:
    assert attention.novelty(-0.2, 0.25) == pytest.approx(-0.15)


# --- reading coverage from the store --------------------------------------------------


def test_attention_is_the_coverage_share_over_the_window(store: Store) -> None:
    store.add_coverage(coverage("company/x", matched=100))
    found = attention.company_attention(store, ["company/x"], AS_OF)
    assert found["company/x"].share == pytest.approx(100 / 1_000_000)
    assert found["company/x"].days == 90


def test_a_company_with_no_series_is_absent_not_zero(store: Store) -> None:
    """Unknown attention must not read as maximum novelty (D91)."""
    store.add_coverage(coverage("company/x", matched=10))
    found = attention.company_attention(store, ["company/x", "company/missing"], AS_OF)
    assert "company/missing" not in found


def test_too_few_days_is_treated_as_unknown(store: Store) -> None:
    store.add_coverage(coverage("company/x", matched=10, days=5))
    assert attention.company_attention(store, ["company/x"], AS_OF) == {}


def test_days_outside_the_window_are_ignored(store: Store) -> None:
    old = [
        CoverageRow(
            "company", "company/x", date(2020, 1, 1) + timedelta(days=i), 999, 1000, None, "h"
        )
        for i in range(90)
    ]
    store.add_coverage(old + coverage("company/x", matched=10))
    found = attention.company_attention(store, ["company/x"], AS_OF)
    assert found["company/x"].share == pytest.approx(10 / 1_000_000)


def test_known_at_hides_a_later_backfill(store: Store) -> None:
    """A backtest must see the attention that was measurable on the day."""
    from datetime import UTC, datetime

    store.add_coverage(coverage("company/x", matched=10), now=datetime(2025, 7, 1, tzinfo=UTC))
    store.add_coverage(coverage("company/x", matched=900), now=datetime(2025, 9, 1, tzinfo=UTC))
    now = attention.company_attention(store, ["company/x"], AS_OF)
    then = attention.company_attention(store, ["company/x"], AS_OF, known_at=date(2025, 7, 15))
    assert now["company/x"].matched > then["company/x"].matched


# --- the obvious rule -----------------------------------------------------------------


def test_attention_decides_when_it_is_known() -> None:
    # Hops would call this obvious, attention says it is not.
    assert not attention.is_obvious(percentile=0.1, hops=2)
    # And the reverse: far away but heavily covered.
    assert attention.is_obvious(percentile=0.95, hops=5)


def test_hops_are_the_fallback_when_attention_is_unknown() -> None:
    """Retiring D5 and D23 must not silently mark unmeasured companies as novel (D92)."""
    assert attention.is_obvious(percentile=None, hops=2)
    assert not attention.is_obvious(percentile=None, hops=5)


def test_nothing_is_obvious_without_attention_or_hops() -> None:
    assert not attention.is_obvious(percentile=None, hops=None)
