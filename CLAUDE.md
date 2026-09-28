# Ripple

Ripple ranks public companies by their exposure to trends. A trend is a shock on
a theme node; the shock propagates through a two-layer graph (products and
technologies above, companies below) and comes out as ranked companies, each
with the paths that explain the ranking. It will be served over MCP.

- Full design: `docs/PLAN.md`
- Current phase: **Phase 4, evaluation**. Work from `docs/phase-4.md`, one milestone per session, and check off tasks there as they finish. Phases 0 to 3 are built, with reviews in `docs/phase-0-review.md`, `docs/phase-1-review.md` and `docs/phase-2-review.md`; their decisions still apply. Phase 3 is complete except what waits on the GDELT company series (M34: query precision, the novelty read, the attention check) and `docs/phase-3-review.md`; those are carried. Phase 4 is a backward test for now (D131); its M40 study needs the company series for attention, and M37 and M38 need no coverage data.

## Commands

```bash
uv run pytest                 # tests
uv run ruff check . && uv run ruff format .
uv run ripple validate data/seed
uv run ripple load               # every source in data/sources.yaml, validated together
uv run ripple exposed theme/ai-compute-demand --top 20
uv run ripple explain theme/ai-compute-demand company/asml
uv run ripple sensitivity theme/ai-compute-demand --trials 200 --jitter 0.5
uv run ripple signals fetch --all --from 2022-01-01 --to 2026-09-27   # resume by rerunning; keep --to fixed (D117)
uv run ripple signals show theme/cpo-adoption --days 30
uv run ripple trending --window 90            # themes whose coverage burst recently
uv run ripple attention --theme theme/ai-compute-demand
uv run ripple exposed theme/ai-compute-demand --by-novelty --top 15
uv run ripple events                          # pre-registered event set vs detected bursts
uv run ripple exposed product/hbm --top 10    # a product or material shock, not only a theme (D118)
uv run ripple evidence e-1a2b3c4d5e           # one edge: evidence, source layer, losing rows
uv run ripple profile company/vertiv          # revenue mix, neighbours, exposure per theme
uv run ripple verify status                   # evidence rulings; also next, rule, walk, recompute, links
uv run ripple check-brief docs/briefs/ai-compute-demand.md --theme theme/ai-compute-demand
uv run ripple brief theme/ai-compute-demand   # Codex writes a brief through the MCP tools only
uv run ripple prices fetch --from 2022-01-01   # daily adjusted closes, 61 listings + 5 indices (M36)
uv run ripple prices show NVDA
uv run ripple ui --port 8765  # read-only browser UI at http://127.0.0.1:8765 (docs/ui.md)
uv run ripple-mcp             # MCP server over stdio (registered in .mcp.json; RIPPLE_DB overrides the store path)
```

## Layout

```
data/seed/            hand-written YAML: nodes and edges with evidence
ripple/
  model.py            pydantic models: Node, Edge, Evidence
  load.py             YAML loader
  validate.py         validation rules
  store.py            DuckDB bitemporal store, snapshot(as_of)
  graph.py            build_graph: snapshot -> node index + sparse W
  propagate.py        propagate(W, shock, hops)
  paths.py            top_paths, min_hops
  score.py            score(): exposure, paths, flags; sensitivity()
  search.py           fuzzy node search used by search_entities
  cli.py              typer CLI
  mcp_server.py       MCP server: the six PLAN §10 tools, one envelope (M30)
  edgar.py            EDGAR client, filing cache, universe file (Phase 1, M10)
  xbrl.py             revenue facts and breakdowns from XBRL instances (M11)
  codex.py            Codex runner: codex exec, schema output, tools disabled (M13)
  jobs.py             resumable Codex batches: jobs table, concurrency 4, quota stop (M16)
  sections.py         filing text and item sections with offsets; fallback chunks (M17)
  growth.py           universe growth, one hop: companies named in filings (M18)
  judge.py            LLM judge in batches of 20; precision; product passages (M18, M20)
  extract.py          text extraction with span verification; REQUIRES edges (M19, M20)
  mapping.py          revenue lines to products, review queue (M13)
  xbrl_edges.py       region (EXPOSED_TO) and customer (SUPPLIES) edges from XBRL (M14)
  news.py             GDELT DOC 2.0 client, response cache, 12s pacing (Phase 2, M24)
  signal.py           coverage series -> baseline, Poisson surprise, bursts, tone flag (M25)
  queries.py          per-theme GDELT query precision, judged by Codex (M26)
  attention.py        company coverage share, percentiles, novelty (M27)
  events.py           pre-registered event set vs detected bursts (M29)
  explain.py          paths and edges with evidence as records; explain_link, get_evidence (Phase 3, M31)
  profile.py          company_profile: revenue mix, neighbours, exposure per theme (M32)
  verify.py           evidence rulings ledger, applied at load; xbrl recompute (M33)
  brief.py            brief citation checker; Codex briefs through the MCP tools (M35)
  prices.py           daily prices from Yahoo's chart endpoint, cache, market index per listing (Phase 4, M36)
  ui/                 read-only browser UI: starlette JSON routes, static page (docs/ui.md, off-roadmap)
tests/
  fixtures/mini/      small graph with hand-computed answers (phase-0.md, M2)
  golden/             ranking expectations for the real seed (write before looking at a ranking)
  golden/events.yaml  pre-registered signal events; fixed before any coverage was fetched (D101)
  golden/brief-*.     brief rubric and prompts; fixed before any brief is generated (D114)
  fixtures/edgar/     recorded EDGAR responses (trimmed) for offline tests
  fixtures/gdelt/     recorded GDELT responses, including the plain-text rate-limit refusal
  fixtures/prices/    recorded price responses, trimmed: a split, a dividend, Golden Week, errors
data/universe.yaml    companies in scope: node ID, CIK, form (Phase 1)
data/sources.yaml     source name to YAML directory (seed, xbrl, llm, review; D40)
data/raw/             filing cache, gitignored
data/prices/          price response cache, gitignored
data/xbrl/            xbrl source: generated by `ripple review accept`, do not edit
data/review-queue/    Codex proposals awaiting review
```

Do not create modules for later phases (magnitude, graph completion) yet. Add Phase 4 modules as their milestone starts, and list them above with their milestone number. Phase 4 adds `prices.py` (M36), `evaluate.py` (M37) and `tests/golden/evaluation.yaml` (M38). The forward ledger (M39) is deferred.

## Conventions

- Python 3.12, uv, ruff, pytest, type hints everywhere, pydantic v2.
- Node IDs are `<type>/<slug>`: `company/asml`, `product/euv-lithography`, `theme/ai-compute-demand`.
- Edges are stored in their natural direction (company PRODUCES product). `graph.py` maps them to the direction demand flows. The mapping table is in `docs/phase-0.md`.
- `weight` always means a share in (0, 1]: revenue share on PRODUCES, SUPPLIES and SUBSIDIARY_OF; demand share on DRIVES and REQUIRES. Sign lives in `polarity`, never in `weight`.
- Every edge records `weight_source`: `bucket`, `filing` (company or official figure) or `manual` (secondary source such as an analyst or press). Never invent a precise number: use a bucket from `docs/phase-0.md` (values in `ripple/model.py`) and mark it `bucket`. Put the arithmetic for a sourced number in the evidence note.
- Every edge needs evidence: URL, access date and a short note in your own words. Never copy article or filing text into the repo.
- The store is bitemporal. Loading never overwrites or deletes; it supersedes. The loader sets `recorded_at`; seed YAML never contains it. Edge identity is (`src`, `rel`, `dst`) and the edge ID is derived from it.
- Scoring code takes an `as_of` date everywhere, even where Phase 0 always passes today.

## Rules

- Tests never touch the network: EDGAR and Codex are replaced by recorded fixtures and stubs.
- EDGAR requests need `RIPPLE_SEC_USER_AGENT` (set in `~/.zshenv`); stay at or under 5 requests per second.
- Propagation math is covered by the fixture in `tests/fixtures/mini/`. If a change moves any expected number there, the change is wrong or the fixture needs a written reason.
- Before writing MCP server code, read the current MCP Python SDK docs at https://py.sdk.modelcontextprotocol.io. v2 is the current stable line and differs from v1-era FastMCP examples.
- Tests never touch the price source either: recorded responses live in `tests/fixtures/prices/`. Stay at 1 request per 1.5 s; the source is unofficial (D128), so keep volume at research scale.
- Tests never touch GDELT either: recorded responses live in `tests/fixtures/gdelt/`. Stay at 1 GDELT request per 5 seconds with a descriptive User-Agent.
- Theme queries are read from the **store**, not from `data/seed/nodes/themes.yaml`. Run `ripple load` after editing a query, or the signals commands keep using the old one. GDELT's rate limit is per IP and this machine shares its address, so a wasted request is unrecoverable for a while; check the store's query before trusting a signals result.
- A burst is a unit of surprise and exposure is per unit of demand shock. Never multiply them, and never turn a coverage change into a percentage demand change (D88).
- Record design decisions in the Decisions section of the current phase doc (`docs/phase-4.md`); earlier phases' decisions stay where they were written.
