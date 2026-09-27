"""An LLM judge for extracted claims (Phase 1, M18 and M20, D46, D73).

Each item is a claim plus the context it came from (text read from the gitignored cache,
never stored in the repo). Items go to Codex in batches of BATCH_SIZE; the judge labels each
claim correct or incorrect, says whether the entities are resolved correctly, and gives a
one-line reason. Batches run through the job runner, so verdicts are stored and not paid for
twice.
"""

import hashlib
import random
import re
from dataclasses import dataclass
from typing import Any

from ripple.extract import keywords
from ripple.jobs import Job, JobRunner
from ripple.model import Node

BATCH_SIZE = 20
CONTEXT_CHARS = 400  # characters of text on each side of a quote
PASSAGE_CHARS = 300  # characters on each side of the best product mention
MAX_HITS = 200  # keyword matches considered per word

INSTRUCTIONS = """You check claims extracted from company annual reports.
Reply only with JSON matching the schema. Do not call tools.
For each item, read the context (text from the filing) and decide:
- correct: the context supports the claim as stated, including the role or relation;
- entities_correct: every company and product in the claim is the one the text means.
Judge only from the context; say "not supported" in the reason when it is silent.
reason: one short sentence in your own words."""


@dataclass(frozen=True)
class JudgeItem:
    id: str
    claim: str
    context: str


@dataclass(frozen=True)
class Verdict:
    correct: bool
    entities_correct: bool
    reason: str


@dataclass(frozen=True)
class Precision:
    correct: int
    total: int
    entities_correct: int

    @property
    def rate(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def entity_rate(self) -> float:
        return self.entities_correct / self.total if self.total else 0.0


def judge_schema(ids: list[str]) -> dict[str, Any]:
    verdict = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "correct", "entities_correct", "reason"],
        "properties": {
            "id": {"type": "string", "enum": ids},
            "correct": {"type": "boolean"},
            "entities_correct": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdicts"],
        "properties": {"verdicts": {"type": "array", "items": verdict}},
    }


def judge_prompt(items: list[JudgeItem]) -> str:
    blocks = [f"[{item.id}]\n{item.claim}\nContext: {item.context}" for item in items]
    return "Judge each item.\n\n" + "\n\n".join(blocks) + "\n"


def judge(items: list[JudgeItem], runner: JobRunner, kind: str) -> dict[str, Verdict]:
    batches = [items[i : i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    jobs = []
    for batch in batches:
        ids = [item.id for item in batch]
        key = hashlib.sha256("|".join(ids).encode()).hexdigest()[:16]
        jobs.append(Job(f"{kind}/{key}", judge_prompt(batch), judge_schema(ids), INSTRUCTIONS))
    results = runner.run_batch(f"judge-{kind}", jobs)
    verdicts: dict[str, Verdict] = {}
    for result in results.values():
        for v in result.data.get("verdicts", []):
            verdicts[v["id"]] = Verdict(
                bool(v["correct"]), bool(v["entities_correct"]), v["reason"]
            )
    return verdicts


def precision(verdicts: dict[str, Verdict]) -> Precision:
    return Precision(
        correct=sum(v.correct for v in verdicts.values()),
        total=len(verdicts),
        entities_correct=sum(v.entities_correct for v in verdicts.values()),
    )


def sample[T](pool: list[T], n: int, seed: int = 0) -> list[T]:
    """A deterministic sample of n items (all of them, if there are fewer)."""
    if len(pool) <= n:
        return list(pool)
    return random.Random(seed).sample(pool, n)


def context_around(text: str, start: int, end: int) -> str:
    return text[max(0, start - CONTEXT_CHARS) : min(len(text), end + CONTEXT_CHARS)]


def product_passage(text: str, node: Node) -> str:
    """The passage of the text that names the most of the node's keywords, or "" if none."""
    words = sorted(keywords(node))
    patterns = [re.compile(rf"\b{re.escape(w)}", re.I) for w in words]
    best: tuple[int, int] | None = None
    for pattern in patterns:
        for i, match in enumerate(pattern.finditer(text)):
            if i >= MAX_HITS:
                break
            window = text[max(0, match.start() - PASSAGE_CHARS) : match.end() + PASSAGE_CHARS]
            score = sum(bool(p.search(window)) for p in patterns)
            if best is None or score > best[0]:
                best = (score, match.start())
    if best is None:
        return ""
    start = best[1]
    return text[max(0, start - PASSAGE_CHARS) : start + PASSAGE_CHARS + 40]
