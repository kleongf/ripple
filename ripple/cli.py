import dataclasses
import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from ripple import edgar, growth, jobs, mapping, sections, xbrl, xbrl_edges
from ripple import extract as extract_module
from ripple import judge as judge_module
from ripple.codex import CodexError
from ripple.graph import Graph, build_graph
from ripple.load import load_seed, load_sources
from ripple.model import DEFAULT_MIN_CONFIDENCE, today_utc
from ripple.paths import Path as ExplainPath
from ripple.paths import top_paths
from ripple.propagate import Direction
from ripple.score import DEFAULT_MAX_HOPS, OBVIOUS_HOPS, JitterMode, score, sensitivity
from ripple.store import SeedInvalidError, Store
from ripple.validate import product_path
from ripple.validate import validate as validate_seed

DEFAULT_DB = Path("data/ripple.duckdb")
DEFAULT_SOURCES = Path("data/sources.yaml")
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
) -> None:
    """Check seed YAML against the Phase 0 validation rules."""
    seed = load_seed(directory)
    problems = validate_seed(seed, today=today_utc())
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
    db: DbOption = DEFAULT_DB,
) -> None:
    """Validate source directories together and record each in the store (D40).

    Without arguments, loads every source listed in data/sources.yaml (or data/seed alone).
    Never overwrites; supersedes, and only within the sources loaded.
    """
    if directory is not None:
        dirs = {source: directory}
    else:
        dirs = _source_dirs(sources or DEFAULT_SOURCES)
    with Store(db) as store:
        try:
            report = store.load_sources(dirs)
        except SeedInvalidError as exc:
            for problem in exc.problems:
                console.print(str(problem), style="red", markup=False)
            raise _fail(f"not loaded: {len(exc.problems)} errors") from exc
    console.print(
        f"{db} ({', '.join(dirs)}): inserted {report.inserted}, superseded {report.superseded}, "
        f"unchanged {report.unchanged}"
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
        int, typer.Option("--obvious-hops", help="Hide companies this many hops away or fewer.")
    ] = OBVIOUS_HOPS,
    as_of: AsOfOption = None,
    min_confidence: MinConfidenceOption = DEFAULT_MIN_CONFIDENCE,
    max_hops: MaxHopsOption = DEFAULT_MAX_HOPS,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Rank companies by exposure to a theme shock."""
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
                limit=top,
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
    table = Table(title=f"{theme} {arrow}  (as of {result.as_of}, {result.theme_kind})")
    for column in ("#", "Company", "Ticker", "Exposure", "Hops", "Guessed", "Conf"):
        justify = "right" if column in ("#", "Exposure", "Hops") else "left"
        table.add_column(column, justify=justify, no_wrap=True)
    table.add_column("Top path (via)", overflow="fold")
    for rank, r in enumerate(result.results, start=1):
        top_path = " → ".join(labels.get(n, n) for n in r.paths[0].nodes[1:-1]) if r.paths else ""
        table.add_row(
            str(rank),
            r.label,
            r.ticker or "",
            f"{r.exposure:+.4f}",
            str(r.min_hops or ""),
            str(r.guessed_weights),
            f"{r.path_confidence:.2f}",
            top_path,
        )
    console.print(table)


@app.command()
def explain(
    theme: str,
    company: str,
    k: Annotated[int, typer.Option("--k", help="Number of paths.")] = 3,
    as_of: AsOfOption = None,
    min_confidence: MinConfidenceOption = DEFAULT_MIN_CONFIDENCE,
    max_hops: MaxHopsOption = DEFAULT_MAX_HOPS,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Show the strongest paths from a theme to a company, with the evidence for each edge."""
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
