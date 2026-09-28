import dataclasses
import json
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import duckdb
import typer
import yaml
from rich.console import Console
from rich.table import Table

from ripple import attention as attention_module
from ripple import brief as brief_module
from ripple import codex as codex_module
from ripple import edgar, growth, jobs, mapping, news, queries, sections, signal, xbrl, xbrl_edges
from ripple import events as events_module
from ripple import explain as explain_module
from ripple import extract as extract_module
from ripple import judge as judge_module
from ripple import profile as profile_module
from ripple import store as store_module
from ripple import verify as verify_module
from ripple.codex import CodexError
from ripple.graph import Graph, build_graph
from ripple.load import load_seed, load_sources
from ripple.model import DEFAULT_MIN_CONFIDENCE, Node, today_utc
from ripple.paths import Path as ExplainPath
from ripple.paths import top_paths
from ripple.propagate import Direction, Shock
from ripple.score import (
    DEFAULT_MAX_HOPS,
    OBVIOUS_HOPS,
    JitterMode,
    company_exposures,
    score,
    sensitivity,
)
from ripple.store import SeedInvalidError, Store
from ripple.validate import product_path
from ripple.validate import validate as validate_seed

DEFAULT_DB = Path("data/ripple.duckdb")
DEFAULT_SOURCES = Path("data/sources.yaml")
LedgerOption = Annotated[
    Path, typer.Option("--ledger", help="Verification rulings (docs/phase-3.md, M33).")
]
REVENUE_RELATIONS = {"PRODUCES", "SUPPLIES", "SUBSIDIARY_OF"}

app = typer.Typer(
    help="Rank companies by their exposure to a theme shock.",
    no_args_is_help=True,
)
console = Console(highlight=False, soft_wrap=True)

DbOption = Annotated[Path, typer.Option("--db", help="DuckDB store file.")]
AsOfOption = Annotated[
    datetime | None,
    typer.Option("--as-of", formats=["%Y-%m-%d"], help="Date to score as of (default today, UTC)."),
]
MinConfidenceOption = Annotated[
    float, typer.Option("--min-confidence", help="Edges below this confidence do not propagate.")
]
MaxHopsOption = Annotated[int, typer.Option("--max-hops")]


def _as_of(value: datetime | None) -> date:
    return value.date() if value else today_utc()


def _fail(message: str) -> typer.Exit:
    console.print(message, style="red", markup=False)
    return typer.Exit(code=1)


@app.callback()
def main() -> None:
    """Ripple command line."""


@app.command()
def validate(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    source: Annotated[str, typer.Option("--source", help="Source name for DIRECTORY.")] = "seed",
    ledger: LedgerOption = verify_module.DEFAULT_LEDGER,
) -> None:
    """Check seed YAML against the validation rules, with verification rulings applied."""
    rulings = verify_module.load_ledger(ledger)
    seed = verify_module.apply_rulings(load_seed(directory, source), rulings)
    problems = validate_seed(
        seed, today=today_utc(), check_verified=verify_module.started(seed, rulings)
    )
    for problem in problems:
        style = "red" if problem.severity == "error" else "yellow"
        console.print(str(problem), style=style, markup=False)
    n_errors = sum(p.severity == "error" for p in problems)
    n_warnings = len(problems) - n_errors
    evidence = [item for e in seed.edges for item in e.value.evidence]
    n_verified = sum(item.verified for item in evidence)
    console.print(f"{n_verified} of {len(evidence)} evidence items verified")
    console.print(f"{n_errors} errors, {n_warnings} warnings")
    if n_errors:
        raise typer.Exit(code=1)


@app.command()
def load(
    directory: Annotated[
        Path | None, typer.Argument(exists=True, file_okay=False, help="One source directory.")
    ] = None,
    source: Annotated[str, typer.Option("--source", help="Source name for DIRECTORY.")] = "seed",
    sources: Annotated[
        Path | None,
        typer.Option("--sources", exists=True, help="YAML map of source name to directory."),
    ] = None,
    ledger: LedgerOption = verify_module.DEFAULT_LEDGER,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Validate source directories together and record each in the store (D40).

    Without arguments, loads every source listed in data/sources.yaml (or data/seed alone).
    Never overwrites; supersedes, and only within the sources loaded. Verification rulings in
    the ledger are applied first, so a verified item is stored as verified (D113).
    """
    if directory is not None:
        dirs = {source: directory}
    else:
        dirs = _source_dirs(sources or DEFAULT_SOURCES)
    rulings = verify_module.load_ledger(ledger)
    with Store(db) as store:
        before = store.snapshot(today_utc()).sources
        try:
            report = store.load_sources(
                dirs, prepare=lambda seed: verify_module.apply_rulings(seed, rulings)
            )
        except SeedInvalidError as exc:
            for problem in exc.problems:
                console.print(str(problem), style="red", markup=False)
            raise _fail(f"not loaded: {len(exc.problems)} errors") from exc
        after = store.snapshot(today_utc())
    console.print(
        f"{db} ({', '.join(dirs)}): inserted {report.inserted}, superseded {report.superseded}, "
        f"unchanged {report.unchanged}"
    )
    # A verified row outranks an unverified one (D63), so a ruling can change which layer's row
    # wins an edge. Every such change is listed (D113).
    for edge in after.edges:
        old, new = before.get(edge.id), after.sources.get(edge.id)
        if old is not None and old != new:
            console.print(
                f"winner changed: {edge.id} {edge.label} now from {new} (was {old})",
                style="yellow",
                markup=False,
            )


def _source_dirs(path: Path) -> dict[str, Path]:
    if not path.exists():
        return {"seed": Path("data/seed")}
    listed = yaml.safe_load(path.read_text()) or {}
    return {name: Path(directory) for name, directory in listed.items() if Path(directory).exists()}


@app.command()
def exposed(
    theme: str,
    direction: Annotated[str, typer.Option(help="up or down")] = "up",
    top: Annotated[int, typer.Option("--top")] = 20,
    hide_obvious: Annotated[bool, typer.Option("--hide-obvious")] = False,
    obvious_hops: Annotated[
        int,
        typer.Option(
            "--obvious-hops",
            help="Fallback when attention is unknown: hide companies this many hops away or fewer.",
        ),
    ] = OBVIOUS_HOPS,
    obvious_percentile: Annotated[
        float,
        typer.Option(
            "--obvious-percentile", help="Hide companies at or above this attention rank."
        ),
    ] = attention_module.OBVIOUS_PERCENTILE,
    by_novelty: Annotated[
        bool, typer.Option("--by-novelty", help="Sort by novelty instead of exposure.")
    ] = False,
    as_of: AsOfOption = None,
    min_confidence: MinConfidenceOption = DEFAULT_MIN_CONFIDENCE,
    max_hops: MaxHopsOption = DEFAULT_MAX_HOPS,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Rank companies by exposure to a theme shock, with attention and novelty (M27).

    Novelty is exposure discounted by how much coverage the company already gets, so a high
    novelty means high exposure that the news has not paired with the theme. Attention is
    company-wide coverage, not theme-paired, so check a lead before acting on it.
    """
    if direction not in ("up", "down"):
        raise _fail("--direction must be up or down")
    dir_: Direction = "up" if direction == "up" else "down"
    with Store(db) as store:
        try:
            result = score(
                store,
                theme,
                as_of=_as_of(as_of),
                direction=dir_,
                max_hops=max_hops,
                hide_obvious=hide_obvious,
                obvious_hops=obvious_hops,
                obvious_percentile=obvious_percentile,
                limit=None if by_novelty else top,
                min_confidence=min_confidence,
            )
        except ValueError as exc:
            raise _fail(str(exc)) from exc
        labels = {
            node_id: node.label for node_id, node in store.snapshot(result.as_of).nodes.items()
        }

    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
        return

    arrow = "↑" if dir_ == "up" else "↓"
    rows = result.results
    if by_novelty:
        # Companies with no coverage series have unknown novelty and sort last, never first:
        # missing data must not look like a discovery (D91).
        rows = sorted(rows, key=lambda r: (r.novelty is None, -abs(r.novelty or 0.0)))[:top]
    title = f"{theme} {arrow}  (as of {result.as_of}, {result.theme_kind}"
    title += ", by novelty)" if by_novelty else ")"
    table = Table(title=title)
    for column in ("#", "Company", "Ticker", "Exposure", "Novelty", "Attn %ile", "Hops", "Conf"):
        justify = "left" if column in ("Company", "Ticker") else "right"
        table.add_column(column, justify=justify, no_wrap=True)
    table.add_column("Top path (via)", overflow="fold")
    for rank, r in enumerate(rows, start=1):
        top_path = " → ".join(labels.get(n, n) for n in r.paths[0].nodes[1:-1]) if r.paths else ""
        table.add_row(
            str(rank),
            r.label,
            r.ticker or "",
            f"{r.exposure:+.4f}",
            "-" if r.novelty is None else f"{r.novelty:+.4f}",
            "-" if r.attention_percentile is None else f"{r.attention_percentile:.2f}",
            str(r.min_hops or ""),
            f"{r.path_confidence:.2f}",
            top_path,
        )
    console.print(table)
    if all(r.attention is None for r in rows):
        console.print(
            "no attention data: run `ripple signals fetch --all` to fill it in", style="yellow"
        )


@app.command()
def explain(
    theme: str,
    company: str,
    k: Annotated[int, typer.Option("--k", help="Number of paths.")] = 3,
    as_of: AsOfOption = None,
    min_confidence: MinConfidenceOption = DEFAULT_MIN_CONFIDENCE,
    max_hops: MaxHopsOption = DEFAULT_MAX_HOPS,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print what explain_link returns.")
    ] = False,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Show the strongest paths from a theme to a company, with the evidence for each edge."""
    if as_json:
        with Store(db, read_only=True) as store:
            try:
                data = explain_module.explain_link(
                    store, theme, company, _as_of(as_of), k, max_hops, min_confidence
                )
            except ValueError as exc:
                raise _fail(str(exc)) from exc
        print(json.dumps(data, indent=2))
        return
    with Store(db) as store:
        snapshot = store.snapshot(_as_of(as_of))
    graph = build_graph(snapshot, min_confidence=min_confidence)
    for node_id in (theme, company):
        if node_id not in graph.nodes:
            raise _fail(f"unknown node {node_id}")
    paths = top_paths(graph, theme, company, k=k, max_hops=max_hops)
    if not paths:
        raise _fail(f"no path from {theme} to {company} within {max_hops} hops")
    for number, path in enumerate(paths, start=1):
        console.print(
            f"{number}. {path.contribution:+.4f}  {format_path(graph, path)}", markup=False
        )
        for e in path.edge_records:
            console.print(
                f"     {e.id}  {e.label}  weight {e.weight} ({e.weight_source}), "
                f"confidence {e.confidence}, source {snapshot.sources.get(e.id, 'seed')}",
                markup=False,
            )
            for item in e.evidence:
                console.print(f"         {item.accessed}  {item.url}  {item.note}", markup=False)
            for alt_source, alt in snapshot.alternatives.get(e.id, []):
                console.print(
                    f"         also from {alt_source}: weight {alt.weight} ({alt.weight_source}), "
                    f"confidence {alt.confidence}",
                    markup=False,
                )


@app.command()
def evidence(
    edge_id: str,
    as_of: AsOfOption = None,
    min_confidence: MinConfidenceOption = DEFAULT_MIN_CONFIDENCE,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print what get_evidence returns.")
    ] = False,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Show one edge with its evidence, the source layer that won and the rows that lost."""
    with Store(db, read_only=True) as store:
        try:
            data = explain_module.get_evidence(store, edge_id, _as_of(as_of), min_confidence)
        except ValueError as exc:
            raise _fail(str(exc)) from exc
    if as_json:
        print(json.dumps(data, indent=2))
        return
    carries = "propagates" if data["propagates"] else "does not propagate"
    console.print(
        f"{data['edge']}  {data['src_label']} {data['rel']} {data['dst_label']}\n"
        f"  weight {data['weight']} ({data['weight_source']}), polarity {data['polarity']}, "
        f"confidence {data['confidence']}, {carries}, source {data['source']}, "
        f"valid from {data['valid_from']}",
        markup=False,
    )
    for item in data["evidence"]:
        mark = "verified" if item["verified"] else "unverified"
        console.print(f"  [{mark}] {item['accessed']}  {item['url']}", markup=False)
        console.print(f"      {item['note']}", markup=False)
    for alt in data["alternatives"]:
        console.print(
            f"  lost: {alt['source']} weight {alt['weight']} ({alt['weight_source']}), "
            f"confidence {alt['confidence']}",
            markup=False,
        )


@app.command("profile")
def profile_command(
    company: str,
    as_of: AsOfOption = None,
    min_confidence: MinConfidenceOption = DEFAULT_MIN_CONFIDENCE,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print what company_profile returns.")
    ] = False,
    db: DbOption = DEFAULT_DB,
) -> None:
    """One company: revenue mix, customers, suppliers, regions and exposure per theme (M32)."""
    with Store(db, read_only=True) as store:
        try:
            data = profile_module.company_profile(store, company, _as_of(as_of), min_confidence)
        except ValueError as exc:
            raise _fail(str(exc)) from exc
    if as_json:
        print(json.dumps(data, indent=2))
        return
    head = data["company"]
    console.print(
        f"{head['label']} ({head['ticker'] or 'no ticker'})  as of {data['as_of']}", markup=False
    )
    mix = Table("Product", "Share", "Source", "Conf", title="Revenue mix")
    for r in data["revenue_mix"]:
        conf = f"{r['confidence']}" + ("" if r["propagates"] else " (held)")
        mix.add_row(r["label"], f"{r['weight']:.3f}", r["weight_source"], conf)
    console.print(mix)
    console.print(f"mapped share of revenue: {data['mapped_share']:.3f}")
    for field, title in (
        ("customers", "Customers"),
        ("suppliers", "Suppliers"),
        ("parents", "Parent"),
        ("subsidiaries", "Subsidiaries"),
    ):
        if data[field]:
            names = ", ".join(f"{r['label']} {r['weight']:.2f}" for r in data[field])
            console.print(f"{title}: {names}", markup=False)
    if data["regions"]:
        regions = ", ".join(f"{r['label']} {r['weight']:.2f}" for r in data["regions"][:8])
        console.print(f"Regions: {regions}", markup=False)
    themes = Table("Theme", "Kind", "Exposure", "Rank", "Top path", title="Exposure per theme")
    for t in data["themes"]:
        path = t["top_path"]
        via = " → ".join(n["label"] for n in path["nodes"][1:-1]) if path else ""
        themes.add_row(
            t["label"], t["kind"] or "", f"{t['exposure']:+.4f}", f"{t['rank']}/{t['of']}", via
        )
    console.print(themes)
    console.print("Exposures are per theme; never add them across themes (D9).")
    attention = data["attention"]
    console.print(
        "attention: unknown (no coverage series)"
        if attention is None
        else f"attention: {attention['share']:.2e} of coverage over {attention['days']} days"
    )


def format_path(graph: Graph, path: ExplainPath, arrow: str = "↑") -> str:
    """Theme ↑ → node (weight of the edge into it) → ... → company (rev share)."""
    parts = [f"{graph.nodes[path.nodes[0]].label} {arrow}"]
    for node_id, e in zip(path.nodes[1:], path.edge_records, strict=True):
        signed = e.weight * e.polarity
        share = f"rev {signed}" if e.rel in REVENUE_RELATIONS else f"{signed}"
        parts.append(f"{graph.nodes[node_id].label} ({share})")
    return " → ".join(parts)


@app.command("sensitivity")
def sensitivity_command(
    theme: str,
    trials: Annotated[int, typer.Option("--trials")] = 200,
    mode: Annotated[str, typer.Option("--mode", help="bucket (default) or flat")] = "bucket",
    jitter: Annotated[float, typer.Option("--jitter", help="Flat mode: factor range 1 ± j.")] = 0.5,
    top_n: Annotated[int, typer.Option("--top-n")] = 10,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    as_of: AsOfOption = None,
    min_confidence: MinConfidenceOption = DEFAULT_MIN_CONFIDENCE,
    max_hops: MaxHopsOption = DEFAULT_MAX_HOPS,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Jitter bucket-sourced weights and report how stable the ranking is (D17)."""
    with Store(db) as store:
        graph = build_graph(store.snapshot(_as_of(as_of)), min_confidence=min_confidence)
    if mode not in ("bucket", "flat"):
        raise _fail("--mode must be bucket or flat")
    jitter_mode: JitterMode = "bucket" if mode == "bucket" else "flat"
    try:
        result = sensitivity(graph, theme, trials, jitter, top_n, max_hops, seed, jitter_mode)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    how = (
        "bucket weights redrawn within their bucket ranges"
        if jitter_mode == "bucket"
        else f"bucket weights x U[{1 - jitter:.2f}, {1 + jitter:.2f}]"
    )
    console.print(f"{theme}: {trials} trials, {how}")
    console.print(f"median top-{result.top_n} Spearman: {result.median_spearman_top:.3f}")
    console.print(
        f"median top-{result.top_n} set overlap: {result.median_top_overlap:g}/{result.top_n}"
    )
    console.print(f"median Kendall tau (all companies): {result.median_kendall_all:.3f}")
    console.print("least stable (mean absolute rank change):")
    for company, shift in result.least_stable:
        label = graph.nodes[company].label
        console.print(f"  {shift:5.2f}  {label} ({company})", markup=False)


# Phase 1: EDGAR


CacheOption = Annotated[Path, typer.Option("--cache", help="Filing cache directory.")]
UniverseOption = Annotated[Path, typer.Option("--universe", help="Universe file.")]


def _edgar_client(cache: Path) -> edgar.EdgarClient:
    try:
        return edgar.make_client(cache)
    except edgar.EdgarConfigError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def universe(
    seed: Annotated[Path, typer.Option("--seed", exists=True, file_okay=False)] = Path("data/seed"),
    out: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
) -> None:
    """Resolve seed companies to SEC filers and write the universe file."""
    client = _edgar_client(cache)
    entries = edgar.build_universe((n.value for n in load_seed(seed).nodes), client)
    if out.exists():  # keep companies added by hand or by later milestones
        known = {e.node for e in entries}
        entries += [e for e in edgar.load_universe(out) if e.node not in known]
    edgar.save_universe(out, entries)
    n_sec = sum(e.sec for e in entries)
    console.print(f"{out}: {len(entries)} companies, {n_sec} SEC filers")


@app.command()
def fetch(
    nodes: Annotated[list[str] | None, typer.Argument(help="Company IDs; default all.")] = None,
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
) -> None:
    """Download the latest annual filing of each SEC filer into the cache."""
    client = _edgar_client(cache)
    entries = edgar.load_universe(universe_path)
    if nodes:
        unknown = set(nodes) - {e.node for e in entries}
        if unknown:
            raise _fail(f"not in {universe_path}: {', '.join(sorted(unknown))}")
        entries = [e for e in entries if e.node in nodes]
    skipped = 0
    for entry in entries:
        if not entry.sec or entry.cik is None:
            skipped += 1
            continue
        try:
            filing = client.latest_annual_filing(entry.cik)
            if filing is None:
                console.print(f"{entry.node}  no annual filing found", style="yellow")
                continue
            cached = client.fetch_filing(filing)
        except edgar.EdgarError as exc:
            console.print(f"{entry.node}  {exc}", style="red", markup=False)
            continue
        console.print(
            f"{entry.node}  {filing.form}  period {filing.report_date}  filed "
            f"{filing.filing_date}  {', '.join(sorted(cached.files))}",
            markup=False,
        )
    if skipped:
        console.print(f"skipped {skipped} companies that do not file with the SEC")


@app.command()
def revenue(
    nodes: Annotated[
        list[str] | None, typer.Argument(help="Company IDs to show in detail.")
    ] = None,
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
) -> None:
    """Revenue splits from cached XBRL: a coverage table, or detail for the given companies."""
    entries = [e for e in edgar.load_universe(universe_path) if e.sec and e.cik is not None]
    if nodes:
        entries = [e for e in entries if e.node in nodes]
    results = {}
    for entry in entries:
        cached = edgar.latest_cached(cache, entry.cik)  # type: ignore[arg-type]
        results[entry.node] = xbrl.load_revenue(cached) if cached else None

    if nodes:
        for node, facts in results.items():
            _print_revenue_detail(node, facts)
        return

    table = Table(title="XBRL revenue splits (lines per axis)")
    for column in ("Company", "FY end", "Rev bn", "Prod", "Seg", "Geo", "Cust"):
        table.add_column(column, no_wrap=True)
    covered = 0
    for node, facts in results.items():
        if facts is None:
            table.add_row(node, "-", "no XBRL revenue", "", "", "", "")
            continue
        covered += any(k in facts.breakdowns for k in ("product", "segment"))
        table.add_row(
            node,
            str(facts.end),
            f"{facts.total / 1e9:,.1f} {facts.currency}",
            *(_breakdown_cell(facts, kind) for kind in ("product", "segment", "geography")),
            str(len(facts.customers)) if facts.customers else "",
        )
    console.print(table)
    console.print("* partial: the lines do not add up to total revenue")
    console.print(f"product or segment split: {covered} of {len(results)} SEC filers")


def _breakdown_cell(facts: xbrl.RevenueFacts, kind: str) -> str:
    breakdown = facts.breakdowns.get(kind)
    if breakdown is None:
        return ""
    return f"{len(breakdown.lines)}" + ("" if breakdown.complete else "*")


def _print_revenue_detail(node: str, facts: xbrl.RevenueFacts | None) -> None:
    if facts is None:
        console.print(f"{node}: no XBRL revenue in the cache", style="yellow")
        return
    console.print(
        f"{node}: {facts.concept} {facts.start} to {facts.end}, "
        f"total {facts.total / 1e9:,.2f} bn {facts.currency}",
        markup=False,
    )
    for kind, breakdown in facts.breakdowns.items():
        state = "complete" if breakdown.complete else "partial"
        console.print(f"  {kind} ({breakdown.axis}, {state})", markup=False)
        for line in breakdown.lines:
            console.print(f"    {line.share:6.1%}  {line.label}  [{line.member}]", markup=False)
        if breakdown.dropped:
            console.print(f"    dropped aggregates: {', '.join(breakdown.dropped)}", markup=False)
    if facts.customers:
        console.print("  customers", markup=False)
        for line in facts.customers:
            console.print(f"    {line.share:6.1%}  {line.label}  [{line.member}]", markup=False)


# Phase 1: revenue-line mapping and the review queue (M13)


QueueOption = Annotated[Path, typer.Option("--queue", help="Review queue directory.")]


@app.command("map")
def map_command(
    nodes: Annotated[list[str] | None, typer.Argument(help="Company IDs; default all.")] = None,
    seed: Annotated[Path, typer.Option("--seed", exists=True, file_okay=False)] = Path("data/seed"),
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    queue: QueueOption = mapping.DEFAULT_QUEUE,
    effort: Annotated[str, typer.Option("--effort")] = "medium",
    force: Annotated[bool, typer.Option("--force", help="Redo mapped companies.")] = False,
) -> None:
    """Ask Codex to map each company's XBRL revenue lines onto the product taxonomy."""
    seed_nodes = {n.value.id: n.value for n in load_seed(seed).nodes}
    taxonomy = sorted(
        (n for n in seed_nodes.values() if n.type in ("product", "material")), key=lambda n: n.id
    )
    entries = [e for e in edgar.load_universe(universe_path) if e.sec and e.cik is not None]
    if nodes:
        entries = [e for e in entries if e.node in nodes]
    runner = None
    for entry in entries:
        path = mapping.queue_path(queue, entry.node)
        if path.exists() and not force:
            console.print(f"{entry.node}  already in the queue ({path})", markup=False)
            continue
        cached = edgar.latest_cached(cache, entry.cik)  # type: ignore[arg-type]
        facts = xbrl.load_revenue(cached) if cached else None
        if cached is None or facts is None or mapping.choose_breakdown(facts) is None:
            console.print(f"{entry.node}  no revenue breakdown to map", style="yellow")
            continue
        runner = runner or mapping.make_runner(effort)
        label = seed_nodes[entry.node].label if entry.node in seed_nodes else entry.node
        try:
            result = mapping.map_company(
                entry.node, label, facts, taxonomy, runner, cached.filing.url, cached.filing.form
            )
        except CodexError as exc:
            console.print(f"{entry.node}  {exc}", style="red", markup=False)
            continue
        mapping.save_mapping(path, result)
        assigned = sum(bool(line.assignments) for line in result.lines)
        console.print(
            f"{entry.node}  {result.kind}: {assigned} of {len(result.lines)} lines mapped, "
            f"{result.usage.get('input_tokens', 0):,} input tokens",
            markup=False,
        )


review_app = typer.Typer(help="Review mapping proposals (M13).", no_args_is_help=True)
app.add_typer(review_app, name="review")


@review_app.command("list")
def review_list(queue: QueueOption = mapping.DEFAULT_QUEUE) -> None:
    """List mappings in the queue with their status."""
    for path in sorted(queue.glob("*.yaml")):
        m = mapping.load_mapping(path)
        proposals = sum(line.new_product is not None for line in m.lines)
        extra = f", {proposals} new-product proposals" if proposals else ""
        console.print(
            f"{m.status:9} {m.node}  ({m.kind}, {len(m.lines)} lines{extra})", markup=False
        )


@review_app.command("show")
def review_show(node: str, queue: QueueOption = mapping.DEFAULT_QUEUE) -> None:
    """Show one mapping: each revenue line, its products, and the resulting edge weights."""
    path = mapping.queue_path(queue, node)
    if not path.exists():
        raise _fail(f"{node} is not in the queue")
    m = mapping.load_mapping(path)
    console.print(
        f"{m.node} ({m.status}) {m.form} FY{m.fiscal_end.year}, {m.kind} lines "
        f"({'complete' if m.complete else 'partial'}), total {m.total / 1e9:,.1f} bn {m.currency}",
        markup=False,
    )
    for line in m.lines:
        products = ", ".join(f"{a.product} x{a.fraction:.2f}" for a in line.assignments) or "-"
        console.print(f"  {line.share:6.1%}  {line.label}  ->  {products}", markup=False)
        if line.new_product:
            console.print(f"          proposes new product: {line.new_product}", markup=False)
        if line.note:
            console.print(f"          {line.note}", markup=False)
    console.print("edges on accept:", markup=False)
    for e in mapping.edges_from_mapping(m, accessed=today_utc()):
        console.print(
            f"  {e['dst']}  weight {e['weight']} ({e['weight_source']}, confidence "
            f"{e['confidence']})",
            markup=False,
        )


@review_app.command("accept")
def review_accept(
    node: str,
    queue: QueueOption = mapping.DEFAULT_QUEUE,
    out: Annotated[Path, typer.Option("--out", help="xbrl source directory.")] = (
        mapping.DEFAULT_XBRL
    ),
    reviewer: Annotated[str, typer.Option("--reviewer")] = "user",
) -> None:
    """Accept a mapping: write its PRODUCES edges into the xbrl source."""
    path = mapping.queue_path(queue, node)
    if not path.exists():
        raise _fail(f"{node} is not in the queue")
    written = mapping.accept_mapping(path, out, reviewer=reviewer, accessed=today_utc())
    console.print(f"{node} accepted by {reviewer}: {written}", markup=False)


@review_app.command("reject")
def review_reject(
    node: str,
    reason: Annotated[str, typer.Option("--reason")],
    queue: QueueOption = mapping.DEFAULT_QUEUE,
    reviewer: Annotated[str, typer.Option("--reviewer")] = "user",
) -> None:
    """Reject a mapping (no edges are written)."""
    path = mapping.queue_path(queue, node)
    if not path.exists():
        raise _fail(f"{node} is not in the queue")
    mapping.reject_mapping(path, reviewer, reason)
    console.print(f"{node} rejected by {reviewer}", markup=False)


@app.command("xbrl-edges")
def xbrl_edges_command(
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    sources: Annotated[Path, typer.Option("--sources", help="Graph used for the W9 check.")] = (
        DEFAULT_SOURCES
    ),
    out: Annotated[Path, typer.Option("--out", help="xbrl source directory.")] = (
        mapping.DEFAULT_XBRL
    ),
) -> None:
    """Write region (EXPOSED_TO) and named-customer (SUPPLIES) edges from cached XBRL (M14)."""
    graph = load_sources(_source_dirs(sources))
    graph_edges = [e.value for e in graph.edges]
    companies = [n.value for n in graph.nodes if n.value.type == "company"]
    regions: dict[str, dict] = {}
    for entry in edgar.load_universe(universe_path):
        if not entry.sec or entry.cik is None:
            continue
        cached = edgar.latest_cached(cache, entry.cik)
        facts = xbrl.load_revenue(cached) if cached else None
        if cached is None or facts is None:
            continue
        slug = entry.node.split("/", 1)[-1]
        region_nodes, region_list = xbrl_edges.region_edges(
            entry.node, facts, cached.filing, accessed=today_utc()
        )
        customer_list, skipped = xbrl_edges.customer_edges(
            entry.node,
            facts,
            cached.filing,
            companies,
            has_product_path=lambda c, s: product_path(graph_edges, customer=c, supplier=s),
            accessed=today_utc(),
        )
        for record in region_nodes:
            regions.setdefault(record["id"], record)
        _write_generated(out / "edges" / f"{slug}-regions.yaml", region_list, entry.node)
        _write_generated(out / "edges" / f"{slug}-customers.yaml", customer_list, entry.node)
        skipped_text = ", ".join(f"{label} ({why})" for label, why in skipped.items())
        console.print(
            f"{entry.node}: {len(region_list)} regions, {len(customer_list)} customers"
            + (f"; skipped {skipped_text}" if skipped_text else ""),
            markup=False,
        )
    _write_generated(
        out / "nodes" / "regions.yaml", sorted(regions.values(), key=lambda r: r["id"]), "XBRL"
    )


def _write_generated(path: Path, records: list[dict], origin: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"# Generated from {origin} by `ripple xbrl-edges` (M14). Do not edit.\n"
    path.write_text(header + (yaml.safe_dump(records, sort_keys=False) if records else "[]\n"))


@app.command()
def usage(
    jobs_db: Annotated[Path, typer.Option("--jobs", help="Jobs database.")] = jobs.DEFAULT_JOBS,
) -> None:
    """Token usage of finished Codex jobs, by kind (M16)."""
    if not jobs_db.exists():
        raise _fail(f"no jobs database at {jobs_db}")
    import duckdb

    con = duckdb.connect(str(jobs_db), read_only=True)
    rows = con.execute(
        "SELECT kind, status, count(*), "
        "sum(coalesce(CAST(json_extract(usage, '$.input_tokens') AS BIGINT), 0)), "
        "sum(coalesce(CAST(json_extract(usage, '$.output_tokens') AS BIGINT), 0)) "
        "FROM jobs GROUP BY kind, status ORDER BY kind, status"
    ).fetchall()
    versions = [r[0] for r in con.execute("SELECT DISTINCT codex_version FROM jobs").fetchall()]
    con.close()
    for kind, status, count, tokens_in, tokens_out in rows:
        console.print(
            f"{kind:12} {status:8} {count:5} jobs  {tokens_in:>12,} in  {tokens_out:>10,} out",
            markup=False,
        )
    console.print(f"codex versions: {', '.join(v for v in versions if v)}", markup=False)


@app.command("sections")
def sections_command(
    nodes: Annotated[list[str] | None, typer.Argument(help="Company IDs; default all.")] = None,
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
) -> None:
    """Cut cached filings into sections (M17) and report what was found."""
    entries = [e for e in edgar.load_universe(universe_path) if e.sec and e.cik is not None]
    if nodes:
        entries = [e for e in entries if e.node in nodes]
    found_items = 0
    for entry in entries:
        cached = edgar.latest_cached(cache, entry.cik)  # type: ignore[arg-type]
        if cached is None:
            console.print(f"{entry.node}  not cached", style="yellow")
            continue
        text, parts = sections.load_sections(cached)
        items = [s for s in parts if not s.fallback]
        found_items += bool(items)
        listing = ", ".join(f"{s.name} {(s.end - s.start) // 4:,}t" for s in parts)
        mode = "items" if items else "FALLBACK chunks"
        console.print(f"{entry.node}  {cached.filing.form}  {mode}: {listing}", markup=False)
    console.print(f"items found: {found_items} of {len(entries)} filers")


# Phase 1b: universe growth (M18)


@app.command()
def grow(
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    seed: Annotated[Path, typer.Option("--seed", exists=True, file_okay=False)] = Path("data/seed"),
    out: Annotated[Path, typer.Option("--out", help="llm source directory.")] = Path("data/llm"),
    report: Annotated[Path, typer.Option("--report")] = Path("data/review-queue/growth.yaml"),
    jobs_db: Annotated[Path, typer.Option("--jobs")] = jobs.DEFAULT_JOBS,
    target: Annotated[int, typer.Option("--target", help="Universe size to aim for.")] = 150,
    todo: Annotated[Path, typer.Option("--todo")] = Path("data/seed/TODO.md"),
    review: Annotated[Path, typer.Option("--review", help="Additions rejected in review.")] = Path(
        "data/review-queue/growth-review.yaml"
    ),
) -> None:
    """Grow the universe by one hop from the seed filers' Items 1 and 1A (M18, D71)."""
    seed_nodes = {n.value.id: n.value for n in load_seed(seed).nodes}
    taxonomy = sorted(
        (n for n in seed_nodes.values() if n.type in ("product", "material")), key=lambda n: n.id
    )
    # Each run regenerates the additions: entries added by an earlier run are dropped first.
    nodes_path = out / "nodes" / "companies.yaml"
    grown = (
        {n["id"] for n in yaml.safe_load(nodes_path.read_text()) or []}
        if nodes_path.exists()
        else set()
    )
    entries = [e for e in edgar.load_universe(universe_path) if e.node not in grown]
    seed_filers = [e for e in entries if e.sec and e.cik is not None and e.node in seed_nodes]

    work = []
    for entry in seed_filers:
        cached = edgar.latest_cached(cache, entry.cik)  # type: ignore[arg-type]
        if cached is None:
            continue
        text, parts = sections.load_sections(cached)
        for part in parts:
            if part.fallback or part.name in growth.GROWTH_SECTIONS:
                body = sections.section_text(text, part)
                prompt = growth.growth_prompt(
                    seed_nodes[entry.node].label,
                    entry.node,
                    cached.filing.form,
                    part.name,
                    body,
                    taxonomy,
                )
                job = jobs.Job(
                    f"{entry.node}/{part.name}",
                    prompt,
                    growth.growth_schema(taxonomy),
                    growth.INSTRUCTIONS,
                )
                work.append((entry.node, part, body, job))
    runner = growth.make_job_runner(jobs_db)
    try:
        results = runner.run_batch("growth", [job for *_, job in work])
    except jobs.BatchStopped as exc:
        raise _fail(f"{exc}. Run `ripple grow` again later to resume.") from exc
    answers = [
        (node, part.name, part.start, body, results[job.key].data)
        for node, part, body, job in work
        if job.key in results
    ]

    tickers_path = cache / "company_tickers.json"
    if not tickers_path.exists():
        _edgar_client(cache).tickers()
    rows = json.loads(tickers_path.read_text()).values()
    resolver = growth.NameResolver((int(r["cik_str"]), r["ticker"], r["title"]) for r in rows)
    candidates, dropped = growth.collect_candidates(
        answers,
        resolver,
        {n.id for n in taxonomy},
        {e.node: e.cik for e in seed_filers if e.cik is not None},
    )

    client: list[edgar.EdgarClient] = []

    def client_factory() -> edgar.EdgarClient:
        if not client:
            client.append(_edgar_client(cache))
        return client[0]

    forms: dict[int, str | None] = {}

    def form_of(cik: int) -> str | None:
        if cik not in forms:
            forms[cik] = growth.annual_form(client_factory, cik, date.today())
        return forms[cik]

    decisions = growth.select_additions(
        candidates,
        known_ciks={e.cik for e in entries if e.cik is not None},
        cap=max(target - len(entries), 0),
        annual_form=form_of,
        rejected=_growth_rejections(review),
    )

    new_nodes: list[dict] = []
    taken = set(seed_nodes) | {e.node for e in entries}
    for candidate in growth.rank_candidates(candidates):
        if decisions[candidate.name] != "added" or candidate.cik is None:
            continue
        node_id = growth.node_id_for(candidate, taken)
        taken.add(node_id)
        new_nodes.append(
            {
                "id": node_id,
                "type": "company",
                "label": candidate.name,
                "aliases": [candidate.title] if candidate.title else [],
                "ticker": candidate.ticker,
                "ids": {"cik": str(candidate.cik)},
            }
        )
        entries.append(
            edgar.UniverseEntry(
                node_id, candidate.ticker, candidate.cik, forms.get(candidate.cik), True
            )
        )
    _write_generated(nodes_path, new_nodes, "universe growth (M18)")
    edgar.save_universe(universe_path, entries)
    _write_growth_report(report, candidates, decisions, target)
    _write_non_sec(todo, [c for c in candidates if decisions[c.name] == "not an SEC filer"])

    added = sum(d == "added" for d in decisions.values())
    plural = "" if dropped == 1 else "s"
    console.print(
        f"{len(work)} sections read; {len(candidates)} candidates; {added} added; "
        f"dropped {dropped} mention{plural} whose quote was not found",
        markup=False,
    )


def _growth_rejections(path: Path) -> dict[int, str]:
    """CIK -> reason, from the review file (`rejected: [{cik, name, reason}]`)."""
    if not path.exists():
        return {}
    rows = (yaml.safe_load(path.read_text()) or {}).get("rejected", [])
    return {int(row["cik"]): row["reason"] for row in rows}


def _write_growth_report(
    path: Path, candidates: list, decisions: dict[str, str], target: int
) -> None:
    rows = [
        {
            "name": c.name,
            "ticker": c.ticker,
            "cik": c.cik,
            "title": c.title,
            "products": sorted(c.products),
            "decision": decisions[c.name],
            "named_by": sorted(c.filers),
            "mentions": [dataclasses.asdict(m) for m in c.mentions],
        }
        for c in growth.rank_candidates(candidates)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Universe growth candidates (M18). Mentions are offsets into cached filing text.\n"
    path.write_text(
        header + yaml.safe_dump({"target": target, "candidates": rows}, sort_keys=False)
    )


NON_SEC_START = "<!-- growth:non-sec:start -->"
NON_SEC_END = "<!-- growth:non-sec:end -->"


def _write_non_sec(path: Path, candidates: list) -> None:
    lines = [NON_SEC_START, "", "## Named in filings but not SEC filers (M18, for later)", ""]
    for c in growth.rank_candidates(candidates):
        roles = sorted({m.role for m in c.mentions})
        lines.append(f"- {c.name}: named by {', '.join(sorted(c.filers))} ({', '.join(roles)})")
    lines += ["", NON_SEC_END]
    block = "\n".join(lines)
    text = path.read_text() if path.exists() else ""
    if NON_SEC_START in text and NON_SEC_END in text:
        before, rest = text.split(NON_SEC_START, 1)
        text = before + block + rest.split(NON_SEC_END, 1)[1]
    else:
        text = text.rstrip() + ("\n\n" if text else "") + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# Phase 1b: the judge (M18, M20)


def _filing_text(cache: Path, cik: int | None) -> str:
    if cik is None:
        return ""
    cached = edgar.latest_cached(cache, cik)
    return sections.load_sections(cached)[0] if cached else ""


def _business_text(cache: Path, cik: int | None, chars: int = 3000) -> str:
    """The start of Item 1 (or the first fallback chunk): context for judging mappings."""
    if cik is None:
        return ""
    cached = edgar.latest_cached(cache, cik)
    if cached is None:
        return ""
    text, parts = sections.load_sections(cached)
    first = next((s for s in parts if s.name in ("1", "4")), parts[0] if parts else None)
    return sections.section_text(text, first)[:chars] if first else ""


@app.command("judge-growth")
def judge_growth(
    report: Annotated[Path, typer.Option("--report", exists=True)] = Path(
        "data/review-queue/growth.yaml"
    ),
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    seed: Annotated[Path, typer.Option("--seed", exists=True, file_okay=False)] = Path("data/seed"),
    jobs_db: Annotated[Path, typer.Option("--jobs")] = jobs.DEFAULT_JOBS,
    out: Annotated[Path, typer.Option("--out")] = Path("data/review-queue/judge-growth.yaml"),
    sample_size: Annotated[int, typer.Option("--sample")] = 100,
) -> None:
    """Judge a sample of universe additions against the filing text that named them (M18)."""
    data = yaml.safe_load(report.read_text())
    seed_nodes = {n.value.id: n.value for n in load_seed(seed).nodes}
    labels = {node_id: node.label for node_id, node in seed_nodes.items()}
    ciks = {e.node: e.cik for e in edgar.load_universe(universe_path)}
    texts: dict[str, str] = {}
    items = []
    for c in data["candidates"]:
        if c["decision"] != "added" or not c["mentions"]:
            continue
        m = c["mentions"][0]
        if m["filer"] not in texts:
            texts[m["filer"]] = _filing_text(cache, ciks.get(m["filer"]))
        products = ", ".join(labels.get(p, p) for p in c["products"]) or "none"
        claim = (
            f"{labels.get(m['filer'], m['filer'])} names {c['name']} as a {m['role']}. "
            f"{c['name']} makes at least one of: {products}."
        )
        mention = judge_module.context_around(texts[m["filer"]], m["start"], m["end"])
        own_text = _filing_text(cache, c["cik"])
        found = {
            p: judge_module.product_passage(own_text, seed_nodes[p])
            for p in c["products"]
            if p in seed_nodes
        }
        passages = [f"[{labels[p]}] {passage}" for p, passage in found.items() if passage]
        own = _business_text(cache, c["cik"], chars=1500) or "(not cached)"
        context = (
            f"From {labels.get(m['filer'], m['filer'])}'s filing: {mention}\n\n"
            f"From {c['name']}'s own business description: {own}\n\n"
            f"Where {c['name']}'s own filing names the products:\n" + "\n".join(passages)
        )
        items.append(judge_module.JudgeItem(c["name"], claim, context))
    chosen = judge_module.sample(items, sample_size, seed=0)
    verdicts = judge_module.judge(chosen, jobs.make_codex_job_runner(jobs_db), "growth")
    result = judge_module.precision(verdicts)
    _write_verdicts(out, verdicts, result)
    console.print(
        f"additions judged: {result.correct} of {result.total} correct ({result.rate:.0%}), "
        f"entities correct {result.entities_correct} of {result.total}",
        markup=False,
    )
    for item_id, verdict in sorted(verdicts.items()):
        if not verdict.correct:
            console.print(f"  incorrect: {item_id}: {verdict.reason}", markup=False)


@app.command("judge-mappings")
def judge_mappings(
    queue: QueueOption = mapping.DEFAULT_QUEUE,
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    jobs_db: Annotated[Path, typer.Option("--jobs")] = jobs.DEFAULT_JOBS,
    out: Annotated[Path, typer.Option("--out", help="xbrl source directory.")] = (
        mapping.DEFAULT_XBRL
    ),
    accept: Annotated[bool, typer.Option("--accept", help="Accept mappings with no flags.")] = (
        False
    ),
    reviewer: Annotated[str, typer.Option("--reviewer")] = "judge",
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    """Judge pending mappings line by line; optionally accept the ones with no flags (D67)."""
    ciks = {e.node: e.cik for e in edgar.load_universe(universe_path)}
    pending = []
    for path in sorted(queue.glob("*.yaml")):
        m = mapping.load_mapping(path)
        if m.status == "pending":
            pending.append((path, m))
    items = []
    for _, m in pending:
        business = _business_text(cache, ciks.get(m.node))
        for line in m.lines:
            if not line.assignments:
                continue
            parts = "; ".join(f"{a.fraction:.0%} {a.product}" for a in line.assignments)
            claim = (
                f"{m.label}: revenue line '{line.label}' ({line.share:.1%} of revenue) is {parts}; "
                "the rest of the line is outside these products."
            )
            items.append(judge_module.JudgeItem(f"{m.node}#{line.member}", claim, business))
    verdicts = judge_module.judge(items, jobs.make_codex_job_runner(jobs_db), "mapping")
    _write_verdicts(
        report or queue.parent / "judge-mappings.yaml", verdicts, judge_module.precision(verdicts)
    )
    for path, m in pending:
        ids = [f"{m.node}#{line.member}" for line in m.lines if line.assignments]
        flags = [i for i in ids if i not in verdicts or not verdicts[i].correct]
        if flags:
            reasons = "; ".join(
                verdicts[i].reason if i in verdicts else "not judged" for i in flags
            )
            console.print(f"{m.node}  flagged: {reasons}", markup=False)
        elif accept:
            mapping.accept_mapping(path, out, reviewer=reviewer, accessed=today_utc())
            console.print(f"{m.node}  accepted ({reviewer})", markup=False)
        else:
            console.print(f"{m.node}  ok", markup=False)


def _write_verdicts(path: Path, verdicts: dict, result: judge_module.Precision) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "judged": result.total,
        "correct": result.correct,
        "rate": round(result.rate, 3),
        "entities_correct": result.entities_correct,
        "verdicts": {k: dataclasses.asdict(v) for k, v in sorted(verdicts.items())},
    }
    header = "# LLM judge verdicts (docs/phase-1.md, D46, D73). No filing text is stored.\n"
    path.write_text(header + yaml.safe_dump(body, sort_keys=False))


# Phase 1b: text extraction (M19)


EXTRACT_SECTIONS = {"1", "1A", "7", "3D", "4", "5"}


@app.command()
def extract(
    nodes: Annotated[list[str] | None, typer.Argument(help="Company IDs; default all.")] = None,
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    seed: Annotated[Path, typer.Option("--seed", exists=True, file_okay=False)] = Path("data/seed"),
    sources_path: Annotated[Path, typer.Option("--sources")] = DEFAULT_SOURCES,
    queue: Annotated[Path, typer.Option("--queue")] = Path("data/review-queue/extract"),
    jobs_db: Annotated[Path, typer.Option("--jobs")] = jobs.DEFAULT_JOBS,
    dev: Annotated[int | None, typer.Option("--dev", help="Only a dev set of N filers.")] = None,
    effort: Annotated[str, typer.Option("--effort")] = "medium",
) -> None:
    """Extract REQUIRES proposals, disclosed numbers and evidence from filing text (M19)."""
    taxonomy = sorted(
        (n.value for n in load_seed(seed).nodes if n.value.type in ("product", "material")),
        key=lambda n: n.id,
    )
    graph_nodes = {n.value.id: n.value for n in load_sources(_source_dirs(sources_path)).nodes}
    companies = [n for n in graph_nodes.values() if n.type == "company"]
    all_filers = [e for e in edgar.load_universe(universe_path) if e.sec and e.cik is not None]
    entries = [e for e in all_filers if not nodes or e.node in nodes]
    if dev is not None:
        entries = _dev_set(entries, cache, dev)

    work = []
    for entry in entries:
        cached = edgar.latest_cached(cache, entry.cik)  # type: ignore[arg-type]
        if cached is None:
            console.print(f"{entry.node}  not cached", style="yellow", markup=False)
            continue
        text, parts = sections.load_sections(cached)
        label = graph_nodes[entry.node].label if entry.node in graph_nodes else entry.node
        for part in parts:
            if part.fallback or part.name in EXTRACT_SECTIONS:
                body = sections.section_text(text, part)
                prompt = extract_module.extraction_prompt(
                    label, entry.node, cached.filing.form, part.name, body, taxonomy
                )
                job = jobs.Job(
                    f"{entry.node}/{part.name}",
                    prompt,
                    extract_module.extraction_schema(taxonomy),
                    extract_module.INSTRUCTIONS,
                )
                work.append((entry.node, cached.filing.form, part, body, job))
    runner = jobs.make_codex_job_runner(jobs_db, effort=effort)
    try:
        results = runner.run_batch("extract", [job for *_, job in work])
    except jobs.BatchStopped as exc:
        raise _fail(f"{exc}. Run the same command again later to resume.") from exc

    per_filer: dict[str, extract_module.FilerExtraction] = {}
    tokens: dict[str, int] = {}
    for node, form, part, body, job in work:
        if job.key not in results:
            continue
        found = extract_module.verify_answer(
            node, form, part.name, part.start, body, results[job.key].data, taxonomy, companies
        )
        per_filer.setdefault(node, extract_module.FilerExtraction(node=node, form=form)).add(found)
        tokens[node] = tokens.get(node, 0) + results[job.key].usage.get("input_tokens", 0)
    for node, found in per_filer.items():
        path = queue / f"{node.split('/', 1)[-1]}.yaml"
        if path.exists():
            extract_module.keep_review(found, extract_module.load_extraction(path))
        extract_module.save_extraction(path, found)
        dropped = sum(found.dropped.values())
        console.print(
            f"{node}  requires {len(found.requires)}, disclosed {len(found.disclosed)}, "
            f"produces {len(found.produces)}, supplies {len(found.supplies)}, "
            f"new products {len(found.new_products)}, dropped {dropped}",
            markup=False,
        )
    if tokens:
        average = sum(tokens.values()) // len(tokens)
        console.print(
            f"{average:,} input tokens per filer; projection for {len(all_filers)} SEC filers: "
            f"{average * len(all_filers):,} input tokens",
            markup=False,
        )


def _dev_set(entries: list, cache: Path, size: int) -> list:
    """A deterministic dev set that includes 20-F and fallback-layout filers when present."""
    special = []
    for entry in entries:
        cached = edgar.latest_cached(cache, entry.cik)
        if cached is None:
            continue
        _, parts = sections.load_sections(cached)
        if cached.filing.form != "10-K" or any(p.fallback for p in parts):
            special.append(entry)
    chosen = special[: max(size // 4, 1)]
    rest = [e for e in entries if e not in chosen]
    return chosen + judge_module.sample(rest, max(size - len(chosen), 0), seed=0)


# Phase 1b: judge and review (M20)


@app.command("judge-extract")
def judge_extract(
    queue: Annotated[Path, typer.Option("--queue")] = Path("data/review-queue/extract"),
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    seed: Annotated[Path, typer.Option("--seed", exists=True, file_okay=False)] = Path("data/seed"),
    jobs_db: Annotated[Path, typer.Option("--jobs")] = jobs.DEFAULT_JOBS,
) -> None:
    """Judge pending REQUIRES proposals and disclosed numbers; mark them accepted or flagged."""
    labels = {n.value.id: n.value.label for n in load_seed(seed).nodes}
    ciks = {e.node: e.cik for e in edgar.load_universe(universe_path)}
    files = [(path, extract_module.load_extraction(path)) for path in sorted(queue.glob("*.yaml"))]
    items: dict[str, list[judge_module.JudgeItem]] = {"requires": [], "disclosed": []}
    targets: dict[str, object] = {}
    for _, found in files:
        pending = [r for r in found.requires + found.disclosed if r.status == "pending"]
        if not pending:
            continue
        text = _filing_text(cache, ciks.get(found.node))
        company = labels.get(found.node, found.node)
        for r in found.requires:
            if r.status == "pending":
                claim = (
                    f"{labels.get(r.product, r.product)} requires "
                    f"{labels.get(r.input, r.input)} as an input or component."
                )
                key = f"{found.node}/{r.id}"
                context = judge_module.context_around(text, r.start, r.end)
                items["requires"].append(judge_module.JudgeItem(key, claim, context))
                targets[key] = r
        for d in found.disclosed:
            if d.status == "pending":
                claim = (
                    f"{company}: {d.description} is {d.share:.1%} of revenue "
                    f"({d.period}, by {d.basis})."
                )
                key = f"{found.node}/{d.id}"
                context = judge_module.context_around(text, d.start, d.end)
                items["disclosed"].append(judge_module.JudgeItem(key, claim, context))
                targets[key] = d
    runner = jobs.make_codex_job_runner(jobs_db)
    for kind, batch in items.items():
        if not batch:
            continue
        try:
            verdicts = judge_module.judge(batch, runner, f"extract-{kind}")
        except jobs.BatchStopped as exc:
            raise _fail(f"{exc}. Run the same command again later to resume.") from exc
        for key, verdict in verdicts.items():
            target = targets[key]
            target.status = "accepted" if verdict.correct else "flagged"  # type: ignore[attr-defined]
            target.reviewer = "judge"  # type: ignore[attr-defined]
            target.judge = verdict.correct  # type: ignore[attr-defined]
        result = judge_module.precision(verdicts)
        label = "REQUIRES" if kind == "requires" else kind
        console.print(
            f"{label} judged: {result.correct} of {result.total} correct ({result.rate:.0%}), "
            f"entities correct {result.entities_correct} of {result.total}",
            markup=False,
        )
        claims = {item.id: item.claim for item in batch}
        for key, verdict in sorted(verdicts.items()):
            if not verdict.correct:
                console.print(f"  flagged {key}: {claims[key]} {verdict.reason}", markup=False)
    for path, found in files:
        extract_module.save_extraction(path, found)


@app.command("requires-edges")
def requires_edges(
    queue: Annotated[Path, typer.Option("--queue")] = Path("data/review-queue/extract"),
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    sources_path: Annotated[Path, typer.Option("--sources")] = DEFAULT_SOURCES,
    out: Annotated[Path, typer.Option("--out", help="llm source directory.")] = Path("data/llm"),
) -> None:
    """Write accepted REQUIRES proposals into the llm source at confidence 0.5 (M20, D69)."""
    others = {name: d for name, d in _source_dirs(sources_path).items() if name != "llm"}
    current = [
        e.value
        for e in load_sources(others).edges
        if e.value.rel == "REQUIRES" and e.value.valid_to is None
    ]
    totals: dict[str, float] = {}
    for edge in current:
        totals[edge.dst] = totals.get(edge.dst, 0.0) + edge.weight
    extractions = [extract_module.load_extraction(p) for p in sorted(queue.glob("*.yaml"))]
    ciks = {e.node: e.cik for e in edgar.load_universe(universe_path)}
    sources: dict[str, extract_module.FilerSource] = {}
    for found in extractions:
        cached = edgar.latest_cached(cache, ciks[found.node]) if ciks.get(found.node) else None
        if cached is not None:
            sources[found.node] = extract_module.FilerSource(
                cached.filing.url, cached.filing.filing_date
            )
    edges, skipped = extract_module.requires_edges(
        [x for x in extractions if x.node in sources],
        sources,
        existing={(e.src, e.dst) for e in current},
        input_totals=totals,
        accessed=date.today(),
    )
    path = out / "edges" / "requires.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated from accepted text-extraction proposals by `ripple requires-edges` (M20).\n"
        "# Do not edit: accept or reject proposals in data/review-queue/extract instead.\n"
    )
    path.write_text(header + (yaml.safe_dump(edges, sort_keys=False) if edges else "[]\n"))
    console.print(f"{len(edges)} REQUIRES edges written to {path}", markup=False)
    for line in skipped:
        console.print(f"  skipped {line}", markup=False)


@app.command("extract-report")
def extract_report(
    queue: Annotated[Path, typer.Option("--queue")] = Path("data/review-queue/extract"),
    seed: Annotated[Path, typer.Option("--seed", exists=True, file_okay=False)] = Path("data/seed"),
) -> None:
    """Judge precision, review status and seed REQUIRES recall of the extraction (M21, D73)."""
    extractions = [extract_module.load_extraction(p) for p in sorted(queue.glob("*.yaml"))]
    for label, name in (("REQUIRES", "requires"), ("disclosed", "disclosed")):
        rows = [item for x in extractions for item in getattr(x, name)]
        judged = [item.judge for item in rows if item.judge is not None]
        rate = f"{sum(judged) / len(judged):.0%}" if judged else "n/a"
        counts = {s: sum(item.status == s for item in rows) for s in (
            "accepted", "rejected", "flagged", "pending", "applied")}  # fmt: skip
        console.print(
            f"{label}: {len(rows)} proposed; judged {len(judged)}, precision {rate}; "
            + ", ".join(f"{s} {n}" for s, n in counts.items()),
            markup=False,
        )
    seed_links = {
        (e.value.src, e.value.dst) for e in load_seed(seed).edges if e.value.rel == "REQUIRES"
    }
    found, missed = extract_module.requires_recall(seed_links, extractions)
    share = f"{len(found) / len(seed_links):.0%}" if seed_links else "n/a"
    console.print(f"seed REQUIRES recall: {len(found)} of {len(seed_links)} ({share})")
    for src, dst in sorted(missed):
        console.print(f"  missed {src} REQUIRES {dst}", markup=False)


signals_app = typer.Typer(help="News coverage signals from GDELT (M24).", no_args_is_help=True)
app.add_typer(signals_app, name="signals")


def _series_targets(
    nodes: dict[str, Node],
    themes: list[str] | None,
    companies: list[str] | None,
    everything: bool,
) -> list[tuple[str, str, str]]:
    """(kind, node ID, query) for each series to fetch."""
    targets: list[tuple[str, str, str]] = []
    by_type = {
        kind: [n for n, v in nodes.items() if v.type == kind] for kind in ("theme", "company")
    }
    wanted_themes = themes or (by_type["theme"] if everything else [])
    wanted_companies = companies or (by_type["company"] if everything else [])
    for node_id in wanted_themes:
        node = nodes.get(node_id)
        if node is None:
            raise _fail(f"unknown node {node_id}")
        targets.append(("theme", node_id, news.theme_query(node)))
    for node_id in wanted_companies:
        node = nodes.get(node_id)
        if node is None:
            raise _fail(f"unknown node {node_id}")
        targets.append(("company", node_id, news.company_query(node)))
    return targets


LOCK_RETRIES = 6
LOCK_WAIT = 10.0


def _append_coverage(
    db: Path, rows: list[store_module.CoverageRow], sleep: Callable[[float], None] = time.sleep
) -> int:
    """Append one series, retrying while another process holds the store's write lock (a
    `ripple load`, or any command that opens the store for writing), so a long fetch does not
    crash on a moment's contention (D117)."""
    for attempt in range(LOCK_RETRIES):
        try:
            with Store(db) as store:
                return store.add_coverage(rows)
        except duckdb.IOException:
            if attempt == LOCK_RETRIES - 1:
                raise
            sleep(LOCK_WAIT)
    raise AssertionError("unreachable")


@signals_app.command("fetch")
def signals_fetch(
    theme: Annotated[
        list[str] | None, typer.Option("--theme", help="Theme IDs; repeatable.")
    ] = None,
    company: Annotated[
        list[str] | None, typer.Option("--company", help="Company IDs; repeatable.")
    ] = None,
    all_nodes: Annotated[bool, typer.Option("--all", help="Every theme and company.")] = False,
    start: Annotated[
        datetime, typer.Option("--from", formats=["%Y-%m-%d"], help="First day (>= 2017-01-01).")
    ] = datetime(2022, 1, 1),
    end: Annotated[
        datetime | None,
        typer.Option("--to", formats=["%Y-%m-%d"], help="Last day (default today)."),
    ] = None,
    tone: Annotated[bool, typer.Option("--tone/--no-tone", help="Also fetch mean tone.")] = True,
    refetch: Annotated[
        bool,
        typer.Option(
            "--refetch", help="Fetch series the store already holds with the current query."
        ),
    ] = False,
    db: DbOption = DEFAULT_DB,
    cache: Annotated[Path, typer.Option("--cache", help="Response cache.")] = news.DEFAULT_CACHE,
) -> None:
    """Fetch daily coverage series and append them to the store.

    One request covers a whole date range, so a full pass is about one request per series.
    Responses are cached and series already held with the current query are skipped, so a
    rerun resumes. Pass a fixed --to when resuming across days: the end date is part of the
    cache key. The store is opened only to read the targets and to append each series, so a
    long fetch does not lock out `ripple load` or the MCP server.
    """
    first, last = start.date(), (end.date() if end else today_utc())
    if last < first:
        raise _fail("--to is before --from")
    with Store(db, read_only=True) as store:
        nodes = store.snapshot(today_utc()).nodes
        held = store.held_series()
    targets = _series_targets(nodes, theme, company, all_nodes)
    if not targets:
        raise _fail("nothing to fetch: pass --theme, --company or --all")
    if not refetch:
        # GDELT can lag a day or two behind the end asked for, so near enough counts as held.
        enough = last - timedelta(days=2)
        done = [
            t for t in targets if (held.get((t[1], news.query_hash(t[2]))) or date.min) >= enough
        ]
        targets = [t for t in targets if t not in done]
        if done:
            console.print(f"{len(done)} series already held through {last}; skipped")
    written = 0
    with news.NewsClient(cache_dir=cache) as client:
        for kind, node_id, query in targets:
            try:
                points = client.volume(query, first, last)
                # Tone only matters for the theme reversal flag (D90); a company series is
                # only ever summed into attention, so half the requests are unnecessary.
                want_tone = tone and kind == "theme"
                tones = client.tone(query, first, last) if want_tone else {}
            except news.RateLimited as exc:
                console.print(f"stopped: {exc}", style="red", markup=False)
                break
            except news.NewsError as exc:
                console.print(f"{node_id}: {exc}", style="yellow", markup=False)
                continue
            rows = [
                store_module.CoverageRow(
                    kind=kind,
                    key=node_id,
                    day=p.day,
                    matched=p.matched,
                    norm=p.norm,
                    tone=tones.get(p.day),
                    query_hash=news.query_hash(query),
                )
                for p in points
            ]
            written += _append_coverage(db, rows)
            total = sum(p.matched for p in points)
            console.print(f"{node_id}: {len(rows)} days, {total} matching articles", markup=False)
    console.print(
        f"{written} coverage rows appended; "
        f"{client.requests} requests, {client.cache_hits} cache hits"
    )


@signals_app.command("show")
def signals_show(
    node: Annotated[
        str | None, typer.Argument(help="Theme or company ID; omit for a summary.")
    ] = None,
    days: Annotated[int, typer.Option("--days", help="How many recent days to print.")] = 30,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Print a coverage series, or a summary of every series held."""
    with Store(db, read_only=True) as store:
        if node is None:
            table = Table("Kind", "Node", "Days", "First", "Last")
            for kind, key, count, first, last in store.coverage_summary():
                table.add_row(kind, key, str(count), str(first), str(last))
            console.print(table)
            return
        rows = store.coverage(node)
        if not rows:
            raise _fail(f"no coverage for {node}; run `ripple signals fetch --theme {node}`")
        table = Table("Day", "Matched", "All articles", "Share per 100k", "Tone")
        for row in rows[-days:]:
            table.add_row(
                str(row.day),
                str(row.matched),
                f"{row.norm:,}",
                f"{row.share * 1e5:.2f}",
                "-" if row.tone is None else f"{row.tone:+.2f}",
            )
        console.print(table)
        console.print(f"{len(rows)} days held for {node}")


@app.command()
def trending(
    window: Annotated[int, typer.Option("--window", help="Look back this many days.")] = 90,
    theme: Annotated[str | None, typer.Option("--theme", help="Only this theme.")] = None,
    min_surprise: Annotated[
        float, typer.Option("--min-surprise", help="-log10 p threshold after dispersion.")
    ] = signal.MIN_SURPRISE,
    db: DbOption = DEFAULT_DB,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Themes whose news coverage burst recently, strongest first.

    A burst means the theme is intensifying; the DRIVES polarity decides who gains and who
    loses. `ratio` is coverage against the theme's own 90-day normal, `surprise` is -log10 of
    the Poisson tail probability after the overdispersion correction. Never multiply either by
    an exposure: they are units of surprise, not of demand (D88).
    """
    today = today_utc()
    with Store(db, read_only=True) as store:
        if as_json:
            print(json.dumps(signal.trending_themes(store, today, window, min_surprise), indent=2))
            return
        nodes = store.snapshot(today).nodes
        found, unmeasured = signal.trending(
            store, today, window, min_surprise, themes=[theme] if theme else None
        )

    if unmeasured:
        console.print(f"{len(unmeasured)} themes have no coverage series yet", style="yellow")
    if not found:
        console.print(f"no bursts in the last {window} days at surprise >= {min_surprise}")
        return
    table = Table("Theme", "Window", "Days", "Ratio", "Surprise", "Articles", "Flag")
    for b in found:
        label = nodes[b.key].label if b.key in nodes else b.key
        table.add_row(
            label,
            f"{b.start} to {b.end}",
            str(b.days),
            f"{b.peak_ratio:.1f}x",
            f"{b.peak_surprise:.1f}",
            str(b.matched),
            b.tone_flag or "",
        )
    console.print(table)


@app.command("attention")
def attention_command(
    theme: Annotated[
        str | None, typer.Option("--theme", help="Rank within this theme's exposed companies.")
    ] = None,
    top: Annotated[int, typer.Option("--top")] = 25,
    as_of: AsOfOption = None,
    db: DbOption = DEFAULT_DB,
) -> None:
    """How much news coverage each company gets, most covered first.

    Attention is the company's share of all coverage over the last 90 days (M27, D91). It is not
    paired with a theme, so a company famous for something unrelated looks crowded here.
    """
    day = _as_of(as_of)
    with Store(db, read_only=True) as store:
        snapshot = store.snapshot(day)
        if theme:
            graph = build_graph(snapshot)
            try:
                companies = list(company_exposures(graph, Shock(theme), DEFAULT_MAX_HOPS))
            except ValueError as exc:
                raise _fail(str(exc)) from exc
        else:
            companies = [n for n, v in snapshot.nodes.items() if v.type == "company"]
        found = attention_module.company_attention(store, companies, day)
    if not found:
        raise _fail("no attention data; run `ripple signals fetch --all` first")
    pcts = attention_module.percentiles({c: a.share for c, a in found.items()})
    ordered = sorted(found.values(), key=lambda a: -a.share)
    table = Table("#", "Company", "Share per 100k", "%ile", "Articles", "Days")
    for rank, item in enumerate(ordered[:top], start=1):
        label = (
            snapshot.nodes[item.company].label if item.company in snapshot.nodes else item.company
        )
        table.add_row(
            str(rank),
            label,
            f"{item.share * 1e5:.2f}",
            f"{pcts[item.company]:.2f}",
            f"{item.matched:,}",
            str(item.days),
        )
    console.print(table)
    missing = [c for c in companies if c not in found]
    if missing:
        console.print(
            f"{len(missing)} of {len(companies)} companies have no series; "
            "their attention is unknown, not zero",
            style="yellow",
        )


@app.command("events")
def events_command(
    path: Annotated[
        Path, typer.Option("--events", help="Pre-registered event set.")
    ] = events_module.DEFAULT_EVENTS,
    min_surprise: Annotated[float, typer.Option("--min-surprise")] = signal.MIN_SURPRISE,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Check the pre-registered event set against detected bursts (M29).

    The event set was fixed before any coverage was fetched. Nothing here may be tuned in
    response to a miss: that would make the measurement lookahead (D101).
    """
    with Store(db, read_only=True) as store:
        report = events_module.check_events(store, path, min_surprise=min_surprise)
        themes = sorted({r.event.theme for r in report.results})
        extra = events_module.false_positives(store, themes, path, min_surprise=min_surprise)

    table = Table("Event", "Date", "Theme", "Dir", "Hit", "Burst window", "Ratio", "Flag")
    for r in report.results:
        e = r.event
        window = f"{r.burst.start} to {r.burst.end}" if r.burst else ""
        if not r.hit and r.nearest_gap is not None:
            window = f"nearest burst {r.nearest_gap}d away"
            if r.shares_burst_with:
                window += f" (same burst as {r.shares_burst_with})"
        table.add_row(
            e.id + ("*" if e.approximate else ""),
            str(e.day),
            e.theme.removeprefix("theme/"),
            e.direction,
            "yes" if r.hit else "no",
            window,
            f"{r.burst.peak_ratio:.1f}x" if r.burst else "",
            (r.burst.tone_flag or "") if r.burst else "",
        )
    console.print(table)
    console.print("* date recalled rather than checked; reported separately")
    console.print(
        f"hit rate {report.hit_rate:.0%} of {len(report.results)} events "
        f"(exact dates only: {report.exact_hit_rate:.0%} of {len(report.exact)}); "
        f"bar {report.min_hit_rate:.0%} -> {'met' if report.met else 'NOT met'}"
    )
    if report.flag_precision is not None:
        console.print(
            f"tone reversal flag correct on {report.flag_precision:.0%} of matched events "
            f"({len(report.reversals)} are reversals)"
        )
    total_extra = sum(len(v) for v in extra.values())
    console.print(f"{total_extra} bursts matched no event across {len(themes)} themes")


@app.command("verify-attention")
def verify_attention_command(
    theme: str,
    top: Annotated[int, typer.Option("--top", help="How many ranked companies to check.")] = (
        attention_module.VERIFY_TOP
    ),
    as_of: AsOfOption = None,
    db: DbOption = DEFAULT_DB,
    cache: Annotated[Path, typer.Option("--cache")] = news.DEFAULT_CACHE,
) -> None:
    """Check the top of a ranking with theme-paired queries (M27, D91).

    Company-wide attention cannot tell "famous" from "famous for this theme". This runs one extra
    GDELT request per company to show what share of the theme's own articles named it, so a lead
    can be checked before it is acted on. Costs top+1 requests, so keep `top` small.
    """
    day = _as_of(as_of)
    with Store(db, read_only=True) as store:
        snapshot = store.snapshot(day)
        node = snapshot.nodes.get(theme)
        if node is None or node.type != "theme":
            raise _fail(f"{theme} is not a theme in the graph")
        try:
            result = score(store, theme, as_of=day, limit=top)
        except ValueError as exc:
            raise _fail(str(exc)) from exc
        pairs = [
            (r.company, news.company_query(snapshot.nodes[r.company]))
            for r in result.results
            if r.company in snapshot.nodes
        ]
        shares = {r.company: r.attention for r in result.results if r.attention is not None}

    with news.NewsClient(cache_dir=cache) as client:
        try:
            checks = attention_module.verify_attention(
                client, news.theme_query(node), pairs, day, company_shares=shares
            )
        except news.RateLimited as exc:
            raise _fail(f"stopped: {exc}") from exc

    table = Table("Company", "Paired share", "Joint articles", "Company-wide", "Enough data")
    for check in checks:
        label = snapshot.nodes[check.company].label
        table.add_row(
            label,
            f"{check.paired_share:.3%}",
            str(check.both_articles),
            "-" if check.company_share is None else f"{check.company_share * 1e5:.2f}/100k",
            "yes" if check.enough_data else "no",
        )
    console.print(table)
    console.print(
        f"of {checks[0].theme_articles if checks else 0} articles about this theme in the window. "
        "A company high on company-wide attention but low here is famous for something else.",
        style="dim",
    )


@app.command("calibrate")
def calibrate_command(
    target: Annotated[
        float, typer.Option("--target", help="Bursts per theme per year to aim for.")
    ] = signal.TARGET_BURSTS_PER_YEAR,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Choose the burst threshold from how often bursts should fire (M25, D107).

    This reads only the coverage series, never the event dates, so the pre-registered event set
    stays a genuine out-of-sample test. The judgement it encodes is "how rare should a burst be",
    which does not depend on which events happened.
    """
    with Store(db, read_only=True) as store:
        nodes = store.snapshot(today_utc()).nodes
        series = {}
        for node_id, node in nodes.items():
            if node.type != "theme" or not node.query:
                continue
            rows = store.coverage(node_id)
            if rows:
                series[node_id] = rows
    if not series:
        raise _fail("no coverage held; run `ripple signals fetch --all` first")
    result = signal.calibrate_threshold(series, target=target)
    console.print(
        f"{result.series} series over {result.years:.1f} years; "
        f"target {result.target:.1f} bursts per theme-year"
    )
    table = Table("Threshold", "Bursts/theme-year")
    shown = [row for row in result.curve if row[0] * 2 % 1 == 0][:24]
    for threshold, rate in shown:
        mark = "  <- chosen" if threshold == result.threshold else ""
        table.add_row(f"{threshold:.2f}{mark}", f"{rate:.2f}")
    console.print(table)
    console.print(
        f"chosen threshold {result.threshold:.2f} gives {result.rate:.2f} bursts per theme-year; "
        f"MIN_SURPRISE in ripple/signal.py is {signal.MIN_SURPRISE}"
    )


@app.command("query-precision")
def query_precision_command(
    theme: Annotated[
        list[str] | None, typer.Option("--theme", help="Only these themes; default all.")
    ] = None,
    size: Annotated[int, typer.Option("--size", help="Articles sampled per theme.")] = (
        queries.SAMPLE_SIZE
    ),
    jobs_db: Annotated[Path, typer.Option("--jobs")] = jobs.DEFAULT_JOBS,
    report: Annotated[Path, typer.Option("--report")] = Path("data/query-precision.yaml"),
    db: DbOption = DEFAULT_DB,
    cache: Annotated[Path, typer.Option("--cache")] = news.DEFAULT_CACHE,
    as_of: AsOfOption = None,
) -> None:
    """Measure how precise each theme's GDELT query is (M26).

    Costs one GDELT request per theme plus one Codex batch per 20 articles. Judged on relevance
    only, never on whether a burst lines up with the event set, which would make M29 lookahead
    (D104). The report stores URLs, verdicts and reasons, never headlines (D72).
    """
    day = _as_of(as_of)
    with Store(db, read_only=True) as store:
        nodes = store.snapshot(day).nodes
        wanted = theme or [n for n, v in nodes.items() if v.type == "theme" and v.query]
        themes = {}
        for node_id in wanted:
            node = nodes.get(node_id)
            if node is None or node.type != "theme":
                raise _fail(f"{node_id} is not a theme")
            themes[node_id] = node

    with news.NewsClient(cache_dir=cache) as client:
        try:
            samples = queries.sample_articles(client, list(themes.values()), day, size=size)
        except news.RateLimited as exc:
            raise _fail(f"stopped: {exc}. Rerun; cached responses are kept.") from exc
        except news.NewsError as exc:
            raise _fail(str(exc)) from exc

    empty = [t for t, arts in samples.items() if not arts]
    results = queries.measure(themes, samples, jobs.make_codex_job_runner(jobs_db))

    table = Table("Theme", "Sampled", "Relevant", "Precision", "Bar")
    for theme_id in wanted:
        r = results.get(theme_id)
        if r is None:
            continue
        table.add_row(
            theme_id.removeprefix("theme/"),
            str(r.sampled),
            str(r.relevant),
            "-" if not r.measurable else f"{r.rate:.0%}",
            "too thin" if not r.measurable else ("met" if r.met else "MISSED"),
        )
    console.print(table)

    measurable = [r for r in results.values() if r.measurable]
    if measurable:
        pooled = sum(r.relevant for r in measurable) / sum(r.sampled for r in measurable)
        console.print(
            f"pooled precision {pooled:.0%} over {len(measurable)} measurable themes; "
            f"bar {queries.PRECISION_BAR:.0%}; "
            f"{sum(1 for r in measurable if not r.met)} below it"
        )
    if empty:
        console.print(f"no recent articles at all for: {', '.join(empty)}", style="yellow")

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        yaml.safe_dump(
            {
                "measured": day,
                "bar": queries.PRECISION_BAR,
                "themes": {
                    t: {
                        "sampled": r.sampled,
                        "relevant": r.relevant,
                        "rate": round(r.rate, 3),
                        "measurable": r.measurable,
                        "verdicts": [
                            {"url": u, "relevant": c, "reason": why} for u, c, why in r.verdicts
                        ],
                    }
                    for t, r in results.items()
                },
            },
            sort_keys=False,
        )
    )
    console.print(f"written to {report}")


# --- evidence verification (Phase 3, M33, D113) ----------------------------------------

verify_app = typer.Typer(help="Rule on evidence items: the ledger behind `verified` (M33).")
app.add_typer(verify_app, name="verify")
QueueOption = Annotated[Path, typer.Option("--queue", help="Mapping review queue.")]
RULING_KEYS = {"v": "verified", "w": "wrong-note", "d": "dead-link", "u": "unsupported"}


def _verify_state(db: Path, ledger: Path) -> tuple[store_module.Snapshot, dict]:
    with Store(db, read_only=True) as store:
        snapshot = store.snapshot(today_utc())
    return snapshot, verify_module.load_ledger(ledger)


@verify_app.command("status")
def verify_status(
    db: DbOption = DEFAULT_DB, ledger: LedgerOption = verify_module.DEFAULT_LEDGER
) -> None:
    """How many evidence items are ruled on, by source layer."""
    snapshot, rulings = _verify_state(db, ledger)
    status = verify_module.status(snapshot, rulings)
    table = Table("Source", "Items", "Ruled", "Left")
    for source, (count, ruled) in sorted(status.by_source.items()):
        table.add_row(source, str(count), str(ruled), str(count - ruled))
    console.print(table)
    console.print(
        f"{status.ruled} of {status.total} ruled: {status.verified} verified, {status.failed} "
        f"failed; {status.total - status.ruled} left across {status.documents_left} documents"
    )
    held = {i.key for i in verify_module.items(snapshot)}
    stale = sum(key not in held for key in rulings)
    if stale:
        console.print(
            f"{stale} rulings match no current item: their note or URL changed, so the item "
            "needs a new ruling",
            style="yellow",
        )
    rows = [*snapshot.edges, *(e for alts in snapshot.alternatives.values() for _, e in alts)]
    stored = sum(item.verified for e in rows for item in e.evidence)
    if stored < status.verified:
        console.print(
            f"the store holds {stored} verified items: run `ripple load` to store the rest",
            style="yellow",
        )


def _print_document(
    url: str,
    group: list[verify_module.Item],
    checks: dict[tuple[str, str, str], verify_module.Recomputed],
    labels: dict[str, str],
) -> None:
    console.print(f"\n{url}", style="bold", markup=False)
    for n, item in enumerate(group, start=1):
        e = item.edge
        role = "" if item.winner else "  (lost precedence)"
        console.print(
            f"  {n}. {e.id}  {labels.get(e.src, e.src)} {e.rel} {labels.get(e.dst, e.dst)}  "
            f"weight {e.weight} ({e.weight_source}), {item.source}{role}",
            markup=False,
        )
        console.print(f"     note: {item.note}", markup=False)
        check = checks.get(item.key)
        if check is not None:
            mark = "recompute ok" if check.match else "RECOMPUTE MISMATCH"
            console.print(f"     {mark}: {check.detail}", markup=False)


def _checks(
    group: list[verify_module.Item],
    snapshot: store_module.Snapshot,
    universe_path: Path,
    cache: Path,
    queue: Path,
) -> dict[tuple[str, str, str], verify_module.Recomputed]:
    if not any(i.source == "xbrl" for i in group):
        return {}
    companies = [n for n in snapshot.nodes.values() if n.type == "company"]
    universe = edgar.load_universe(universe_path) if universe_path.exists() else []
    found = verify_module.recompute(group, universe, cache, queue, companies)
    return {r.item.key: r for r in found}


@verify_app.command("next")
def verify_next(
    source: Annotated[str | None, typer.Option("--source", help="seed, xbrl or llm.")] = None,
    count: Annotated[int, typer.Option("--count", help="How many documents to show.")] = 1,
    db: DbOption = DEFAULT_DB,
    ledger: LedgerOption = verify_module.DEFAULT_LEDGER,
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    queue: QueueOption = mapping.DEFAULT_QUEUE,
) -> None:
    """Show the next documents to check, with every unruled item that cites them.

    `xbrl` items show whether their arithmetic recomputes from the cached filing.
    """
    snapshot, rulings = _verify_state(db, ledger)
    groups = verify_module.unruled_by_document(snapshot, rulings, source)
    if not groups:
        console.print("nothing left to rule on")
        return
    labels = {n: v.label for n, v in snapshot.nodes.items()}
    for url, group in list(groups.items())[:count]:
        _print_document(url, group, _checks(group, snapshot, universe_path, cache, queue), labels)
    console.print(f"\n{len(groups)} documents left")


@verify_app.command("rule")
def verify_rule(
    ruling: Annotated[str, typer.Argument(help="verified, wrong-note, dead-link or unsupported.")],
    document: Annotated[
        str | None, typer.Option("--document", help="Rule every unruled item citing this URL.")
    ] = None,
    edge_id: Annotated[str | None, typer.Option("--edge", help="Only this edge.")] = None,
    source: Annotated[str | None, typer.Option("--source", help="Only this layer.")] = None,
    reason: Annotated[str, typer.Option("--reason", help="Why, for a failed ruling.")] = "",
    by: Annotated[str, typer.Option("--by", help="Who read the source.")] = "user",
    db: DbOption = DEFAULT_DB,
    ledger: LedgerOption = verify_module.DEFAULT_LEDGER,
) -> None:
    """Record a ruling for the unruled items citing a document, or for one edge's items."""
    if ruling not in verify_module.RULINGS:
        raise _fail(f"ruling must be one of {', '.join(verify_module.RULINGS)}")
    if document is None and edge_id is None:
        raise _fail("pass --document URL, --edge ID, or both")
    if ruling != "verified" and not reason:
        raise _fail("a failed ruling needs --reason")
    snapshot, rulings = _verify_state(db, ledger)
    chosen = [
        i
        for i in verify_module.items(snapshot)
        if i.key not in rulings
        and (document is None or i.url == document)
        and (edge_id is None or i.edge.id == edge_id)
        and (source is None or i.source == source)
    ]
    if not chosen:
        raise _fail("no unruled item matches")
    written = verify_module.append_rulings(
        {i.key: _entry(i, ruling, by, reason) for i in chosen}.values(), ledger
    )
    console.print(f"{written} items ruled {ruling}; run `ripple load` to store the rulings")


def _entry(item: verify_module.Item, ruling: str, by: str, reason: str) -> verify_module.Entry:
    return verify_module.Entry(
        edge=item.edge.id,
        url=item.url,
        note_hash=verify_module.note_hash(item.note),
        ruling=ruling,  # type: ignore[arg-type]
        date=today_utc(),
        by=by,
        reason=reason,
    )


@verify_app.command("walk")
def verify_walk(
    source: Annotated[str | None, typer.Option("--source", help="seed, xbrl or llm.")] = None,
    by: Annotated[str, typer.Option("--by", help="Who is reading.")] = "user",
    db: DbOption = DEFAULT_DB,
    ledger: LedgerOption = verify_module.DEFAULT_LEDGER,
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    queue: QueueOption = mapping.DEFAULT_QUEUE,
) -> None:
    """Walk the unruled documents one at a time and rule on them interactively.

    Open each URL, read it against the notes, then rule. Rulings are saved after every
    document, so quitting loses nothing.
    """
    snapshot, rulings = _verify_state(db, ledger)
    groups = verify_module.unruled_by_document(snapshot, rulings, source)
    labels = {n: v.label for n, v in snapshot.nodes.items()}
    done = 0
    for number, (url, group) in enumerate(groups.items(), start=1):
        console.print(f"\n[{number}/{len(groups)}]", markup=False)
        _print_document(url, group, _checks(group, snapshot, universe_path, cache, queue), labels)
        choice = typer.prompt("  [a] all verified, [e] each item, [s] skip, [q] quit", default="e")
        if choice == "q":
            break
        if choice == "s":
            continue
        entries = []
        for n, item in enumerate(group, start=1):
            if choice == "a":
                entries.append(_entry(item, "verified", by, ""))
                continue
            key = typer.prompt(f"  {n}: [v]erified [w]rong-note [d]ead-link [u]nsupported [s]kip")
            if key not in RULING_KEYS:
                continue
            why = "" if key == "v" else typer.prompt("     reason")
            entries.append(_entry(item, RULING_KEYS[key], by, why))
        done += verify_module.append_rulings(entries, ledger)
    console.print(f"{done} items ruled; run `ripple load` to store the rulings")


@verify_app.command("links")
def verify_links(
    out: Annotated[Path, typer.Option("--out", help="Report: URL and status only.")] = Path(
        "data/link-check.yaml"
    ),
    db: DbOption = DEFAULT_DB,
) -> None:
    """Request every evidence URL once and report dead and moved links. A pre-pass for the
    person ruling: it records no ruling, and a live page may still not support its note."""
    snapshot, _ = _verify_state(db, verify_module.DEFAULT_LEDGER)
    urls = {i.url for i in verify_module.items(snapshot)}
    try:
        agent = edgar.user_agent_from_env()
    except edgar.EdgarConfigError as exc:
        raise _fail(str(exc)) from exc
    console.print(f"checking {len(urls)} URLs…")
    with verify_module.link_client(agent) as client:
        links = verify_module.check_links(urls, client)
    bad = [link for link in links if not link.ok]
    moved = [link for link in links if link.moved]
    for link in bad:
        console.print(f"DEAD {link.status or link.error}  {link.url}", style="red", markup=False)
    for link in moved:
        console.print(f"moved  {link.url} -> {link.final_url}", style="yellow", markup=False)
    out.write_text(
        "# Evidence link check (M33): status only, no content, no rulings.\n"
        + yaml.safe_dump(
            {
                "checked": today_utc().isoformat(),
                "links": [dataclasses.asdict(link) for link in links],
            },
            sort_keys=False,
        )
    )
    console.print(
        f"{len(links)} checked: {len(links) - len(bad)} live ({len(moved)} redirected), "
        f"{len(bad)} dead or unreachable; report in {out}"
    )


@verify_app.command("recompute")
def verify_recompute(
    db: DbOption = DEFAULT_DB,
    universe_path: UniverseOption = edgar.DEFAULT_UNIVERSE,
    cache: CacheOption = edgar.DEFAULT_CACHE,
    queue: QueueOption = mapping.DEFAULT_QUEUE,
) -> None:
    """Redo the arithmetic behind every `xbrl` evidence item from the cached filings."""
    snapshot, _ = _verify_state(db, verify_module.DEFAULT_LEDGER)
    groups = verify_module.unruled_by_document(snapshot, {}, "xbrl")
    table = Table("Filing", "Items", "Match", title="xbrl evidence recomputed")
    mismatches: list[verify_module.Recomputed] = []
    for url, group in groups.items():
        checks = _checks(group, snapshot, universe_path, cache, queue)
        ok = sum(c.match for c in checks.values())
        mismatches += [c for c in checks.values() if not c.match]
        table.add_row(url.rsplit("/", 1)[-1], str(len(group)), f"{ok}/{len(checks)}")
    console.print(table)
    for check in mismatches:
        console.print(f"{check.item.edge.label}: {check.detail}", style="yellow", markup=False)
    console.print(f"{len(mismatches)} mismatches")


# --- thematic briefs (Phase 3, M35, D114) ------------------------------------------------


@app.command("check-brief")
def check_brief(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="The brief.")],
    theme: Annotated[str, typer.Option("--theme", help="The theme the brief is about.")],
    as_of: AsOfOption = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Resolve every node ID, edge ID, URL and exposure a brief cites (the rubric's mechanical
    half). Exits 1 unless every citation resolves."""
    with Store(db, read_only=True) as store:
        result = brief_module.check(path.read_text(), store, theme, _as_of(as_of))
    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        console.print(
            f"{path}: {len(result.edges)} edges, {len(result.nodes)} nodes, {len(result.urls)} "
            f"URLs, {len(result.exposures)} exposures cited, as of {result.as_of}",
            markup=False,
        )
        for problem in result.problems:
            console.print(f"  FAIL {problem}", style="red", markup=False)
        console.print(
            f"  {len(result.unverified_edges)} cited edges have only unverified evidence; "
            f"{len(result.bucket_edges)} carry bucket weights; the rubric asks the brief to say so",
            markup=False,
        )
        console.print("mechanical check: " + ("pass" if result.passed else "FAIL"))
    if not result.passed:
        raise typer.Exit(code=1)


@app.command("brief")
def brief_command(
    theme: str,
    out: Annotated[Path, typer.Option("--out", help="Directory for briefs.")] = (
        brief_module.DEFAULT_OUT
    ),
    prompts: Annotated[Path, typer.Option("--prompts", help="Pre-registered prompts.")] = (
        brief_module.DEFAULT_PROMPTS
    ),
    effort: Annotated[str, typer.Option("--effort", help="Codex reasoning effort.")] = "medium",
    db: DbOption = DEFAULT_DB,
) -> None:
    """Have Codex write the brief on THEME with the ripple MCP server as its only tool, then
    run the citation check. Writes the brief and the list of tool calls it made."""
    server = codex_module.McpServer(
        command=str(Path(sys.executable).with_name("ripple-mcp")),
        env={"RIPPLE_DB": str(db.resolve())},
    )
    runner = codex_module.CodexRunner(codex_module.default_config(effort=effort))
    try:
        result = brief_module.write(runner, theme, server, prompts)
    except (ValueError, CodexError) as exc:
        raise _fail(str(exc)) from exc
    slug = theme.split("/", 1)[-1]
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{slug}.md").write_text(result.text)
    calls = [dataclasses.asdict(c) for c in result.calls]
    (out / f"{slug}.calls.json").write_text(
        json.dumps({"usage": result.usage, "calls": calls}, indent=2)
    )
    failed = sum(not c.ok for c in result.calls)
    console.print(
        f"{out / (slug + '.md')}: {len(result.calls)} tool calls ({failed} failed), "
        f"{result.usage.get('input_tokens', 0):,} input tokens",
        markup=False,
    )
    with Store(db, read_only=True) as store:
        check = brief_module.check(result.text, store, theme, today_utc())
    console.print("mechanical check: " + ("pass" if check.passed else "FAIL"))
    for problem in check.problems:
        console.print(f"  FAIL {problem}", style="red", markup=False)


# --- read-only browser UI (off-roadmap tool, docs/ui.md) ---------------------------------


@app.command("ui")
def ui_command(
    port: Annotated[int, typer.Option("--port", help="Port on 127.0.0.1.")] = 8765,
    db: DbOption = DEFAULT_DB,
    ledger: LedgerOption = verify_module.DEFAULT_LEDGER,
) -> None:
    """Serve the read-only UI at http://127.0.0.1:PORT. It never writes to the store."""
    import uvicorn

    from ripple.ui.app import create_app

    console.print(f"Ripple UI on http://127.0.0.1:{port}  (store {db}, read-only)")
    uvicorn.run(create_app(db, ledger), host="127.0.0.1", port=port, log_level="warning")
