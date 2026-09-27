"""Universe growth, one hop (M18). Codex and SEC are replaced by fakes."""

import json
from pathlib import Path
from typing import Any

import pytest

from ripple.codex import CodexResult
from ripple.growth import (
    Candidate,
    Mention,
    NameResolver,
    growth_prompt,
    growth_schema,
    locate_quote,
    rank_candidates,
    select_additions,
)
from ripple.model import Node

TAXONOMY = [
    Node(id="product/inspection-metrology", type="product", label="Inspection and metrology"),
    Node(id="product/ai-servers", type="product", label="AI servers"),
]
TITLES = [
    (1000, "ONTO", "Onto Innovation Inc."),
    (2000, "HPE", "Hewlett Packard Enterprise Co"),
    (3000, "NVDA", "NVIDIA CORP"),
]


# Quotes become offsets


def test_locate_quote_ignores_case_and_whitespace() -> None:
    text = "Our main competitors include\nOnto  Innovation and others."
    start, end = locate_quote(text, "competitors include Onto Innovation")  # type: ignore[misc]
    assert text[start:end] == "competitors include\nOnto  Innovation"


def test_locate_quote_missing_returns_none() -> None:
    assert locate_quote("Nothing here.", "Onto Innovation") is None
    assert locate_quote("Short.", "") is None


# Name resolution


def test_resolver_accepts_a_ticker_whose_title_matches() -> None:
    resolver = NameResolver(TITLES)
    assert resolver.resolve("Onto Innovation", "ONTO") == (1000, "ONTO", "Onto Innovation Inc.")


def test_resolver_rejects_a_ticker_for_another_company() -> None:
    # A made-up or wrong ticker must not pull in an unrelated filer.
    resolver = NameResolver(TITLES)
    assert resolver.resolve("Onto Innovation", "HPE") == (1000, "ONTO", "Onto Innovation Inc.")
    assert resolver.resolve("Acme Robotics", "HPE") is None


def test_resolver_matches_names_without_suffixes() -> None:
    resolver = NameResolver(TITLES)
    assert resolver.resolve("Hewlett Packard Enterprise", None)[0] == 2000  # type: ignore[index]
    assert resolver.resolve("Samsung Electronics", None) is None


def test_resolver_prefix_match_needs_generic_remaining_words() -> None:
    # Real misses from the first growth run: a short name must not match a longer title
    # that names a different business ("Sumitomo" is not Sumitomo Mitsui Financial Group).
    resolver = NameResolver(
        [
            (1, "SMFG", "SUMITOMO MITSUI FINANCIAL GROUP, INC."),
            (2, "WAB", "WESTINGHOUSE AIR BRAKE TECHNOLOGIES CORP"),
            (3, "HTHIY", "HITACHI LTD"),
            (4, "CSCO", "CISCO SYSTEMS, INC."),
            (5, "META", "Meta Platforms, Inc."),
            (6, "AMZN", "AMAZON COM INC"),
        ]
    )
    assert resolver.resolve("Sumitomo", "SMFG") is None
    assert resolver.resolve("Westinghouse", None) is None
    assert resolver.resolve("Hitachi Construction Machinery Co., Ltd.", "HTHIY") is None
    assert resolver.resolve("Cisco", None)[0] == 4  # type: ignore[index]
    assert resolver.resolve("Meta", "META")[0] == 5  # type: ignore[index]
    assert resolver.resolve("Amazon", "AMZN")[0] == 6  # type: ignore[index]


def test_annual_form_requires_a_recent_filing() -> None:
    from datetime import date

    from ripple.edgar import Filing
    from ripple.growth import annual_form

    def client(filed: date):
        class Client:
            def latest_annual_filing(self, cik: int) -> Filing:
                return Filing(cik, "a", "20-F", filed, None, "x.htm")

        return lambda: Client()

    today = date(2026, 9, 27)
    assert annual_form(client(date(2026, 3, 1)), 1, today) == "20-F"
    assert annual_form(client(date(2024, 2, 23)), 1, today) is None  # deregistered filer


# Ranking and selection


def mention(filer: str, role: str = "competitor") -> Mention:
    return Mention(filer=filer, section="1", start=10, end=40, role=role)


def candidate(name: str, filers: list[str], products: list[str], cik: int | None) -> Candidate:
    return Candidate(
        name=name,
        ticker=None,
        cik=cik,
        title=name if cik else None,
        products=set(products),
        mentions=[mention(f) for f in filers],
    )


def test_rank_by_distinct_filers_then_mentions() -> None:
    a = candidate("A", ["company/x", "company/y"], ["product/ai-servers"], 1)
    b = candidate("B", ["company/x", "company/x", "company/x"], ["product/ai-servers"], 2)
    c = candidate("C", ["company/x", "company/y", "company/z"], ["product/ai-servers"], 3)
    assert [c.name for c in rank_candidates([a, b, c])] == ["C", "A", "B"]


def test_select_additions_applies_the_rules_and_the_cap() -> None:
    cands = [
        candidate("Top", ["company/x", "company/y"], ["product/ai-servers"], 1),
        candidate("NoProduct", ["company/x", "company/y"], [], 2),
        candidate("NotSec", ["company/x", "company/y"], ["product/ai-servers"], None),
        candidate("Second", ["company/x"], ["product/inspection-metrology"], 3),
        candidate("Third", ["company/x"], ["product/ai-servers"], 4),
        candidate("Known", ["company/x", "company/y"], ["product/ai-servers"], 5),
    ]
    annual = {1: "10-K", 3: "20-F", 4: "10-K", 5: "10-K"}
    decisions = select_additions(
        cands, known_ciks={5}, cap=2, annual_form=lambda cik: annual.get(cik)
    )
    assert decisions == {
        "Top": "added",
        "Second": "added",
        "Third": "over cap",
        "NoProduct": "makes no taxonomy product",
        "NotSec": "not an SEC filer",
        "Known": "already in the universe",
    }


def test_rejected_in_review_is_not_added_and_frees_its_slot() -> None:
    cands = [
        candidate("Top", ["company/x", "company/y"], ["product/ai-servers"], 1),
        candidate("Second", ["company/x"], ["product/ai-servers"], 2),
    ]
    decisions = select_additions(
        cands,
        known_ciks=set(),
        cap=1,
        annual_form=lambda cik: "10-K",
        rejected={1: "contract manufacturer"},
    )
    assert decisions == {"Top": "rejected in review: contract manufacturer", "Second": "added"}


def test_filer_without_annual_report_is_not_added() -> None:
    cands = [candidate("Top", ["company/x"], ["product/ai-servers"], 1)]
    decisions = select_additions(cands, known_ciks=set(), cap=5, annual_form=lambda cik: None)
    assert decisions == {"Top": "no annual report"}


# Prompt and schema


def test_schema_and_prompt() -> None:
    schema = growth_schema(TAXONOMY)
    item = schema["properties"]["companies"]["items"]
    assert item["properties"]["role"]["enum"] == ["supplier", "customer", "competitor", "partner"]
    assert item["properties"]["products"]["items"]["enum"] == [n.id for n in TAXONOMY]
    assert item["properties"]["ticker"]["type"] == ["string", "null"]
    prompt = growth_prompt("KLA", "company/kla", "10-K", "1", "Section body text.", TAXONOMY)
    assert "Section body text." in prompt and "product/ai-servers" in prompt and "KLA" in prompt


# End to end with fakes


class FakeRunner:
    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers

    def version(self) -> str:
        return "codex-cli test"

    def run(self, prompt: str, schema: dict[str, Any], instructions: str) -> CodexResult:
        for marker, answer in self.answers.items():
            if marker in prompt:
                return CodexResult(answer, {"input_tokens": 1000, "output_tokens": 50})
        return CodexResult({"companies": []}, {})


def test_grow_command_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_seed
) -> None:
    import gzip

    import yaml
    from typer.testing import CliRunner

    from ripple import growth as growth_module
    from ripple.cli import app
    from ripple.edgar import UniverseEntry, latest_cached, load_universe, save_universe
    from tests.conftest import base_edges, base_nodes, node
    from tests.test_sections import tenk
    from tests.test_xbrl import cache_fixture

    raw = tmp_path / "raw"
    cache_fixture(raw)
    cached = latest_cached(raw, 1)
    assert cached is not None
    html = tenk("Item {item}. Heading").replace(
        "Lorem ipsum", "We compete with Onto Innovation in inspection. Lorem ipsum", 1
    )
    cached.files["primary"].write_bytes(gzip.compress(html.encode()))
    (raw / "company_tickers.json").write_text(
        json.dumps(
            {str(i): {"cik_str": c, "ticker": t, "title": n} for i, (c, t, n) in enumerate(TITLES)}
        )
    )
    universe = tmp_path / "universe.yaml"
    save_universe(universe, [UniverseEntry("company/x", "EX", 1, "10-K", True)])
    seed = write_seed([*base_nodes(), node("product/inspection-metrology")], base_edges())
    answer = {
        "companies": [
            {
                "name": "Onto Innovation",
                "ticker": "ONTO",
                "role": "competitor",
                "products": ["product/inspection-metrology"],
                "quote": "We compete with Onto Innovation",
            },
            {
                "name": "Samsung Electronics",
                "ticker": None,
                "role": "customer",
                "products": ["product/inspection-metrology"],
                "quote": "made up quote not in the text",
            },
        ]
    }
    monkeypatch.setattr(
        growth_module,
        "make_job_runner",
        lambda db: growth_module.JobRunner(FakeRunner({"Item 1.": answer}), db),
    )
    monkeypatch.setattr(growth_module, "annual_form", lambda client, cik, as_of: "10-K")
    out = tmp_path / "llm"
    # An earlier run added company/stale; a rerun regenerates the additions from scratch.
    (out / "nodes").mkdir(parents=True)
    (out / "nodes" / "companies.yaml").write_text(
        yaml.safe_dump([{"id": "company/stale", "type": "company", "label": "Stale"}])
    )
    save_universe(
        universe,
        [
            UniverseEntry("company/x", "EX", 1, "10-K", True),
            UniverseEntry("company/stale", "ST", 4000, "10-K", True),
        ],
    )
    result = CliRunner().invoke(
        app,
        [
            "grow",
            "--universe",
            str(universe),
            "--cache",
            str(raw),
            "--seed",
            str(seed),
            "--out",
            str(out),
            "--report",
            str(tmp_path / "growth.yaml"),
            "--jobs",
            str(tmp_path / "jobs.duckdb"),
            "--target",
            "10",
            "--todo",
            str(tmp_path / "TODO.md"),
        ],
    )
    assert result.exit_code == 0, result.output
    entries = {e.node: e for e in load_universe(universe)}
    assert entries["company/onto-innovation"].cik == 1000
    assert set(entries) == {"company/x", "company/onto-innovation"}
    nodes = yaml.safe_load((out / "nodes" / "companies.yaml").read_text())
    assert [n["id"] for n in nodes] == ["company/onto-innovation"]
    assert nodes[0]["ticker"] == "ONTO"
    report = yaml.safe_load((tmp_path / "growth.yaml").read_text())
    by_name = {c["name"]: c for c in report["candidates"]}
    assert by_name["Onto Innovation"]["decision"] == "added"
    mention = by_name["Onto Innovation"]["mentions"][0]
    assert set(mention) == {"filer", "section", "start", "end", "role"}  # offsets, no text
    assert "Samsung Electronics" not in by_name  # its quote is not in the text: dropped
    assert "dropped 1 mention whose quote was not found" in result.output
    todo = (tmp_path / "TODO.md").read_text()
    assert "not an SEC filer" not in todo or "Samsung" not in todo  # Samsung's quote was dropped


def test_a_filer_never_names_itself() -> None:
    from ripple.growth import collect_candidates

    text = "NVIDIA competes with Onto Innovation in inspection."
    answer = {
        "companies": [
            {
                "name": "NVIDIA",
                "ticker": "NVDA",
                "role": "partner",
                "products": [],
                "quote": "NVIDIA",
            },
            {
                "name": "Onto Innovation",
                "ticker": "ONTO",
                "role": "competitor",
                "products": ["product/inspection-metrology"],
                "quote": "Onto Innovation",
            },
        ]
    }
    candidates, dropped = collect_candidates(
        [("company/nvidia", "1", 100, text, answer)],
        NameResolver(TITLES),
        {"product/inspection-metrology"},
        {"company/nvidia": 3000},
    )
    assert [c.name for c in candidates] == ["Onto Innovation"]
    assert dropped == 0
    assert candidates[0].mentions[0].start == 100 + text.index("Onto")  # absolute offsets
