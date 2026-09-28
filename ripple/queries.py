"""Measure how precise each theme's GDELT query is (Phase 2, M26).

A burst only means something if the articles behind the query are actually about the theme. For
each theme a sample of recent articles is judged for relevance, and the per-theme precision is
reported against an 85% bar, the same bar Phase 1 used for extracted edges.

**Tuning rule (D104).** Queries are refined against *relevance* only, never against whether a
burst lines up with `tests/golden/events.yaml`. Tuning on burst alignment would make the M29
event measurement lookahead, since the event set was fixed in advance. The two measurements stay
independent on purpose.

Article titles are sent to the judge but never written to disk: the stored record is the URL,
the verdict and the judge's own-words reason, the same rule spans follow (D72).
"""

from dataclasses import dataclass
from datetime import date, timedelta

from ripple import news
from ripple.jobs import JobRunner
from ripple.judge import JudgeItem, Verdict, judge
from ripple.model import Node

SAMPLE_SIZE = 20
# ArtList only sees the last three months whatever window is asked for (D85).
SAMPLE_DAYS = 90
PRECISION_BAR = 0.85
# Below this many sampled articles a precision number is not worth reporting.
MIN_SAMPLE = 5

INSTRUCTIONS = """You check whether news articles are about a given topic.
Reply only with JSON matching the schema. Do not call tools.
For each item you get a topic and an article headline with its publisher.
- correct: the headline is plausibly an article about that topic, in the business or technology
  sense the topic describes. A passing mention of a related word is not enough.
- entities_correct: set the same value as correct; this check has no entities.
reason: one short sentence in your own words."""


@dataclass(frozen=True)
class ThemePrecision:
    theme: str
    sampled: int
    relevant: int
    # (url, correct, reason) per judged article. No titles: they are article text (D72).
    verdicts: list[tuple[str, bool, str]]

    @property
    def rate(self) -> float:
        return self.relevant / self.sampled if self.sampled else 0.0

    @property
    def measurable(self) -> bool:
        return self.sampled >= MIN_SAMPLE

    @property
    def met(self) -> bool:
        return self.measurable and self.rate >= PRECISION_BAR


def sample_articles(
    client: news.NewsClient,
    themes: list[Node],
    as_of: date,
    size: int = SAMPLE_SIZE,
    days: int = SAMPLE_DAYS,
) -> dict[str, list[news.Article]]:
    """Recent articles per theme, one GDELT request each."""
    first = as_of - timedelta(days=days)
    out: dict[str, list[news.Article]] = {}
    for node in themes:
        articles = client.articles(news.theme_query(node), first, as_of, limit=size)
        out[node.id] = articles[:size]
    return out


def judge_items(themes: dict[str, Node], samples: dict[str, list[news.Article]]) -> list[JudgeItem]:
    items = []
    for theme_id, articles in samples.items():
        node = themes[theme_id]
        topic = _topic(node)
        for i, article in enumerate(articles):
            items.append(
                JudgeItem(
                    id=f"{theme_id.removeprefix('theme/')}:{i}",
                    claim=f"Topic: {topic}",
                    context=f"Headline: {article.title} ({article.domain or 'unknown publisher'})",
                )
            )
    return items


def _topic(node: Node) -> str:
    aliases = ", ".join(node.aliases[:3])
    return f"{node.label}" + (f" (also called: {aliases})" if aliases else "")


def measure(
    themes: dict[str, Node],
    samples: dict[str, list[news.Article]],
    runner: JobRunner,
) -> dict[str, ThemePrecision]:
    """Judge every sampled article and summarize precision per theme."""
    items = judge_items(themes, samples)
    verdicts = judge(items, runner, "query-precision") if items else {}
    return summarize(samples, verdicts)


def summarize(
    samples: dict[str, list[news.Article]], verdicts: dict[str, Verdict]
) -> dict[str, ThemePrecision]:
    out: dict[str, ThemePrecision] = {}
    for theme_id, articles in samples.items():
        rows: list[tuple[str, bool, str]] = []
        for i, article in enumerate(articles):
            verdict = verdicts.get(f"{theme_id.removeprefix('theme/')}:{i}")
            if verdict is None:
                continue
            rows.append((article.url, verdict.correct, verdict.reason))
        out[theme_id] = ThemePrecision(
            theme=theme_id,
            sampled=len(rows),
            relevant=sum(1 for _, correct, _ in rows if correct),
            verdicts=rows,
        )
    return out
