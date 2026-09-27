"""The LLM judge (M18, M20). Codex is replaced by a fake."""

from pathlib import Path
from typing import Any

from ripple.codex import CodexResult
from ripple.jobs import JobRunner
from ripple.judge import (
    BATCH_SIZE,
    JudgeItem,
    judge,
    judge_prompt,
    judge_schema,
    precision,
    sample,
)


class FakeJudge:
    """Marks an item correct unless its claim contains 'wrong'."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def version(self) -> str:
        return "codex-cli test"

    def run(self, prompt: str, schema: dict[str, Any], instructions: str) -> CodexResult:
        self.prompts.append(prompt)
        ids = schema["properties"]["verdicts"]["items"]["properties"]["id"]["enum"]
        verdicts = []
        for item_id in ids:
            claim = prompt.split(f"[{item_id}]", 1)[1].split("\n", 2)[1]
            verdicts.append(
                {
                    "id": item_id,
                    "correct": "wrong" not in claim,
                    "entities_correct": "bad entity" not in claim,
                    "reason": "ok",
                }
            )
        return CodexResult({"verdicts": verdicts}, {"input_tokens": 10, "output_tokens": 5})


def items(n: int, wrong: set[int] = frozenset()) -> list[JudgeItem]:
    return [
        JudgeItem(
            id=f"i{k}",
            claim=f"claim {k}" + (" wrong" if k in wrong else ""),
            context=f"context {k}",
        )
        for k in range(n)
    ]


def test_schema_limits_ids() -> None:
    schema = judge_schema(["a", "b"])
    item = schema["properties"]["verdicts"]["items"]
    assert item["properties"]["id"]["enum"] == ["a", "b"]
    assert item["required"] == ["id", "correct", "entities_correct", "reason"]


def test_prompt_numbers_claims_and_context() -> None:
    prompt = judge_prompt(items(2))
    assert "[i0]" in prompt and "claim 1" in prompt and "context 1" in prompt


def test_judge_batches_of_twenty(tmp_path: Path) -> None:
    fake = FakeJudge()
    verdicts = judge(items(45, wrong={3, 30}), JobRunner(fake, tmp_path / "j.duckdb"), "test")
    assert len(fake.prompts) == 3  # 20 + 20 + 5
    assert BATCH_SIZE == 20
    assert len(verdicts) == 45
    assert not verdicts["i3"].correct and verdicts["i4"].correct


def test_rerun_uses_stored_verdicts(tmp_path: Path) -> None:
    db = tmp_path / "j.duckdb"
    judge(items(5), JobRunner(FakeJudge(), db), "test")
    again = FakeJudge()
    judge(items(5), JobRunner(again, db), "test")
    assert again.prompts == []


def test_precision_and_entity_accuracy(tmp_path: Path) -> None:
    batch = items(10, wrong={1, 2})
    batch[5] = JudgeItem("i5", "claim 5 bad entity", "context 5")
    verdicts = judge(batch, JobRunner(FakeJudge(), tmp_path / "j.duckdb"), "test")
    report = precision(verdicts)
    assert (report.correct, report.total) == (8, 10)
    assert report.rate == 0.8
    assert report.entities_correct == 9


def test_sample_is_deterministic_and_capped() -> None:
    pool = items(300)
    first = sample(pool, 100, seed=1)
    assert first == sample(pool, 100, seed=1)
    assert len(first) == 100 and len({i.id for i in first}) == 100
    assert sample(items(7), 100, seed=1) == items(7)  # all, if fewer


# CLI commands


def _cache_with_text(tmp_path: Path, body: str):
    import gzip

    from ripple.edgar import UniverseEntry, latest_cached, save_universe
    from tests.test_sections import tenk
    from tests.test_xbrl import cache_fixture

    raw = tmp_path / "raw"
    cache_fixture(raw)
    cached = latest_cached(raw, 1)
    assert cached is not None
    html = tenk("Item {item}. Heading").replace("Lorem ipsum", body + " Lorem ipsum", 1)
    cached.files["primary"].write_bytes(gzip.compress(html.encode()))
    universe = tmp_path / "universe.yaml"
    save_universe(universe, [UniverseEntry("company/x", "EX", 1, "10-K", True)])
    return raw, universe


def test_judge_growth_command(tmp_path: Path, monkeypatch, write_seed) -> None:
    import yaml
    from typer.testing import CliRunner

    from ripple import jobs as jobs_module
    from ripple.cli import app
    from ripple.edgar import latest_cached
    from ripple.sections import load_sections
    from tests.conftest import base_edges, base_nodes, node

    raw, universe = _cache_with_text(tmp_path, "We compete with Onto Innovation in inspection.")
    text, _ = load_sections(latest_cached(raw, 1))
    start = text.index("Onto Innovation")
    report = tmp_path / "growth.yaml"
    report.write_text(
        yaml.safe_dump(
            {
                "target": 150,
                "candidates": [
                    {
                        "name": "Onto Innovation",
                        "ticker": "ONTO",
                        "cik": 1,  # its own filing is the cached one, for the test
                        "title": "Onto Innovation Inc.",
                        "products": ["product/inspection-metrology"],
                        "decision": "added",
                        "named_by": ["company/x"],
                        "mentions": [
                            {
                                "filer": "company/x",
                                "section": "1",
                                "start": start,
                                "end": start + 15,
                                "role": "competitor",
                            }
                        ],
                    },
                    {
                        "name": "Samsung",
                        "ticker": None,
                        "cik": None,
                        "title": None,
                        "products": [],
                        "decision": "not an SEC filer",
                        "named_by": ["company/x"],
                        "mentions": [],
                    },
                ],
            }
        )
    )
    seed = write_seed([*base_nodes(), node("product/inspection-metrology")], base_edges())
    fake = FakeJudge()
    monkeypatch.setattr(
        jobs_module, "make_codex_job_runner", lambda db, effort="medium": JobRunner(fake, db)
    )
    out = tmp_path / "judge-growth.yaml"
    result = CliRunner().invoke(
        app,
        [
            "judge-growth",
            "--report",
            str(report),
            "--universe",
            str(universe),
            "--cache",
            str(raw),
            "--seed",
            str(seed),
            "--jobs",
            str(tmp_path / "j.duckdb"),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "additions judged: 1 of 1 correct (100%)" in result.output
    assert "Onto Innovation" in fake.prompts[0] and "competitor" in fake.prompts[0]
    assert "We compete with Onto Innovation" in fake.prompts[0]  # context from the cache
    # Products are judged against the addition's own business description, not world knowledge.
    assert "own business description" in fake.prompts[0]
    # The claim is the selection rule itself: at least one taxonomy product (D71).
    assert "makes at least one of: " in fake.prompts[0]
    assert "own filing names the products:\n[" in fake.prompts[0]
    saved = yaml.safe_load(out.read_text())
    assert saved["verdicts"]["Onto Innovation"]["correct"] is True
    assert "context" not in yaml.safe_dump(saved)  # no filing text written


def test_judge_mappings_command_accepts_clean_mappings(tmp_path: Path, monkeypatch) -> None:
    from datetime import date

    from typer.testing import CliRunner

    from ripple import jobs as jobs_module
    from ripple.cli import app
    from ripple.mapping import Assignment, CompanyMapping, LineMapping, load_mapping, save_mapping

    raw, universe = _cache_with_text(tmp_path, "We make wafer inspection systems.")
    queue = tmp_path / "queue"

    def mapping(node: str, product: str) -> CompanyMapping:
        return CompanyMapping(
            node=node,
            label=node,
            form="10-K",
            filing_url="u",
            fiscal_start=date(2025, 7, 1),
            fiscal_end=date(2026, 6, 30),
            currency="USD",
            total=100.0,
            kind="product",
            axis="srt:ProductOrServiceAxis",
            complete=True,
            lines=[
                LineMapping("ex:A", "Wafer Inspection", 0.6, [Assignment(product, 1.0)], None, "")
            ],
        )

    save_mapping(queue / "x.yaml", mapping("company/x", "product/inspection-metrology"))
    save_mapping(queue / "y.yaml", mapping("company/y", "product/wrong-product"))
    fake = FakeJudge()
    monkeypatch.setattr(
        jobs_module, "make_codex_job_runner", lambda db, effort="medium": JobRunner(fake, db)
    )
    result = CliRunner().invoke(
        app,
        [
            "judge-mappings",
            "--queue",
            str(queue),
            "--universe",
            str(universe),
            "--cache",
            str(raw),
            "--jobs",
            str(tmp_path / "j.duckdb"),
            "--out",
            str(tmp_path / "xbrl"),
            "--accept",
        ],
    )
    assert result.exit_code == 0, result.output
    assert load_mapping(queue / "x.yaml").status == "accepted"
    assert load_mapping(queue / "x.yaml").reviewer == "judge"
    assert load_mapping(queue / "y.yaml").status == "pending"  # flagged for Claude Code
    assert "company/y" in result.output and "flagged" in result.output


def test_product_passage_finds_where_the_filing_names_the_product() -> None:
    from ripple.judge import product_passage
    from ripple.model import Node

    switches = Node(id="product/datacenter-switches", type="product", label="Datacenter switches")
    text = (
        "Intro about the company and its data center strategy. "
        + "Filler. " * 200
        + "We sell high-speed Ethernet switches for datacenter networks. "
        + "Filler. " * 200
    )
    passage = product_passage(text, switches)
    assert "Ethernet switches for datacenter" in passage
    assert len(passage) <= 2 * 300 + 60
    assert product_passage("Nothing relevant.", switches) == ""
