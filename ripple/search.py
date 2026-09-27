"""Fuzzy lookup of node IDs by label, alias, ticker or slug."""

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from ripple.model import Node

MIN_SCORE = 0.6


@dataclass(frozen=True)
class Match:
    node: Node
    score: float

    @property
    def id(self) -> str:
        return self.node.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.node.id,
            "label": self.node.label,
            "type": self.node.type,
            "ticker": self.node.ticker,
            "kind": self.node.kind,
            "score": round(self.score, 3),
        }


def search_entities(
    nodes: dict[str, Node], query: str, type: str | None = None, limit: int = 10
) -> list[Match]:
    """Best matches first. An exact name scores 1.0, a substring 0.6-1.0, else fuzzy ratio."""
    needle = _normalize(query)
    if not needle:
        return []
    matches = []
    for node in nodes.values():
        if type is not None and node.type != type:
            continue
        best = max(_similarity(needle, name) for name in _names(node))
        if best >= MIN_SCORE:
            matches.append(Match(node, best))
    matches.sort(key=lambda m: (-m.score, m.node.label))
    return matches[:limit]


def _names(node: Node) -> list[str]:
    slug = node.id.split("/", 1)[-1].replace("-", " ")
    names = [node.label, slug, *node.aliases]
    if node.ticker:
        names.append(node.ticker)
    return [_normalize(name) for name in names]


def _similarity(needle: str, name: str) -> float:
    if needle == name:
        return 1.0
    if needle in name:
        return 0.6 + 0.4 * len(needle) / len(name)
    return SequenceMatcher(None, needle, name).ratio()


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("-", " ").split())
