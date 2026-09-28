"""Per-theme query precision (Phase 2, M26). No network and no Codex: fixtures and stubs."""

from ripple import news, queries
from ripple.judge import Verdict
from ripple.model import Node


def theme(id_: str, label: str, aliases: list[str] | None = None) -> Node:
    return Node(
        id=id_,
        type="theme",
        label=label,
        kind="volume",
        aliases=aliases or [],
        query=f'("{label}")',
    )


def article(n: int, title: str = "Headline") -> news.Article:
    return news.Article(
        url=f"https://example.com/{n}",
        title=title,
        domain="example.com",
        day=None,
        language="English",
    )


def test_judge_items_carry_the_topic_and_the_headline() -> None:
    themes = {
        "theme/cpo-adoption": theme("theme/cpo-adoption", "Co-packaged optics adoption", ["CPO"])
    }
    samples = {"theme/cpo-adoption": [article(0, "Broadcom ships CPO switch")]}
    items = queries.judge_items(themes, samples)
    assert len(items) == 1
    assert "Co-packaged optics adoption" in items[0].claim
    assert "also called: CPO" in items[0].claim
    assert "Broadcom ships CPO switch" in items[0].context
    assert items[0].id == "cpo-adoption:0"


def test_summarize_counts_relevant_articles() -> None:
    samples = {"theme/t": [article(0), article(1), article(2), article(3)]}
    verdicts = {
        "t:0": Verdict(True, True, "about the topic"),
        "t:1": Verdict(True, True, "about the topic"),
        "t:2": Verdict(False, False, "unrelated use of the word"),
        "t:3": Verdict(True, True, "about the topic"),
    }
    result = queries.summarize(samples, verdicts)["theme/t"]
    assert (result.sampled, result.relevant) == (4, 3)
    assert result.rate == 0.75


def test_a_theme_at_the_bar_passes() -> None:
    samples = {"theme/t": [article(i) for i in range(20)]}
    verdicts = {f"t:{i}": Verdict(i < 17, True, "r") for i in range(20)}
    result = queries.summarize(samples, verdicts)["theme/t"]
    assert result.rate == 0.85
    assert result.met


def test_a_theme_below_the_bar_fails() -> None:
    samples = {"theme/t": [article(i) for i in range(20)]}
    verdicts = {f"t:{i}": Verdict(i < 16, True, "r") for i in range(20)}
    assert not queries.summarize(samples, verdicts)["theme/t"].met


def test_a_thin_sample_is_not_measurable() -> None:
    """A theme with almost no recent articles cannot be scored either way."""
    samples = {"theme/t": [article(0), article(1)]}
    verdicts = {"t:0": Verdict(True, True, "r"), "t:1": Verdict(True, True, "r")}
    result = queries.summarize(samples, verdicts)["theme/t"]
    assert not result.measurable
    assert not result.met


def test_unjudged_articles_are_not_counted() -> None:
    samples = {"theme/t": [article(0), article(1)]}
    result = queries.summarize(samples, {"t:0": Verdict(True, True, "r")})["theme/t"]
    assert result.sampled == 1


def test_stored_verdicts_hold_urls_not_titles() -> None:
    """Article text must never reach a repo file (D72)."""
    samples = {"theme/t": [article(0, "A headline that must not be stored")]}
    result = queries.summarize(samples, {"t:0": Verdict(True, True, "about the topic")})["theme/t"]
    stored = repr(result.verdicts)
    assert "https://example.com/0" in stored
    assert "must not be stored" not in stored
