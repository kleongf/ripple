"""The pre-registered analysis plan matches the code that will run it (Phase 4, M38).

`tests/golden/evaluation.yaml` is fixed before any burst is scored. These tests make sure the
study cannot quietly run on different numbers from the ones that were approved: every value the
code also defines must agree.
"""

from pathlib import Path

import yaml

from ripple import attention, evaluate, prices, signal
from ripple.model import DEFAULT_MIN_CONFIDENCE
from ripple.score import DEFAULT_MAX_HOPS

PLAN = Path(__file__).parent / "golden" / "evaluation.yaml"


def plan() -> dict:
    return yaml.safe_load(PLAN.read_text())


def test_the_plan_loads_and_says_what_it_is() -> None:
    p = plan()
    assert p["test"] == "backward"
    assert p["status"] in {"draft", "approved"}
    if p["status"] == "approved":
        assert p["approved"]["by"] and p["approved"]["on"]
    assert "today's graph" in p["bias_statement"]


def test_burst_rules_match_the_detector() -> None:
    bursts = plan()["bursts"]
    assert bursts["min_surprise"] == signal.MIN_SURPRISE
    assert bursts["persistence"] == signal.PERSISTENCE


def test_graph_and_attention_rules_match_the_code() -> None:
    p = plan()
    assert p["graph"]["min_confidence"] == DEFAULT_MIN_CONFIDENCE
    assert p["graph"]["max_hops"] == DEFAULT_MAX_HOPS
    assert p["attention"]["window_days"] == attention.WINDOW_DAYS
    assert p["attention"]["min_days"] == attention.MIN_DAYS


def test_return_rules_match_the_code() -> None:
    returns = plan()["returns"]
    assert returns["calendar_listing"] == evaluate.CALENDAR_LISTING
    assert returns["slack_days"] == evaluate.SLACK_DAYS
    assert returns["currency"] == "USD"
    assert {
        currency: (rule["listing"], rule["operation"]) for currency, rule in returns["fx"].items()
    } == prices.FX_BY_CURRENCY


def test_the_horizons_are_the_ones_the_code_computes() -> None:
    p = plan()
    horizons = {p["primary"]["horizon"]} | {s["horizon"] for s in p["secondary"]}
    assert horizons == set(evaluate.HORIZONS)


def test_the_primary_statistic_and_verdict_are_fully_specified() -> None:
    primary = plan()["primary"]
    assert primary["statistic"] == "mean_ic_novelty_minus_ic_exposure"
    assert primary["expected_sign"] == "positive"
    interval = primary["interval"]
    assert interval["level"] == 0.95 and interval["sides"] == "two"
    assert interval["draws"] >= 10_000 and isinstance(interval["seed"], int)
    assert set(primary["verdict"]) == {"beats", "worse", "inconclusive"}
    assert primary["min_scored_bursts"] == 30
    assert plan()["eligibility"]["min_companies"] == 5


def test_secondary_results_are_named_once() -> None:
    names = [s["name"] for s in plan()["secondary"]]
    assert len(names) == len(set(names))
    portfolios = next(s for s in plan()["secondary"] if s["name"] == "five_name_portfolios")
    assert portfolios["k"] == 5 and portfolios["min_eligible_companies"] == 10
