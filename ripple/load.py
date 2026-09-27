"""Read every YAML file under a seed directory into nodes and edges."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ripple.model import Edge, Located, Node, Problem


@dataclass
class Seed:
    nodes: list[Located[Node]] = field(default_factory=list)
    edges: list[Located[Edge]] = field(default_factory=list)
    # Files or records that could not be parsed (rule E0).
    problems: list[Problem] = field(default_factory=list)


def load_seed(directory: Path, source: str = "seed") -> Seed:
    seed = Seed()
    files = sorted(p for p in directory.rglob("*") if p.suffix in {".yaml", ".yml"})
    for file in files:
        _load_file(file, seed, source)
    return seed


def load_sources(sources: dict[str, Path]) -> Seed:
    """Every source directory in one Seed; each record remembers its source."""
    merged = Seed()
    for source, directory in sources.items():
        part = load_seed(directory, source)
        merged.nodes += part.nodes
        merged.edges += part.edges
        merged.problems += part.problems
    return merged


def _load_file(file: Path, seed: Seed, source: str) -> None:
    try:
        records = yaml.safe_load(file.read_text())
    except yaml.YAMLError as exc:
        seed.problems.append(_schema_problem(file, "(file)", f"invalid YAML: {exc}"))
        return
    if records is None:
        return
    if not isinstance(records, list):
        seed.problems.append(_schema_problem(file, "(file)", "expected a list of records"))
        return
    for index, record in enumerate(records):
        _load_record(file, index, record, seed, source)


def _load_record(file: Path, index: int, record: Any, seed: Seed, source: str) -> None:
    if not isinstance(record, dict):
        seed.problems.append(_schema_problem(file, f"#{index}", "expected a mapping"))
        return
    is_edge = "rel" in record
    name = _record_name(index, record, is_edge)
    try:
        if is_edge:
            seed.edges.append(Located(Edge.model_validate(record), file, source))
        else:
            seed.nodes.append(Located(Node.model_validate(record), file, source))
    except ValidationError as exc:
        seed.problems.append(_schema_problem(file, name, _describe(exc)))


def _record_name(index: int, record: dict[str, Any], is_edge: bool) -> str:
    if is_edge:
        return " ".join(str(record.get(k, "?")) for k in ("src", "rel", "dst"))
    return str(record.get("id", f"#{index}"))


def _describe(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"])
        parts.append(f"{location}: {err['msg']}")
    return "; ".join(parts)


def _schema_problem(file: Path, record: str, message: str) -> Problem:
    return Problem("error", "E0", file, record, message)
