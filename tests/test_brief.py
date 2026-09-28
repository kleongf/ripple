"""The brief citation checker and the pre-registered prompts (Phase 3, M35, D114)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ripple import brief
from ripple.cli import app
from ripple.model import Edge
from ripple.store import Store

MINI = Path(__file__).parent / "fixtures" / "mini"
AS_OF = date(2026, 9, 26)
URL = "https://example.com/fixture"


def edge_id(src: str, rel: str, dst: str) -> str:
    return Edge(
        src=src,
        rel=rel,
        dst=dst,
        weight=0.5,
        weight_source="manual",
        polarity=1,
        confidence=1.0,
        valid_from="2020-01-01",
    ).id


ASML_EUV = edge_id("company/asml", "PRODUCES", "product/euv")


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "mini.duckdb")
    s.load(MINI, now=datetime(2026, 9, 1, tzinfo=UTC))
    return s


GOOD = f"""# AI compute

As of 2026-09-26. `company/asml` has exposure 0.16 through EUV (`{ASML_EUV}`), a
manual weight; the source is unverified ({URL}). `company/nvidia` has exposure 0.9 and
`company/parts` has exposure 0.45 through its sales to Nvidia.
"""


def test_a_well_cited_brief_passes(store: Store) -> None:
    result = brief.check(GOOD, store, "theme/ai-compute", AS_OF)
    assert result.problems == []
    assert result.passed
    assert result.edges == [ASML_EUV]
    assert result.urls == [URL]
    assert result.exposures == [
        ("company/asml", "0.16"),
        ("company/nvidia", "0.9"),
        ("company/parts", "0.45"),
    ]
    assert result.unverified_edges == [ASML_EUV]


def test_bad_edge_mismatched_number_and_foreign_url_all_fail(store: Store) -> None:
    text = (
        "`company/asml` has exposure 0.25 via `e-0000000000`. See "
        "https://www.reuters.com/some-article and " + URL + "."
    )
    result = brief.check(text, store, "theme/ai-compute", AS_OF)
    assert not result.passed
    joined = " | ".join(result.problems)
    assert "e-0000000000 is not in the store" in joined
    assert "brief says exposure 0.25, find_exposed gives 0.16" in joined
    assert "reuters.com" in joined
    assert URL not in joined  # the trailing full stop is not part of the URL


def test_precision_follows_what_was_written(store: Store) -> None:
    # 0.2 is not 0.16 to one decimal place; 0.2 would need the true value in [0.15, 0.25).
    close = brief.check(
        f"`company/asml` exposure 0.2 `{ASML_EUV}` {URL}", store, "theme/ai-compute", AS_OF
    )
    assert close.passed
    wrong = brief.check(
        f"`company/asml` exposure 0.1 `{ASML_EUV}` {URL}", store, "theme/ai-compute", AS_OF
    )
    assert not wrong.passed


def test_a_sign_error_fails(store: Store) -> None:
    text = f"`company/aircool` has exposure 0.21 `{ASML_EUV}` {URL}"
    result = brief.check(text, store, "theme/cooling-shift", AS_OF)
    assert any("gives -0.21" in p for p in result.problems)


def test_an_unknown_node_and_an_unexposed_company_fail(store: Store) -> None:
    text = f"`company/ghost` and `company/aircool` exposure 0.3. `{ASML_EUV}` {URL}"
    result = brief.check(text, store, "theme/ai-compute", AS_OF)
    joined = " | ".join(result.problems)
    assert "node company/ghost is not in the store" in joined
    assert "company/aircool has no exposure to theme/ai-compute" in joined


def test_a_brief_with_no_citations_is_not_sourced(store: Store) -> None:
    result = brief.check("Nvidia is exposed to AI.", store, "theme/ai-compute", AS_OF)
    assert not result.passed
    assert "no edge ID is cited" in result.problems


def test_check_brief_command_exit_codes(store: Store, tmp_path: Path) -> None:
    db = tmp_path / "mini.duckdb"
    store.close()
    good, bad = tmp_path / "good.md", tmp_path / "bad.md"
    good.write_text(GOOD)
    bad.write_text("`company/asml` has exposure 0.99.")
    args = ["--theme", "theme/ai-compute", "--as-of", "2026-09-26", "--db", str(db)]
    runner = CliRunner()
    passed = runner.invoke(app, ["check-brief", str(good), *args])
    assert passed.exit_code == 0, passed.output
    assert "mechanical check: pass" in passed.output
    failed = runner.invoke(app, ["check-brief", str(bad), *args])
    assert failed.exit_code == 1
    assert "FAIL" in failed.output


def test_the_pre_registered_prompts_cover_three_themes_that_exist() -> None:
    instructions, prompts = brief.load_prompts()
    assert set(prompts) == {
        "theme/ai-compute-demand",
        "theme/datacenter-power-demand",
        "theme/liquid-cooling-adoption",
    }
    assert "exposure <number>" in instructions
    import yaml

    themes = {n["id"] for n in yaml.safe_load(Path("data/seed/nodes/themes.yaml").read_text())}
    assert set(prompts) <= themes
