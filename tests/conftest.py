from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

Record = dict[str, Any]


def evidence() -> list[Record]:
    return [{"url": "https://example.com/source", "note": "test", "accessed": date(2026, 1, 1)}]


def node(id_: str, **extra: Any) -> Record:
    type_ = id_.split("/")[0]
    record: Record = {"id": id_, "type": type_, "label": id_.split("/")[1]}
    if type_ == "theme":
        record["kind"] = "volume"
    record.update(extra)
    return record


def edge(src: str, rel: str, dst: str, weight: float = 0.5, **extra: Any) -> Record:
    record: Record = {
        "src": src,
        "rel": rel,
        "dst": dst,
        "weight": weight,
        "weight_source": "bucket",
        "polarity": 1,
        "confidence": 0.9,
        "valid_from": date(2020, 1, 1),
        "valid_to": None,
        "evidence": evidence(),
    }
    record.update(extra)
    return record


def base_nodes() -> list[Record]:
    """A small graph that passes validation with no errors and no warnings."""
    return [
        node("theme/vol"),
        node("theme/shift", kind="share_shift"),
        node("product/a"),
        node("product/b"),
        node("product/c"),
        node("company/x"),
        node("company/y"),
        node("company/z"),
    ]


def base_edges() -> list[Record]:
    return [
        edge("theme/vol", "DRIVES", "product/a", 0.8),
        edge("product/a", "REQUIRES", "product/b", 0.4),
        edge("theme/shift", "DRIVES", "product/a", 0.1),
        edge("theme/shift", "DRIVES", "product/c", 0.1, polarity=-1),
        edge("company/x", "PRODUCES", "product/a", 0.6),
        edge("company/y", "PRODUCES", "product/b", 0.6),
        edge("company/z", "PRODUCES", "product/c", 0.25),
    ]


SeedWriter = Callable[[list[Record], list[Record]], Path]


@pytest.fixture
def write_seed(tmp_path: Path) -> SeedWriter:
    """Write nodes and edges as two YAML files and return the directory."""

    def write(nodes: list[Record], edges: list[Record]) -> Path:
        seed = tmp_path / "seed"
        (seed / "nodes").mkdir(parents=True, exist_ok=True)
        (seed / "edges").mkdir(parents=True, exist_ok=True)
        (seed / "nodes" / "all.yaml").write_text(yaml.safe_dump(nodes, sort_keys=False))
        (seed / "edges" / "all.yaml").write_text(yaml.safe_dump(edges, sort_keys=False))
        return seed

    return write
