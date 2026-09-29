# Ripple

Ripple ranks public companies by their exposure to trends, and shows why.

A trend is a shock on a **theme**, for example AI compute demand or liquid-cooling adoption. The
shock travels through a two-layer graph. The upper layer holds products and technologies (AI
accelerators require leading-edge logic, which requires EUV lithography), and the lower layer
holds the companies that make them (ASML produces EUV scanners). Every edge carries a share, so
the effect shrinks at each step. What comes out is a ranked list of companies, each with an
**exposure** (revenue impact per unit of shock) and the paths that explain it, down to the
sources behind every edge.

On top of that, Ripple watches news coverage for **bursts** that say a theme is moving, and
computes **novelty**: exposure discounted by how much attention a company already gets, so real
exposure the news has not caught up with stands out.

It runs as a command-line tool, an MCP server for language models, and a small read-only
browser UI.

```
$ uv run ripple exposed theme/ai-compute-demand --top 5      # abridged
 #  Company               Exposure  Hops  Top path (via)
 1  Astera Labs, Inc.     +0.6400      3  AI servers → PCIe and CXL connectivity
 2  Nvidia                +0.5148      2  AI servers → AI accelerators
 3  Fabrinet              +0.4800      3  AI servers → Optical transceivers
 4  Super Micro Computer  +0.4800      2  AI servers
 5  Coherent              +0.3984      3  AI servers → Optical transceivers
```

Ripple is a research tool that surfaces and explains leads. It is not investment advice, and it
says so in its own output: bucket-guessed weights, unverified evidence and unknown attention are
flagged rather than hidden.

## How it works

- **The graph.** 61 companies, 47 products and materials, and 15 themes, stored in a bitemporal
  DuckDB store, which records both when a fact was true and when Ripple learned it. Edges come
  from three layers:
  - hand-written seed YAML with a cited source for every edge;
  - XBRL revenue breakdowns from SEC 10-K and 20-F filings;
  - LLM-extracted value-chain links, each verified against a span of the filing.

  Each edge records whether its weight is a filing figure, a sourced estimate or a bucket
  guess.
- **Propagation.** A sparse matrix pushes a unit shock along the direction demand flows for up to
  five hops. Explanation paths come from Yen's k-shortest paths on −log|weight|.
- **Signals.** Daily coverage counts from the GDELT DOC 2.0 API, turned into bursts with a
  Poisson surprise statistic, an overdispersion correction and a threshold calibrated from the
  coverage alone.
- **Attention and novelty.** A company's share of all coverage over 90 days, ranked within each
  theme's exposed set.
- **Evaluation.** Daily prices, USD buy-and-hold returns against the universe, and a
  pre-registered backward test of whether novelty ranks companies better than exposure alone.

The full design is in [`docs/PLAN.md`](docs/PLAN.md). Each phase has its own plan with decisions
(`docs/phase-N.md`) and a review of what was measured (`docs/phase-N-review.md`).

## Getting started

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ripple load                  # build data/ripple.duckdb from the YAML sources in data/
uv run ripple exposed theme/ai-compute-demand --top 20
uv run ripple explain theme/ai-compute-demand company/asml
uv run ripple profile company/vertiv
```

The graph comes from files in the repository, so `ripple load` is all it needs. Coverage and
prices are fetched from outside and cached locally (not committed):

```bash
export RIPPLE_SEC_USER_AGENT="Your Name you@example.com"   # SEC and GDELT ask for a contact
uv run ripple signals fetch --all --from 2022-01-01 --to 2026-09-27   # GDELT; rerun to resume
uv run ripple prices fetch --from 2022-01-01                          # daily adjusted closes
```

GDELT limits requests per IP address. On a shared address most requests are refused, and the
fetch is built to stop cleanly and resume from its cache on the next run.

### Other commands

```bash
uv run ripple trending --window 90                         # themes whose coverage burst recently
uv run ripple exposed theme/ai-compute-demand --by-novelty # exposure the news hasn't priced in
uv run ripple exposed theme/ai-compute-demand --priced-in  # does the price already move with it?
uv run ripple exposed product/hbm --top 10                 # shock a product instead of a theme
uv run ripple evidence e-2a18e29ae7                        # one edge: sources, layer, losing rows
uv run ripple events                                       # pre-registered events vs bursts
uv run ripple verify status                                # human rulings on the evidence
uv run ripple ui                                           # read-only UI at http://127.0.0.1:8765
uv run ripple --help
```

## Using it from a language model

`ripple-mcp` is an MCP server over stdio, already registered in [`.mcp.json`](.mcp.json) for
Claude Code. Every tool is read-only and returns `as_of`, confidence and notes on how to read the
result.

| Tool | What it answers |
|---|---|
| `search_entities` | Which node ID does "ASML" or "AI compute" mean? |
| `find_exposed` | Which companies are exposed to this theme or product, how much, and through what? |
| `explain_link` | What are the strongest demand paths between two nodes? |
| `get_evidence` | What documents stand behind this edge, and has a person verified them? |
| `company_profile` | What does this company sell, to whom, and which themes reach it? |
| `trending_themes` | Which themes' news coverage burst recently? |

Set `RIPPLE_DB` to point the server at a different store. In the Phase 3 acceptance test, a model
given only these tools wrote three thematic briefs ([`docs/briefs/`](docs/briefs/)), and every
node ID, edge ID, URL and exposure number they cite resolves against the store.

## Status

| Phase | What it built | State |
|---|---|---|
| 0 | Hand-built slice, propagation, paths, CLI, MCP | done |
| 1 | Structure from SEC filings: XBRL revenue lines, text extraction, universe growth | done; universe at 61 companies against a target of about 150 |
| 2 | News signals: GDELT coverage, bursts, attention and novelty | built; event hit rate 60% (75% on exact dates) against an 80% bar, not met |
| 3 | The full MCP surface, evidence verification, Codex briefs | done except work waiting on company coverage |
| 4 | Evaluation: prices, returns, a pre-registered backward test | prices, returns, pre-registration and the priced-in check done; the study waits on company coverage |

Being honest about what is measured matters here:

- **Event test.** Detected bursts were checked against 15 events written down before any
  coverage was fetched. The pre-registered event test hit 9 of 15. The burst threshold was
  calibrated blind to those events and committed before the test ran.
- **Graph evidence.** All 432 evidence items were ruled on by a person, 114 of them approved in
  bulk.
- **Guessed weights.** Most value-chain weights (89% of DRIVES and 91% of REQUIRES edges) are
  still bucket guesses, and Ripple labels them as such everywhere.
- **The backward test.** It uses today's graph and a universe chosen in 2026, so its result will
  be stated as biased. Its analysis plan is in
  [`tests/golden/evaluation.yaml`](tests/golden/evaluation.yaml) and was committed before any
  burst was scored.

## Development

```bash
uv run pytest                        # about 500 tests, all offline
uv run ruff check . && uv run ruff format .
uv run ripple validate data/seed
```

Tests never touch the network: EDGAR, GDELT, the price source and Codex are replaced by recorded
fixtures and stubs. The propagation math is pinned by a small graph with hand-computed answers in
`tests/fixtures/mini/`.

Working conventions for contributors, including how edges are sourced and weighted, are in
[`CLAUDE.md`](CLAUDE.md).

## Layout

```
ripple/            the library, CLI (cli.py), MCP server (mcp_server.py) and UI (ui/)
data/seed/         hand-written nodes and edges with evidence
data/xbrl/         edges generated from SEC XBRL filings
data/llm/          edges extracted from filing text
data/verified.yaml human rulings on evidence
docs/              the plan, per-phase plans and reviews, the UI notes, the briefs
tests/             offline tests, fixtures, and pre-registered golden files
```
