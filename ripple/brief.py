"""Thematic briefs written through the MCP tools, and the citation checker (Phase 3, M35, D114).

`check` is the mechanical half of `tests/golden/brief-rubric.md`: it resolves every node ID,
edge ID, URL and exposure number a brief cites against the store as of the brief's date. The
reading half is the user's.

`write` runs Codex with the ripple MCP server as its only tool, on a prompt fixed in
`tests/golden/brief-prompts.yaml` before any brief was generated.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ripple.codex import CodexRunner, McpResult, McpServer
from ripple.score import score
from ripple.store import Store

DEFAULT_PROMPTS = Path("tests/golden/brief-prompts.yaml")
DEFAULT_OUT = Path("docs/briefs")

EDGE_ID = re.compile(r"\be-[0-9a-f]{10}\b")
NODE_ID = re.compile(r"\b(?:company|product|material|theme|region)/[a-z0-9]+(?:-[a-z0-9]+)*\b")
URL = re.compile(r"https?://[^\s)\]>`'\"]+")
COMPANY_ID = re.compile(r"\bcompany/[a-z0-9]+(?:-[a-z0-9]+)*\b")
EXPOSURE = re.compile(r"exposure\s*(?:of|=|:|is)?\s*([+\-−]?\d+\.\d+)", re.IGNORECASE)
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*(])|\n+")


@dataclass
class Check:
    theme: str
    as_of: date
    edges: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    # (company, number as written) pairs.
    exposures: list[tuple[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    unverified_edges: list[str] = field(default_factory=list)
    bucket_edges: list[str] = field(default_factory=list)

    @property
    def cited(self) -> int:
        return len(self.edges) + len(self.nodes) + len(self.urls) + len(self.exposures)

    @property
    def passed(self) -> bool:
        return not self.problems and bool(self.edges) and bool(self.urls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "as_of": self.as_of.isoformat(),
            "passed": self.passed,
            "cited": {
                "edges": len(self.edges),
                "nodes": len(self.nodes),
                "urls": len(self.urls),
                "exposures": len(self.exposures),
            },
            "problems": self.problems,
            "unverified_edges": self.unverified_edges,
            "bucket_edges": self.bucket_edges,
        }


def check(text: str, store: Store, theme: str, as_of: date) -> Check:
    """Resolve every citation in a brief against the store as of `as_of`."""
    result = Check(theme=theme, as_of=as_of)
    snapshot = store.snapshot(as_of)
    edges = {e.id: e for e in snapshot.edges}
    for losers in snapshot.alternatives.values():
        for _, e in losers:
            edges.setdefault(e.id, e)
    urls = {
        item.url
        for e in [*snapshot.edges, *(e for alts in snapshot.alternatives.values() for _, e in alts)]
        for item in e.evidence
        if item.url
    }

    result.edges = list(dict.fromkeys(EDGE_ID.findall(text)))
    result.nodes = list(dict.fromkeys(NODE_ID.findall(text)))
    result.urls = list(dict.fromkeys(u.rstrip(".,;:") for u in URL.findall(text)))
    for edge_id in result.edges:
        edge = edges.get(edge_id)
        if edge is None:
            result.problems.append(f"edge {edge_id} is not in the store on {as_of}")
            continue
        if not any(item.verified for item in edge.evidence):
            result.unverified_edges.append(edge_id)
        if edge.weight_source == "bucket":
            result.bucket_edges.append(edge_id)
    for node_id in result.nodes:
        if node_id not in snapshot.nodes:
            result.problems.append(f"node {node_id} is not in the store on {as_of}")
    for url in result.urls:
        if url not in urls:
            result.problems.append(f"URL {url} is not the URL of any evidence item")

    try:
        ranked = score(store, theme, as_of=as_of, limit=None, use_attention=False)
    except ValueError as exc:
        result.problems.append(str(exc))
        return result
    exposure = {r.company: r.exposure for r in ranked.results}
    for sentence in SENTENCE.split(text):
        companies = [(m.start(), m.group()) for m in COMPANY_ID.finditer(sentence)]
        if not companies:
            continue
        for number in EXPOSURE.finditer(sentence):
            # The number belongs to the company named last before it, else the first after it.
            before = [c for at, c in companies if at < number.start()]
            company = before[-1] if before else companies[0][1]
            written = number.group(1)
            result.exposures.append((company, written))
            value = float(written.replace("−", "-"))
            decimals = len(written.split(".")[1])
            actual = exposure.get(company)
            if actual is None:
                result.problems.append(f"{company} has no exposure to {theme} on {as_of}")
            elif abs(abs(value) - abs(actual)) > 0.5 * 10**-decimals + 1e-9 or (
                value < 0 < actual or actual < 0 < value
            ):
                result.problems.append(
                    f"{company}: brief says exposure {written}, find_exposed gives "
                    f"{round(actual, 4)}"
                )
    if not result.edges:
        result.problems.append("no edge ID is cited")
    if not result.urls:
        result.problems.append("no evidence URL is cited")
    return result


def load_prompts(path: Path = DEFAULT_PROMPTS) -> tuple[str, dict[str, str]]:
    data = yaml.safe_load(path.read_text())
    return data["instructions"], {t: v["prompt"] for t, v in data["themes"].items()}


def write(
    runner: CodexRunner,
    theme: str,
    server: McpServer,
    prompts: Path = DEFAULT_PROMPTS,
) -> McpResult:
    """Ask Codex for the brief on `theme`, with the ripple MCP server as its only tool."""
    instructions, by_theme = load_prompts(prompts)
    if theme not in by_theme:
        raise ValueError(f"no pre-registered prompt for {theme} in {prompts}")
    return runner.run_with_mcp(by_theme[theme], instructions, {"ripple": server})
