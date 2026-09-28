from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ripple.score import score
from ripple.store import Store

MINI = Path(__file__).parent / "fixtures" / "mini"
AS_OF = date(2026, 9, 26)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "mini.duckdb")
    s.load(MINI, now=datetime(2026, 9, 1, tzinfo=UTC))
    return s


def companies(result: object) -> list[str]:
    return [r.company for r in result.results]  # type: ignore[attr-defined]


def test_ranks_by_absolute_exposure(store: Store) -> None:
    result = score(store, "theme/ai-compute", as_of=AS_OF)
    assert companies(result) == [
        "company/nvidia",
        "company/parts",
        "company/tsmc",
        "company/multi",
        "company/asml",
        "company/vertiv",
    ]


def test_company_fields(store: Store) -> None:
    result = score(store, "theme/ai-compute", as_of=AS_OF)
    asml = next(r for r in result.results if r.company == "company/asml")
    assert asml.label == "ASML Holding"
    assert asml.ticker == "ASML"
    assert asml.exposure == pytest.approx(0.16)
    assert asml.min_hops == 4
    assert asml.guessed_weights == 0  # fixture weights are "manual", not buckets
    assert asml.path_confidence == pytest.approx(1.0)
    assert len(asml.paths[0].edges) == 4


def test_hide_obvious_drops_min_hops_two(store: Store) -> None:
    result = score(store, "theme/ai-compute", as_of=AS_OF, hide_obvious=True, obvious_hops=2)
    assert "company/nvidia" not in companies(result)
    assert "company/multi" not in companies(result)
    assert companies(result)[0] == "company/parts"


def test_hide_obvious_default_is_three_hops(store: Store) -> None:
    # Volume themes attach at the top of the chain (D7), so the makers of a theme's first
    # inputs sit 3 hops out; the default threshold is 3 (D23).
    result = score(store, "theme/ai-compute", as_of=AS_OF, hide_obvious=True)
    assert companies(result) == ["company/asml"]


def test_limit(store: Store) -> None:
    assert len(score(store, "theme/ai-compute", as_of=AS_OF, limit=2).results) == 2


def test_direction_down(store: Store) -> None:
    result = score(store, "theme/ai-compute", as_of=AS_OF, direction="down")
    assert result.results[0].exposure == pytest.approx(-0.9)
    assert result.results[0].paths[0].contribution == pytest.approx(-0.9)


def test_share_shift_includes_losers(store: Store) -> None:
    result = score(store, "theme/cooling-shift", as_of=AS_OF)
    assert result.theme_kind == "share_shift"
    assert [(r.company, round(r.exposure, 9)) for r in result.results] == [
        ("company/aircool", -0.21),
        ("company/vertiv", 0.09),
    ]


def test_min_confidence_parameter(store: Store) -> None:
    result = score(store, "theme/ai-compute", as_of=AS_OF, min_confidence=0.0)
    assert companies(result)[1] == "company/rumor"
    rumor = result.results[1]
    assert rumor.path_confidence == pytest.approx(0.3)
    assert rumor.weakest_confidence == pytest.approx(0.3)


def test_guessed_weights_counts_bucket_edges(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    text = (
        (MINI / "edges.yaml").read_text().replace("weight_source: manual", "weight_source: bucket")
    )
    (seed / "edges.yaml").write_text(text)
    (seed / "nodes.yaml").write_text((MINI / "nodes.yaml").read_text())
    with Store(tmp_path / "b.duckdb") as s:
        s.load(seed, now=datetime(2026, 9, 1, tzinfo=UTC))
        result = score(s, "theme/ai-compute", as_of=AS_OF)
    asml = next(r for r in result.results if r.company == "company/asml")
    assert asml.guessed_weights == 4


def test_unknown_theme(store: Store) -> None:
    with pytest.raises(ValueError, match="theme/nope"):
        score(store, "theme/nope", as_of=AS_OF)


def test_a_company_cannot_be_shocked(store: Store) -> None:
    with pytest.raises(ValueError, match="shock a theme, product or material"):
        score(store, "company/asml", as_of=AS_OF)


def test_product_shock_reaches_producers_and_inputs_not_customers(store: Store) -> None:
    # A unit demand shock on accelerators, by hand:
    #   nvidia  0.9 (produces accelerators)
    #   parts   0.9 x 0.5 = 0.45 (supplies Nvidia)
    #   tsmc    0.4 x 0.6 = 0.24 (accelerators require leading-edge logic)
    #   multi   0.1 + 0.4 x 0.8 x 0.2 = 0.164
    #   asml    0.4 x 0.8 x 0.5 = 0.16
    # Vertiv sits on the datacenter-capacity branch, which accelerators do not feed, and rumor's
    # edge is below the confidence threshold.
    result = score(store, "product/accelerators", as_of=AS_OF)
    exposure = {r.company: r.exposure for r in result.results}
    assert exposure == pytest.approx(
        {
            "company/nvidia": 0.9,
            "company/parts": 0.45,
            "company/tsmc": 0.24,
            "company/multi": 0.164,
            "company/asml": 0.16,
        }
    )
    assert result.shock_type == "product"
    assert result.theme_kind is None


def test_to_dict_matches_mcp_shape(store: Store) -> None:
    data = score(store, "theme/ai-compute", as_of=AS_OF).to_dict()
    assert set(data) == {
        "theme",
        "shock_type",
        "theme_kind",
        "direction",
        "as_of",
        "min_confidence",
        "max_hops",
        "results",
        "notes",
    }
    assert data["as_of"] == "2026-09-26"
    asml = next(r for r in data["results"] if r["company"] == "company/asml")
    assert set(asml) == {
        "company",
        "label",
        "ticker",
        "exposure",
        "min_hops",
        "guessed_weights",
        "path_confidence",
        "weakest_confidence",
        # Phase 2, M27: attention and novelty are null until a coverage series is held.
        "attention",
        "attention_percentile",
        "novelty",
        "paths",
    }
    assert (asml["attention"], asml["novelty"]) == (None, None)
    path = asml["paths"][0]
    assert set(path) == {"contribution", "confidence", "weakest_confidence", "nodes", "edges"}
    assert path["edges"][0].startswith("e-")
    assert asml["exposure"] == 0.16
