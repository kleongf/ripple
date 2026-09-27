from datetime import date
from pathlib import Path

import pytest

from ripple.xbrl import (
    RevenueFacts,
    member_label,
    parse_instance,
    parse_labels,
    revenue_facts,
)

FIX = Path(__file__).parent / "fixtures" / "xbrl"


@pytest.fixture(scope="module")
def facts() -> RevenueFacts:
    parsed = parse_instance((FIX / "instance.xml").read_bytes())
    labels = parse_labels((FIX / "labels.xml").read_bytes())
    result = revenue_facts(parsed, labels)
    assert result is not None
    return result


def lines(facts: RevenueFacts, kind: str) -> dict[str, float]:
    return {line.member: round(line.share, 6) for line in facts.breakdowns[kind].lines}


def test_parse_instance_keeps_numeric_facts_only() -> None:
    parsed = parse_instance((FIX / "instance.xml").read_bytes())
    assert all(isinstance(f.value, float) for f in parsed)
    assert not any(f.concept.endswith("TextBlock") for f in parsed)
    assert not any(f.concept == "us-gaap:Revenues" for f in parsed)  # nil fact


def test_parse_instance_reads_dimensions_and_periods() -> None:
    parsed = parse_instance((FIX / "instance.xml").read_bytes())
    wafer = [
        f
        for f in parsed
        if ("srt:ProductOrServiceAxis", "ex:WaferInspectionMember") in f.dims
        and len(f.dims) == 1
        and f.start == date(2025, 7, 1)
    ]
    assert [f.value for f in wafer] == [500.0]
    instant = [f for f in parsed if f.start is None]
    assert instant and all(f.end == date(2026, 6, 30) for f in instant)


def test_labels_strip_member_suffix() -> None:
    labels = parse_labels((FIX / "labels.xml").read_bytes())
    assert labels["ex:WaferInspectionMember"] == "Wafer Inspection"
    assert labels["us-gaap:ServiceMember"] == "Service"


def test_member_label_falls_back_to_the_name() -> None:
    assert member_label("ex:OtherRevenueMember", {}) == "Other Revenue"
    assert member_label("country:CN", {}) == "CN"


def test_total_is_the_latest_annual_revenue(facts: RevenueFacts) -> None:
    assert facts.concept == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    assert (facts.start, facts.end, facts.total) == (date(2025, 7, 1), date(2026, 6, 30), 1000.0)


def test_product_breakdown_drops_the_aggregate_member(facts: RevenueFacts) -> None:
    # ProductMember (800) = Wafer 500 + Patterning 250 + Other 50, so it is an aggregate.
    assert lines(facts, "product") == {
        "ex:WaferInspectionMember": 0.5,
        "ex:PatterningMember": 0.25,
        "ex:OtherRevenueMember": 0.05,
        "us-gaap:ServiceMember": 0.2,
    }
    assert facts.breakdowns["product"].dropped == ["us-gaap:ProductMember"]
    assert facts.breakdowns["product"].complete


def test_product_lines_carry_labels(facts: RevenueFacts) -> None:
    labels = {line.member: line.label for line in facts.breakdowns["product"].lines}
    assert labels["ex:WaferInspectionMember"] == "Wafer Inspection"
    assert labels["ex:OtherRevenueMember"] == "Other Revenue"


def test_segment_breakdown_uses_operating_segments(facts: RevenueFacts) -> None:
    assert lines(facts, "segment") == {
        "ex:ProcessControlMember": 0.9,
        "ex:PcbInspectionMember": 0.1,
    }
    assert facts.breakdowns["segment"].axis == "us-gaap:StatementBusinessSegmentsAxis"


def test_geography_breakdown(facts: RevenueFacts) -> None:
    assert lines(facts, "geography") == {
        "country:CN": 0.4,
        "country:TW": 0.35,
        "srt:NorthAmericaMember": 0.25,
    }


def test_customers_use_revenue_benchmark_and_named_amounts(facts: RevenueFacts) -> None:
    shares = {c.member: round(c.share, 6) for c in facts.customers}
    assert shares == {"ex:CustomerAMember": 0.14, "ex:MicrosoftMember": 0.12}
    labels = {c.member: c.label for c in facts.customers}
    assert labels["ex:MicrosoftMember"] == "Microsoft"


def test_incomplete_breakdown_is_flagged() -> None:
    parsed = [
        f
        for f in parse_instance((FIX / "instance.xml").read_bytes())
        if ("country:TW" not in {m for _, m in f.dims})
    ]
    result = revenue_facts(parsed, {})
    assert result is not None
    assert not result.breakdowns["geography"].complete
    assert sum(line.share for line in result.breakdowns["geography"].lines) == pytest.approx(0.65)


def test_no_revenue_total_returns_none() -> None:
    parsed = [f for f in parse_instance((FIX / "instance.xml").read_bytes()) if f.dims]
    assert revenue_facts(parsed, {}) is None


def test_ifrs_axes_are_recognized() -> None:
    xml = (FIX / "instance.xml").read_text()
    xml = xml.replace(
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "ifrs-full:Revenue"
    ).replace("srt:ProductOrServiceAxis", "ifrs-full:ProductsAndServicesAxis")
    xml = xml.replace(
        "xmlns:ex=", 'xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2026" xmlns:ex='
    )
    result = revenue_facts(parse_instance(xml.encode()), {})
    assert result is not None
    assert result.concept == "ifrs-full:Revenue"
    assert result.breakdowns["product"].axis == "ifrs-full:ProductsAndServicesAxis"


def cache_fixture(root: Path, cik: int = 1) -> None:
    """Lay the synthetic instance out like the EDGAR cache (M10)."""
    import gzip
    import json

    directory = root / str(cik) / "0000000001-26-000001"
    directory.mkdir(parents=True)
    (directory / "instance.xml.gz").write_bytes(gzip.compress((FIX / "instance.xml").read_bytes()))
    (directory / "labels.xml.gz").write_bytes(gzip.compress((FIX / "labels.xml").read_bytes()))
    (directory / "primary.htm.gz").write_bytes(gzip.compress(b"<html/>"))
    meta = {
        "cik": cik,
        "accession": "0000000001-26-000001",
        "form": "10-K",
        "filing_date": "2026-08-06",
        "report_date": "2026-06-30",
        "primary_document": "ex-20260630.htm",
        "urls": {"primary": "u1", "instance": "u2", "labels": "u3"},
    }
    (directory / "meta.json").write_text(json.dumps(meta))


def test_load_revenue_from_cache(tmp_path: Path) -> None:
    from ripple.edgar import latest_cached
    from ripple.xbrl import load_revenue

    cache_fixture(tmp_path)
    cached = latest_cached(tmp_path, 1)
    assert cached is not None
    result = load_revenue(cached)
    assert result is not None and result.total == 1000.0
    assert result.breakdowns["product"].lines[0].label == "Wafer Inspection"


def test_revenue_command_reports_coverage(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ripple.cli import app
    from ripple.edgar import UniverseEntry, save_universe

    cache_fixture(tmp_path / "raw")
    universe = tmp_path / "universe.yaml"
    save_universe(
        universe,
        [
            UniverseEntry("company/ex", "EX", 1, "10-K", True),
            UniverseEntry("company/foreign", "X.T", None, None, False),
        ],
    )
    args = ["revenue", "--universe", str(universe), "--cache", str(tmp_path / "raw")]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "company/ex" in result.output
    assert "product or segment split: 1 of 1 SEC filers" in result.output

    detail = CliRunner().invoke(app, [*args, "company/ex"])
    assert detail.exit_code == 0, detail.output
    assert "Wafer Inspection" in detail.output and "50.0%" in detail.output


def test_currency_comes_from_the_total_fact_unit(facts: RevenueFacts) -> None:
    assert facts.currency == "USD"


def _facts_with(lines: dict[str, float], axis: str = "srt:ProductOrServiceAxis"):
    from ripple.xbrl import Fact

    start, end = date(2025, 1, 1), date(2025, 12, 31)
    concept = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    facts = [Fact(concept, 100.0, start, end, (), "USD")]
    facts += [Fact(concept, v, start, end, ((axis, m),), "USD") for m, v in lines.items()]
    return facts


def test_alternative_breakdowns_pick_one_partition() -> None:
    # Vertiv tags Product/Service and ProductExcludingSpares/ServicesAndSpares on one axis.
    result = revenue_facts(
        _facts_with(
            {
                "us-gaap:ProductMember": 82.0,
                "vrt:ProductExcludingSparesMember": 80.2,
                "vrt:ServicesAndSparesMember": 19.8,
                "us-gaap:ServiceMember": 18.0,
            }
        ),
        {},
    )
    assert result is not None
    breakdown = result.breakdowns["product"]
    assert breakdown.complete
    # Company-specific members win a tie over generic us-gaap members.
    assert {line.member for line in breakdown.lines} == {
        "vrt:ProductExcludingSparesMember",
        "vrt:ServicesAndSparesMember",
    }


def test_partition_prefers_more_lines() -> None:
    result = revenue_facts(
        _facts_with(
            {
                "a:BigMember": 60.0,
                "a:SmallMember": 40.0,
                "a:XMember": 30.0,
                "a:YMember": 30.0,
                "a:ZMember": 5.0,
            }
        ),
        {},
    )
    assert result is not None
    assert {line.member for line in result.breakdowns["product"].lines} == {
        "a:SmallMember",
        "a:XMember",
        "a:YMember",
    }


def test_segments_above_total_stay_partial() -> None:
    # Segment revenue before intersegment eliminations sums above the total.
    result = revenue_facts(
        _facts_with(
            {"v:AmericasMember": 62.8, "v:EmeaMember": 23.3, "v:ApacMember": 22.2},
            axis="us-gaap:StatementBusinessSegmentsAxis",
        ),
        {},
    )
    assert result is not None
    assert not result.breakdowns["segment"].complete
    assert len(result.breakdowns["segment"].lines) == 3


def test_ifrs_lowercase_member_suffix_is_stripped() -> None:
    from ripple.xbrl import _strip_suffix

    assert _strip_suffix("Wafer [member]") == "Wafer"
    assert (
        _strip_suffix("Europe Middle East and Africa [Member]") == "Europe Middle East and Africa"
    )


# Real filings, trimmed to revenue and concentration facts (KLA 10-K FY2026, ASML 20-F 2025).


def real(name: str) -> RevenueFacts:
    result = revenue_facts(
        parse_instance((FIX / f"{name}_instance.xml").read_bytes()),
        parse_labels((FIX / f"{name}_labels.xml").read_bytes()),
    )
    assert result is not None
    return result


def test_real_kla_10k() -> None:
    kla = real("kla_fy2026")
    assert (kla.end, kla.currency, kla.total) == (date(2026, 6, 30), "USD", 13_579_476_000.0)
    product = kla.breakdowns["product"]
    assert product.complete and product.dropped == ["us-gaap:ProductMember"]
    shares = {line.label: round(line.share, 3) for line in product.lines}
    assert shares["Wafer Inspection"] == 0.488
    assert shares["Patterning"] == 0.199
    assert kla.breakdowns["segment"].lines[0].label.startswith("Semiconductor Process Control")
    assert kla.breakdowns["geography"].complete


def test_real_asml_20f() -> None:
    asml = real("asml_2025")
    assert (asml.end, asml.currency) == (date(2025, 12, 31), "EUR")
    shares = {line.member: line.share for line in asml.breakdowns["product"].lines}
    euv = shares["asml:NXEMember"] + shares["asml:EXEMember"]
    assert round(euv, 3) == 0.355
    assert asml.breakdowns["product"].complete
    for breakdown in asml.breakdowns.values():
        if breakdown.complete:
            assert sum(line.share for line in breakdown.lines) <= 1.005


def test_aggregate_is_dropped_even_when_incomplete() -> None:
    # Segments miss a corporate line (sum 90 of 100) and include an aggregate: no partition
    # exists, but the aggregate must still go or it double counts.
    result = revenue_facts(
        _facts_with(
            {"a:TotalSegmentsMember": 80.0, "a:AMember": 50.0, "a:BMember": 30.0, "a:CMember": 10.0}
        ),
        {},
    )
    assert result is not None
    breakdown = result.breakdowns["product"]
    assert not breakdown.complete
    assert breakdown.dropped == ["a:TotalSegmentsMember"]
    assert {line.member for line in breakdown.lines} == {"a:AMember", "a:BMember", "a:CMember"}


def test_quarterly_total_never_wins_over_the_annual_one() -> None:
    from ripple.xbrl import Fact

    concept = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    quarter = Fact(concept, 260.0, date(2025, 10, 1), date(2025, 12, 31), (), "USD")
    year = Fact(concept, 1000.0, date(2025, 1, 1), date(2025, 12, 31), (), "USD")
    result = revenue_facts([quarter, year], {})
    assert result is not None
    assert result.total == 1000.0 and result.start == date(2025, 1, 1)
