"""Text extraction and span verification (M19). Codex is replaced by fakes."""

from pathlib import Path

from ripple.extract import (
    FilerExtraction,
    extraction_prompt,
    extraction_schema,
    load_extraction,
    mentions_node,
    number_in_quote,
    save_extraction,
    verify_answer,
)
from ripple.model import Node

TAXONOMY = [
    Node(
        id="product/ai-accelerators",
        type="product",
        label="AI accelerators",
        aliases=["data center GPUs"],
    ),
    Node(id="product/hbm", type="product", label="High-bandwidth memory", aliases=["HBM"]),
    Node(id="product/dram", type="product", label="Conventional DRAM", aliases=["DRAM"]),
]
COMPANIES = [
    Node(id="company/nvidia", type="company", label="Nvidia", aliases=["NVIDIA Corporation"]),
    Node(id="company/sk-hynix", type="company", label="SK hynix"),
]
TEXT = (
    "Our data center GPUs use HBM stacked next to the processor. "
    "Data center revenue was 56% of total revenue in fiscal 2025. "
    "We buy memory from SK hynix. We design AI accelerators."
)


def answer(**lists) -> dict:
    base = {"requires": [], "disclosed": [], "produces": [], "supplies": [], "new_products": []}
    base.update(lists)
    return base


def verify(data: dict) -> FilerExtraction:
    return verify_answer(
        "company/nvidia",
        "10-K",
        "1",
        section_start=1000,
        text=TEXT,
        answer=data,
        taxonomy=TAXONOMY,
        companies=COMPANIES,
    )


# Checks


def test_mentions_node_uses_label_and_alias_keywords() -> None:
    by_id = {n.id: n for n in TAXONOMY}
    assert mentions_node("Our data center GPUs use HBM", by_id["product/hbm"])
    assert mentions_node("the accelerators we sell", by_id["product/ai-accelerators"])
    assert not mentions_node("We sell storage.", by_id["product/hbm"])


def test_number_in_quote() -> None:
    assert number_in_quote("revenue was 56% of total", 0.56)
    assert number_in_quote("about 56.2 percent of sales", 0.562)
    assert not number_in_quote("revenue grew 56% year over year", 0.12)
    assert not number_in_quote("revenue was high", 0.56)


# Verification


def test_verified_requires_proposal_keeps_offsets_not_text() -> None:
    result = verify(
        answer(
            requires=[
                {
                    "product": "product/ai-accelerators",
                    "input": "product/hbm",
                    "bucket": "dominant",
                    "reason": "Accelerators need stacked memory.",
                    "quote": "Our data center GPUs use HBM stacked next to the processor",
                }
            ]
        )
    )
    [proposal] = result.requires
    assert (proposal.product, proposal.input, proposal.bucket) == (
        "product/ai-accelerators",
        "product/hbm",
        "dominant",
    )
    assert proposal.start == 1000 and proposal.end == 1000 + TEXT.index(" stacked") + len(
        " stacked next to the processor"
    )
    assert proposal.status == "pending"
    assert not hasattr(proposal, "quote")


def test_requires_without_both_endpoints_near_the_quote_is_dropped() -> None:
    result = verify(
        answer(
            requires=[
                {
                    "product": "product/ai-accelerators",
                    "input": "product/dram",
                    "bucket": "minor",
                    "reason": "x",
                    "quote": "Our data center GPUs use HBM stacked next to the processor",
                }
            ]
        )
    )
    assert result.requires == []
    assert result.dropped["requires"] == 1


def test_requires_from_a_product_to_itself_is_dropped() -> None:
    result = verify(
        answer(
            requires=[
                {
                    "product": "product/hbm",
                    "input": "product/hbm",
                    "bucket": "minor",
                    "reason": "x",
                    "quote": "use HBM",
                }
            ]
        )
    )
    assert result.requires == [] and result.dropped["requires"] == 1


def test_unfound_quote_is_dropped() -> None:
    result = verify(
        answer(
            produces=[
                {"product": "product/ai-accelerators", "quote": "a sentence that is not there"}
            ]
        )
    )
    assert result.produces == [] and result.dropped["produces"] == 1


def test_disclosed_number_must_appear_in_the_quote() -> None:
    good = {
        "description": "Data center share of revenue",
        "share": 0.56,
        "period": "FY2025",
        "basis": "end market",
        "products": ["product/ai-accelerators"],
        "quote": "Data center revenue was 56% of total revenue in fiscal 2025",
    }
    bad = {**good, "share": 0.65}
    result = verify(answer(disclosed=[good, bad]))
    assert [d.share for d in result.disclosed] == [0.56]
    assert result.dropped["disclosed"] == 1


def test_supplies_needs_both_companies_in_the_graph() -> None:
    result = verify(
        answer(
            supplies=[
                {
                    "supplier": "SK hynix",
                    "customer": "NVIDIA Corporation",
                    "product": "product/hbm",
                    "quote": "We buy memory from SK hynix",
                },
                {
                    "supplier": "Unknown Memory Co",
                    "customer": "Nvidia",
                    "product": None,
                    "quote": "We buy memory from SK hynix",
                },
            ]
        )
    )
    [supplies] = result.supplies
    assert (supplies.supplier, supplies.customer) == ("company/sk-hynix", "company/nvidia")
    assert result.dropped["supplies"] == 1


def test_new_products_are_kept_for_the_user() -> None:
    result = verify(
        answer(
            new_products=[
                {
                    "label": "NVLink switches",
                    "reason": "rack fabric",
                    "quote": "We design AI accelerators",
                }
            ]
        )
    )
    assert [p.label for p in result.new_products] == ["NVLink switches"]


# Schema, prompt and files


def test_schema_and_prompt() -> None:
    schema = extraction_schema(TAXONOMY)
    props = schema["properties"]
    assert set(props) == {"requires", "disclosed", "produces", "supplies", "new_products"}
    req = props["requires"]["items"]["properties"]
    assert req["bucket"]["enum"] == ["minor", "major", "dominant"]
    assert req["input"]["enum"] == [n.id for n in TAXONOMY]
    assert props["disclosed"]["items"]["properties"]["basis"]["enum"] == [
        "product",
        "end market",
        "customer",
        "segment",
    ]
    prompt = extraction_prompt("Nvidia", "company/nvidia", "10-K", "1", TEXT, TAXONOMY)
    assert "Nvidia" in prompt and "product/hbm" in prompt and TEXT in prompt


def test_extraction_file_round_trips(tmp_path: Path) -> None:
    result = verify(
        answer(
            requires=[
                {
                    "product": "product/ai-accelerators",
                    "input": "product/hbm",
                    "bucket": "dominant",
                    "reason": "r",
                    "quote": "use HBM",
                }
            ]
        )
    )
    path = tmp_path / "nvidia.yaml"
    save_extraction(path, result)
    assert load_extraction(path) == result
    assert "use HBM" not in path.read_text()  # offsets only (D72)


def test_extract_command_writes_files_and_projects_cost(
    tmp_path: Path, monkeypatch, write_seed
) -> None:
    import gzip
    from typing import Any

    import yaml
    from typer.testing import CliRunner

    from ripple import jobs as jobs_module
    from ripple.cli import app
    from ripple.codex import CodexResult
    from ripple.edgar import UniverseEntry, latest_cached, save_universe
    from ripple.jobs import JobRunner
    from tests.conftest import base_edges, base_nodes, node
    from tests.test_sections import tenk
    from tests.test_xbrl import cache_fixture

    raw = tmp_path / "raw"
    cache_fixture(raw)
    cached = latest_cached(raw, 1)
    assert cached is not None
    html = tenk("Item {item}. Heading").replace(
        "Lorem ipsum", "Our data center GPUs use HBM stacked in the package. Lorem ipsum", 1
    )
    cached.files["primary"].write_bytes(gzip.compress(html.encode()))
    universe = tmp_path / "universe.yaml"
    save_universe(
        universe,
        [
            UniverseEntry("company/x", "EX", 1, "10-K", True),
            UniverseEntry("company/z", "ZZ", 99, "10-K", True),
        ],
    )
    nodes = [
        *base_nodes(),
        node("product/ai-accelerators", aliases=["data center GPUs"]),
        node("product/hbm", aliases=["HBM"]),
    ]
    seed = write_seed(nodes, base_edges())
    sources = tmp_path / "sources.yaml"
    sources.write_text(yaml.safe_dump({"seed": str(seed)}))

    class Fake:
        def version(self) -> str:
            return "codex-cli test"

        def run(self, prompt: str, schema: dict[str, Any], instructions: str) -> CodexResult:
            reqs = []
            if "section 1," in prompt:
                reqs = [
                    {
                        "product": "product/ai-accelerators",
                        "input": "product/hbm",
                        "bucket": "dominant",
                        "reason": "r",
                        "quote": "Our data center GPUs use HBM",
                    }
                ]
            data = {
                "requires": reqs,
                "disclosed": [],
                "produces": [],
                "supplies": [],
                "new_products": [],
            }
            return CodexResult(data, {"input_tokens": 9000, "output_tokens": 300})

    monkeypatch.setattr(
        jobs_module, "make_codex_job_runner", lambda db, effort="medium": JobRunner(Fake(), db)
    )
    queue = tmp_path / "extract"
    result = CliRunner().invoke(
        app,
        [
            "extract",
            "--universe",
            str(universe),
            "--cache",
            str(raw),
            "--seed",
            str(seed),
            "--sources",
            str(sources),
            "--queue",
            str(queue),
            "--jobs",
            str(tmp_path / "j.duckdb"),
            "--dev",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = load_extraction(queue / "x.yaml")
    assert [(r.product, r.input) for r in saved.requires] == [
        ("product/ai-accelerators", "product/hbm")
    ]
    assert "company/z  not cached" in result.output
    assert "27,000 input tokens per filer" in result.output  # three sections x 9,000
    assert "projection for 2 SEC filers" in result.output

    # A rerun keeps the review state of items already in the queue.
    saved.requires[0].status, saved.requires[0].reviewer = "rejected", "claude-code"
    saved.requires[0].judge = False
    save_extraction(queue / "x.yaml", saved)
    again = CliRunner().invoke(
        app,
        ["extract", "--universe", str(universe), "--cache", str(raw), "--seed", str(seed),
         "--sources", str(sources), "--queue", str(queue), "--jobs", str(tmp_path / "j.duckdb")],
    )  # fmt: skip
    assert again.exit_code == 0, again.output
    kept = load_extraction(queue / "x.yaml").requires[0]
    assert (kept.status, kept.reviewer, kept.judge) == ("rejected", "claude-code", False)


# REQUIRES edges from accepted proposals (M20)


def _proposal(pid: str, product: str, needed: str, bucket: str, status: str, start: int = 10):
    from ripple.extract import RequiresProposal

    return RequiresProposal(
        pid, product, needed, bucket, f"reason {pid}", "1", start, start + 30, status
    )


def test_requires_edges_merge_accepted_proposals() -> None:
    from datetime import date

    from ripple.extract import FilerSource, requires_edges

    a = FilerExtraction("company/nvidia", "10-K")
    a.requires = [
        _proposal("r1", "product/ai-accelerators", "product/hbm", "dominant", "accepted"),
        _proposal("r2", "product/ai-accelerators", "product/dram", "minor", "rejected"),
        _proposal("r3", "product/ai-servers", "product/dram", "minor", "flagged"),
    ]
    b = FilerExtraction("company/micron", "10-K")
    b.requires = [
        _proposal("r4", "product/ai-accelerators", "product/hbm", "major", "accepted", 50),
        _proposal("r5", "product/ai-accelerators", "product/hbm", "dominant", "accepted", 90),
    ]
    sources = {
        "company/nvidia": FilerSource("https://sec.gov/nvda.htm", date(2026, 2, 25)),
        "company/micron": FilerSource("https://sec.gov/mu.htm", date(2025, 10, 1)),
    }
    edges, skipped = requires_edges(
        [a, b], sources, existing=set(), input_totals={}, accessed=date(2026, 9, 27)
    )
    [edge] = edges
    assert (edge["src"], edge["rel"], edge["dst"]) == (
        "product/ai-accelerators",
        "REQUIRES",
        "product/hbm",
    )
    assert edge["weight"] == 0.8 and edge["weight_source"] == "bucket"  # most common bucket
    assert edge["confidence"] == 0.5 and edge["valid_from"] == date(2025, 10, 1)
    spans = [e["span"] for e in edge["evidence"]]
    assert {"section": "1", "start": 10, "end": 40} in spans
    assert all(e["url"].startswith("https://sec.gov/") for e in edge["evidence"])
    assert skipped == []


def test_requires_edges_skip_existing_links_and_overfull_inputs() -> None:
    from datetime import date

    from ripple.extract import FilerSource, requires_edges

    x = FilerExtraction("company/nvidia", "10-K")
    x.requires = [
        _proposal("r1", "product/ai-accelerators", "product/hbm", "dominant", "accepted"),
        _proposal("r2", "product/ai-servers", "product/dram", "major", "accepted"),
        _proposal("r3", "product/ai-servers", "product/hbm", "minor", "accepted"),
    ]
    sources = {"company/nvidia": FilerSource("u", date(2026, 2, 25))}
    edges, skipped = requires_edges(
        [x],
        sources,
        existing={("product/ai-accelerators", "product/hbm")},
        input_totals={"product/dram": 0.9, "product/hbm": 0.8},
        accessed=date(2026, 9, 27),
    )
    assert [(e["src"], e["dst"]) for e in edges] == [("product/ai-servers", "product/hbm")]
    assert sorted(skipped) == [
        "product/ai-accelerators REQUIRES product/hbm: already in the graph",
        "product/ai-servers REQUIRES product/dram: shares into the input would exceed 1",
    ]


def test_requires_bucket_tie_takes_the_smaller_share() -> None:
    from datetime import date

    from ripple.extract import FilerSource, requires_edges

    x = FilerExtraction("company/nvidia", "10-K")
    x.requires = [
        _proposal("r1", "product/ai-servers", "product/hbm", "dominant", "accepted"),
        _proposal("r2", "product/ai-servers", "product/hbm", "minor", "accepted", 60),
    ]
    edges, _ = requires_edges(
        [x], {"company/nvidia": FilerSource("u", date(2026, 1, 1))}, set(), {}, date(2026, 9, 27)
    )
    assert edges[0]["weight"] == 0.1


# Commands: judge-extract and requires-edges (M20)


def _queue_with_proposals(tmp_path: Path):
    from ripple.edgar import latest_cached
    from ripple.extract import DisclosedNumber
    from ripple.sections import load_sections
    from tests.test_judge import _cache_with_text

    raw, universe = _cache_with_text(tmp_path, "Our data center GPUs use HBM next to the die.")
    text, _ = load_sections(latest_cached(raw, 1))
    start = text.index("Our data center GPUs")
    found = FilerExtraction("company/x", "10-K")
    found.requires = [
        _proposal("r1", "product/ai-accelerators", "product/hbm", "dominant", "pending", start),
        _proposal("r2", "product/wrong-part", "product/hbm", "minor", "pending", start),
    ]
    found.disclosed = [
        DisclosedNumber("d1", "Data center share", 0.56, "FY2025", "end market", [], "1", start,
                        start + 20)
    ]  # fmt: skip
    queue = tmp_path / "extract"
    save_extraction(queue / "x.yaml", found)
    return raw, universe, queue


def _extract_seed(write_seed):
    from tests.conftest import base_edges, base_nodes, node

    nodes = [
        *base_nodes(),
        node("product/ai-accelerators", label="AI accelerators"),
        node("product/hbm", label="High-bandwidth memory"),
        node("product/wrong-part", label="wrong part"),
    ]
    return write_seed(nodes, base_edges())


def test_judge_extract_marks_proposals_and_reports_precision(
    tmp_path: Path, monkeypatch, write_seed
) -> None:
    from typer.testing import CliRunner

    from ripple import jobs as jobs_module
    from ripple.cli import app
    from ripple.jobs import JobRunner
    from tests.test_judge import FakeJudge

    raw, universe, queue = _queue_with_proposals(tmp_path)
    seed = _extract_seed(write_seed)
    fake = FakeJudge()
    monkeypatch.setattr(
        jobs_module, "make_codex_job_runner", lambda db, effort="medium": JobRunner(fake, db)
    )
    args = ["judge-extract", "--queue", str(queue), "--universe", str(universe), "--cache",
            str(raw), "--seed", str(seed), "--jobs", str(tmp_path / "j.duckdb")]  # fmt: skip
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "REQUIRES judged: 1 of 2 correct (50%)" in result.output
    assert "disclosed judged: 1 of 1 correct (100%)" in result.output
    assert "Our data center GPUs use HBM" in fake.prompts[0]  # context read from the cache
    saved = load_extraction(queue / "x.yaml")
    status = {r.id: (r.status, r.reviewer) for r in saved.requires}
    assert status == {"r1": ("accepted", "judge"), "r2": ("flagged", "judge")}
    assert [r.judge for r in saved.requires] == [True, False]  # the verdict outlives review
    assert saved.disclosed[0].status == "accepted"
    assert "Our data center GPUs" not in (queue / "x.yaml").read_text()
    # A second run judges nothing new: only pending items go to the judge.
    again = FakeJudge()
    monkeypatch.setattr(
        jobs_module, "make_codex_job_runner", lambda db, effort="medium": JobRunner(again, db)
    )
    CliRunner().invoke(app, args)
    assert again.prompts == []


def test_requires_edges_command_writes_llm_edges(tmp_path: Path, write_seed) -> None:
    import yaml
    from typer.testing import CliRunner

    from ripple.cli import app

    raw, universe, queue = _queue_with_proposals(tmp_path)
    found = load_extraction(queue / "x.yaml")
    found.requires[0].status = "accepted"
    save_extraction(queue / "x.yaml", found)
    seed = _extract_seed(write_seed)
    sources = tmp_path / "sources.yaml"
    sources.write_text(yaml.safe_dump({"seed": str(seed), "llm": str(tmp_path / "llm")}))
    result = CliRunner().invoke(
        app,
        ["requires-edges", "--queue", str(queue), "--universe", str(universe), "--cache",
         str(raw), "--sources", str(sources), "--out", str(tmp_path / "llm")],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    edges = yaml.safe_load((tmp_path / "llm" / "edges" / "requires.yaml").read_text())
    assert [(e["src"], e["dst"], e["confidence"]) for e in edges] == [
        ("product/ai-accelerators", "product/hbm", 0.5)
    ]
    assert edges[0]["evidence"][0]["span"]["section"] == "1"
    assert "1 REQUIRES edges written" in result.output


def test_requires_recall_counts_seed_links_that_extraction_proposed() -> None:
    from ripple.extract import requires_recall

    x = FilerExtraction("company/nvidia", "10-K")
    x.requires = [
        _proposal("r1", "product/ai-accelerators", "product/hbm", "dominant", "rejected"),
        _proposal("r2", "product/ai-servers", "product/dram", "minor", "accepted"),
    ]
    seed = {
        ("product/ai-accelerators", "product/hbm"),
        ("product/ai-accelerators", "product/cowos"),
        ("product/ai-servers", "product/dram"),
        ("product/ai-servers", "product/ai-accelerators"),
    }
    found, missed = requires_recall(seed, [x])
    assert found == {
        ("product/ai-accelerators", "product/hbm"),
        ("product/ai-servers", "product/dram"),
    }
    assert missed == {
        ("product/ai-accelerators", "product/cowos"),
        ("product/ai-servers", "product/ai-accelerators"),
    }


def test_extract_report_gives_precision_status_and_recall(tmp_path: Path, write_seed) -> None:
    from typer.testing import CliRunner

    from ripple.cli import app
    from tests.conftest import base_edges, base_nodes, edge, node

    x = FilerExtraction("company/nvidia", "10-K")
    x.requires = [
        _proposal("r1", "product/ai-accelerators", "product/hbm", "dominant", "accepted"),
        _proposal("r2", "product/ai-servers", "product/hbm", "minor", "rejected"),
        _proposal("r3", "product/ai-servers", "product/ai-accelerators", "major", "pending"),
    ]
    x.requires[0].judge, x.requires[1].judge = True, False
    save_extraction(tmp_path / "q" / "nvidia.yaml", x)
    nodes = [*base_nodes(), node("product/ai-accelerators"), node("product/hbm"),
             node("product/cowos")]  # fmt: skip
    edges = [
        *base_edges(),
        edge("product/ai-accelerators", "REQUIRES", "product/hbm", 0.8),
        edge("product/ai-accelerators", "REQUIRES", "product/cowos", 0.8),
    ]
    seed = write_seed(nodes, edges)
    result = CliRunner().invoke(
        app, ["extract-report", "--queue", str(tmp_path / "q"), "--seed", str(seed)]
    )
    assert result.exit_code == 0, result.output
    assert "REQUIRES: 3 proposed; judged 2, precision 50%" in result.output
    assert "accepted 1, rejected 1, flagged 0, pending 1, applied 0" in result.output
    assert "seed REQUIRES recall: 1 of 3 (33%)" in result.output  # base_edges has one too
    assert "missed product/ai-accelerators REQUIRES product/cowos" in result.output
