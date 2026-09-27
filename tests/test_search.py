from ripple.model import Node
from ripple.search import search_entities

NODES = {
    n.id: n
    for n in [
        Node(
            id="company/nvidia",
            type="company",
            label="Nvidia",
            ticker="NVDA",
            aliases=["NVIDIA Corporation"],
        ),
        Node(id="company/asml", type="company", label="ASML Holding", ticker="ASML"),
        Node(
            id="product/euv",
            type="product",
            label="EUV lithography",
            aliases=["extreme ultraviolet lithography"],
        ),
        Node(id="product/duv", type="product", label="DUV lithography"),
        Node(id="theme/ai-compute", type="theme", kind="volume", label="AI compute demand"),
    ]
}


def ids(query: str, **kwargs: object) -> list[str]:
    return [m.id for m in search_entities(NODES, query, **kwargs)]  # type: ignore[arg-type]


def test_exact_ticker() -> None:
    assert ids("NVDA")[0] == "company/nvidia"


def test_label_substring() -> None:
    assert ids("asml")[0] == "company/asml"


def test_alias() -> None:
    assert ids("extreme ultraviolet")[0] == "product/euv"


def test_fuzzy_misspelling() -> None:
    assert ids("Nvida")[0] == "company/nvidia"


def test_type_filter() -> None:
    assert sorted(ids("lithography", type="product")) == ["product/duv", "product/euv"]
    assert ids("lithography", type="company") == []


def test_theme_by_words() -> None:
    assert ids("AI compute")[0] == "theme/ai-compute"


def test_no_match() -> None:
    assert ids("zzzz") == []


def test_limit() -> None:
    assert len(ids("lithography", limit=1)) == 1


def test_match_fields() -> None:
    [match] = search_entities(NODES, "ASML", limit=1)
    assert match.to_dict() == {
        "id": "company/asml",
        "label": "ASML Holding",
        "type": "company",
        "ticker": "ASML",
        "kind": None,
        "score": 1.0,
    }
