# Phase 0: Hand-built slice

## Goal

Prove the core loop before any NLP exists. A theme shock propagates through a
hand-built graph and comes out as a ranked list of companies, each with a path
that explains why, available from a CLI and from an MCP tool.

If the ranking does not make sense on 150 edges you chose yourself, it will not
make sense on a million extracted ones. Everything built here (schema, store,
propagation, paths, CLI, MCP wrapper) carries into later phases unchanged. Only
the source of the data changes.

**Size:** a weekend or two. Each milestone is sized for one Claude Code session.
Work through them in order, check off tasks in this file as they finish, and
record decisions in the Decisions section at the bottom.

## Scope

**In scope**

- Seed data format (YAML) and a validator
- Bitemporal DuckDB store and snapshots at a date
- Graph build and propagation
- Top-k explanation paths
- CLI: `validate`, `load`, `exposed`, `explain`, `sensitivity`
- MCP server with two tools: `search_entities` and `find_exposed`
- Seed data in two stages (D15): a small Stage A that runs the whole review loop, then Stage B at full size
- Tests, a sensitivity check, and written review notes

**Out of scope** (later phases)

- NLP, EDGAR or news ingestion, price data, backtests
- The attention metric (Phase 0 uses a hop-count placeholder)
- Magnitude beyond bucketed weights
- Price effects of supply shocks (D14) and regional concentration (D16)

## The slice: AI datacenter build-out

Full target: about 60 product nodes, 30 companies, 150 edges, 4 themes. It is
built in two stages.

| Stage | Companies | Edges | Themes | Product groups |
|---|---|---|---|---|
| A | ~20 | ~80 | `ai-compute-demand`, `liquid-cooling-adoption` | Compute, Memory, Foundry and packaging, Equipment, Materials, Datacenter physical (capacity, cooling, switchgear, UPS) |
| B | ~30 | ~150 | adds `datacenter-power-demand`, `custom-silicon-adoption` | adds Networking and Power, plus the rest of Datacenter physical |

### Product groups

| Group | Example nodes |
|---|---|
| Compute | AI servers, AI accelerators, custom AI ASICs, server CPUs |
| Memory | HBM, DRAM, enterprise SSDs |
| Foundry and packaging | leading-edge logic wafers, advanced 2.5D packaging, ABF substrates, ABF film |
| Semiconductor equipment | EUV lithography, DUV lithography, deposition, etch, inspection and metrology |
| Materials | silicon wafers, photoresist, specialty gases, copper |
| Networking | datacenter switches, optical transceivers, lasers, fiber cable, copper interconnect |
| Datacenter physical | datacenter capacity, switchgear, UPS, transformers, backup generators, liquid cooling, air cooling, chillers |
| Power | electricity demand, gas turbines, nuclear generation, transmission equipment |

### Companies

Include a few that are known for something else, because they are the test of
whether the tool finds non-obvious links. ABF film is a good example: it is a
small part of a large company whose main business is not semiconductors.

| Group | Stage A | Stage B adds |
|---|---|---|
| Compute | Nvidia, AMD, Broadcom, Marvell, Super Micro, Dell | |
| Memory | SK hynix, Micron, Samsung Electronics | |
| Foundry, packaging, substrates | TSMC, Ibiden, Ajinomoto | |
| Equipment | ASML, Applied Materials, Lam Research, KLA, Tokyo Electron | |
| Datacenter physical | Vertiv, Schneider Electric, Eaton | |
| Networking and optics | | Arista, Coherent, Lumentum, Corning, Amphenol |
| Power | | GE Vernova, Siemens Energy, Constellation Energy |

Several of these do not file with the SEC. Use their annual reports and IR pages
as evidence.

Stage A also needs at least one company whose cooling revenue is mostly air
cooling, so that `liquid-cooling-adoption` has a real loser. If none can be
sourced, the negative side is tested only by the fixture, and the review says so.

### Themes

Every theme has a `kind` (D7, D8):

- **volume**: the theme creates or removes demand. It DRIVES only nodes at the
  top of the value chain, and `REQUIRES` edges carry the shock down. If it also
  drove an input directly, that input would receive the same demand twice.
- **share_shift**: the theme moves existing demand from one node to another. It
  may DRIVE any node, and it needs at least one positive and one negative
  `DRIVES` edge.

| ID | Kind | Stage | Drives | What it tests |
|---|---|---|---|---|
| `theme/ai-compute-demand` | volume | A | AI servers, datacenter capacity | Deep chains (to equipment and materials) |
| `theme/liquid-cooling-adoption` | share_shift | A | liquid cooling (+), air cooling (−) | Negative polarity and share shift (D3) |
| `theme/datacenter-power-demand` | volume | B | electricity demand only (D28) | A second volume theme that shares nodes with the first |
| `theme/custom-silicon-adoption` | share_shift | B | custom AI ASICs (+), AI accelerators (−) | A share shift inside the compute layer (D14) |

Modeling notes for the seed:

- `ai-compute-demand` reaches accelerators, custom ASICs, CPUs and HBM through
  `AI servers REQUIRES …`, never by driving them directly.
- Datacenter capacity must not `REQUIRE` AI servers. Otherwise AI servers would
  get the theme's shock both directly and through datacenter capacity, which
  breaks validation rule 10.
- The AI compute theme reaches power through `datacenter capacity REQUIRES
  electricity demand`. There are no theme-to-theme edges (D10).

## Data format

Seed files live in `data/seed/`. Split them however is convenient (for example
`nodes/*.yaml` and `edges/*.yaml` by product group); the loader reads the whole
directory.

### Node

```yaml
- id: product/euv-lithography
  type: product
  label: EUV lithography systems
  aliases: [EUV scanners, extreme ultraviolet lithography]
  group: semicap

- id: company/asml
  type: company
  label: ASML Holding
  aliases: [ASML]
  ticker: ASML
  country: NL
  ids: {cik: null, lei: null, wikidata: null}   # fill later; null is fine in Phase 0

- id: theme/ai-compute-demand
  type: theme
  kind: volume               # volume | share_shift; required on themes
  label: AI compute demand
```

ID rules: `<type>/<slug>`, lowercase, hyphens. Types: `company`, `product`,
`material`, `theme`. A material is stored like a product with `type: material`.

### Edge

```yaml
- src: company/asml
  rel: PRODUCES
  dst: product/euv-lithography
  weight: 0.6
  weight_source: bucket      # bucket | filing | manual
  polarity: 1                # 1 or -1
  confidence: 0.9
  valid_from: 2019-01-01
  valid_to: null
  evidence:
    - url: https://example.com/asml-annual-report
      note: EUV is the largest share of system sales   # your own words, never copied text
      accessed: 2026-09-26
```

An edge is identified by (`src`, `rel`, `dst`). Its ID is derived, never written
by hand: `e-` followed by the first 10 hex characters of
`sha256("src|rel|dst")` (D12). The loader sets `recorded_at` to the time of the
load, so the seed YAML never contains it (D13). Set `valid_from` to the date the
evidence supports. If you don't know it, use the evidence date.

### Weight buckets

Use a bucket unless a source gives a number. When a company or official report gives
the number, use it and set `weight_source: filing`. When a secondary source (an
analyst, a research firm or the press) gives it, set `weight_source: manual` (D33).
Put the arithmetic in the evidence note. The bucket values live in `ripple/model.py`
(`BUCKETS`), and validator warning W8 flags a `bucket` weight that is not one of them.

| Quantity | Used on | Buckets |
|---|---|---|
| Revenue share | `PRODUCES`, `SUBSIDIARY_OF`, `SUPPLIES` | pure 0.9 (over about 75% of revenue), core 0.6, meaningful 0.25, minor 0.05 |
| Demand share | `DRIVES`, `REQUIRES` | dominant 0.8, major 0.4, minor 0.1 |
| Share moved | `DRIVES` from a share_shift theme | fraction of the node's demand that moves per unit shock (D8); same buckets as demand share |
| Confidence | all | 0.9 filing or company IR, 0.7 reputable press, 0.5 an inference you would defend, 0.3 speculative (stored, not propagated) |

### Edge types and demand flow

Edges are stored in their natural direction. The graph builder maps each one to
the direction demand flows.

| Stored | Direction in the demand-flow matrix | Transfer quantity |
|---|---|---|
| theme `DRIVES` product | theme → product | share of the product's demand from the theme |
| product `REQUIRES` input | product → input | share of the input's demand from this product |
| company `PRODUCES` product | product → company | share of the company's revenue from the product |
| supplier `SUPPLIES` customer | customer → supplier | share of the supplier's revenue from the customer |
| company `SUBSIDIARY_OF` parent | company → parent | subsidiary's share of parent revenue |
| product `SUBSTITUTES` product | not traversed in Phase 0 (D3) | |
| `COMPETES_WITH`, `EXPOSED_TO`, `CONSTRAINS` | stored, not traversed in Phase 0 | |

Matrix entry: `W[i, j] = weight × polarity`. Edges with `confidence` below
`min_confidence` (default 0.5) are left out of the matrix, the paths and
`min_hops`. Confidence is reported per path, never multiplied into `W` (D11).

## Validation rules

`ripple validate data/seed` reports errors (fail) and warnings (print only).

Errors:

1. IDs are unique and the prefix matches `type`.
2. Every edge endpoint exists.
3. The relation is allowed for the endpoint types (for example `PRODUCES` is company → product or material).
4. `weight` and `confidence` are in (0, 1]; `polarity` is 1 or −1.
5. Every edge has at least one evidence item with `url` and `accessed`.
6. Per company, `PRODUCES` weights sum to at most 1.0.
7. Per input, incoming `REQUIRES` weights sum to at most 1.0.
8. No two edges share (`src`, `rel`, `dst`) with overlapping valid time.
9. `valid_to`, when set, is after `valid_from`.
10. A volume theme drives the same demand twice: one of its `DRIVES` targets can be reached through `REQUIRES` from another of its `DRIVES` targets (D7).
11. A share_shift theme lacks a positive or a negative `DRIVES` edge (D8).
12. Two edges map to the same demand-flow cell (i, j), for example X `SUBSIDIARY_OF` P together with P `SUPPLIES` X. scipy would add them silently and networkx would keep only one.
13. A theme has no `kind`, or its `kind` is not `volume` or `share_shift`.

Warnings:

- A product has no producer.
- A company has no `PRODUCES` edge.
- A node cannot be reached from any theme.
- A company can be reached only through edges below `min_confidence`.
- `DRIVES` weights from different themes into one product sum to more than 1.0. This is allowed, because themes are separate lenses, but exposures from different themes must not be added together (D9).
- An evidence `accessed` date is in the future.
- An evidence note is longer than 200 characters (a sign it was copied).
- A `bucket`-sourced weight is not one of the bucket values for its relation (W8).

## Milestones

### M0. Repository setup

- [x] `git init`, with a `.gitignore` covering `*.duckdb`, `.venv/`, `__pycache__/`, `.pytest_cache/` and `.ruff_cache/`
- [x] `uv init`, Python 3.12, package `ripple` with a `ripple` console script
- [x] Dependencies: pydantic, pyyaml, duckdb, numpy, scipy, networkx, typer, rich, mcp
- [x] Dev dependencies: pytest, ruff
- [x] Only create the modules Phase 0 needs (see CLAUDE.md for the layout)

**Done when** `uv run pytest`, `uv run ruff check` and `uv run ripple --help` all succeed.

### M1. Schema and validator

- [x] Pydantic models: `Node`, `Edge`, `Evidence`. `Edge.id` is a derived property (D12). `Node.kind` is required when `type` is `theme`.
- [x] Loader that reads every YAML file under a directory
- [x] All validation rules above, with file name and record ID in each message
- [x] `ripple validate <dir>`

**Done when** each error rule, including 10 to 13, has a test with a fixture that breaks it.

### M2. Test fixture with known answers

Create `tests/fixtures/mini/` with this graph. The evidence URL can be a
placeholder. The expected values below are exact.

Themes: `theme/ai-compute` has `kind: volume` and `theme/cooling-shift` has
`kind: share_shift`.

| Edge | Weight | Polarity | Confidence |
|---|---|---|---|
| `theme/ai-compute` DRIVES `product/accelerators` | 1.0 | 1 | 1.0 |
| `theme/ai-compute` DRIVES `product/dc-capacity` | 0.5 | 1 | 1.0 |
| `product/accelerators` REQUIRES `product/leading-edge-logic` | 0.4 | 1 | 1.0 |
| `product/leading-edge-logic` REQUIRES `product/euv` | 0.8 | 1 | 1.0 |
| `product/dc-capacity` REQUIRES `product/liquid-cooling` | 0.9 | 1 | 1.0 |
| `company/nvidia` PRODUCES `product/accelerators` | 0.9 | 1 | 1.0 |
| `company/tsmc` PRODUCES `product/leading-edge-logic` | 0.6 | 1 | 1.0 |
| `company/asml` PRODUCES `product/euv` | 0.5 | 1 | 1.0 |
| `company/vertiv` PRODUCES `product/liquid-cooling` | 0.3 | 1 | 1.0 |
| `company/multi` PRODUCES `product/accelerators` | 0.1 | 1 | 1.0 |
| `company/multi` PRODUCES `product/euv` | 0.2 | 1 | 1.0 |
| `company/parts` SUPPLIES `company/nvidia` | 0.5 | 1 | 1.0 |
| `theme/cooling-shift` DRIVES `product/liquid-cooling` | 0.3 | 1 | 1.0 |
| `theme/cooling-shift` DRIVES `product/air-cooling` | 0.3 | −1 | 1.0 |
| `company/aircool` PRODUCES `product/air-cooling` | 0.7 | 1 | 1.0 |
| `company/rumor` PRODUCES `product/accelerators` | 0.5 | 1 | 0.3 |

Expected exposure for a shock of 1.0 on `theme/ai-compute`, `max_hops=5`,
default `min_confidence=0.5`:

| Company | Exposure | Min hops | Arithmetic |
|---|---|---|---|
| nvidia | 0.900 | 2 | 1.0 × 0.9 |
| parts | 0.450 | 3 | 0.9 × 0.5 |
| tsmc | 0.240 | 3 | 1.0 × 0.4 × 0.6 |
| multi | 0.164 | 2 | 1.0 × 0.1 + (1.0 × 0.4 × 0.8) × 0.2 |
| asml | 0.160 | 4 | 1.0 × 0.4 × 0.8 × 0.5 |
| vertiv | 0.135 | 3 | 0.5 × 0.9 × 0.3 |
| rumor | 0 | none | its only edge is below `min_confidence` |

More expected results:

- With `max_hops=3`, asml is 0 (its path has 4 edges) and multi is 0.100.
- With `min_confidence=0`, rumor is 0.500 (1.0 × 0.5) and every other number is unchanged.
- A shock of 1.0 on `theme/cooling-shift` gives aircool −0.210 and vertiv +0.090.
- Summing the contributions of every path from the theme to a company equals its exposure (to 1e-9). This holds because the fixture has no cycles.
- A cycle test: add `company/a` SUPPLIES `company/b` and `company/b` SUPPLIES `company/a`, both 0.5, and check that propagation stays finite and matches the truncated sum.
- The fixture passes validation with three expected warnings: rumor is reachable only through a low-confidence edge (W4), nothing produces `dc-capacity` (W1), and `parts` only supplies Nvidia (W2).

Every confidence except rumor's is 1.0, so taking confidence out of `W` (D11)
changes none of the other numbers.

### M3. Store and snapshots

- [x] DuckDB tables `nodes`, `edges` and `evidence`. Edges are keyed by (`src`, `rel`, `dst`) plus a version. Nodes and edges both carry `recorded_at` and `superseded_at`, and edges also carry `valid_from` and `valid_to`. Evidence rows belong to one edge version.
- [x] `ripple load <dir> [--db data/ripple.duckdb]`. The loader sets `recorded_at` to the load time and takes a `now` parameter so tests can control it (D13).
- [x] Loading never overwrites or deletes:
  - A changed edge or node closes the old row (`superseded_at` = load time) and inserts a new one.
  - An edge or node that is no longer in the directory has its row closed, with no replacement.
  - Reloading unchanged files inserts nothing.
- [x] `snapshot(as_of)` returns rows where `valid_from <= as_of` and `valid_to` is null or after `as_of`. Rows must also satisfy `recorded_at <= T` and have `superseded_at` null or after `T`, where T is the end of the `as_of` day in UTC.

**Done when** tests cover:

- an edge with a future `valid_from` is excluded
- an edge recorded after `as_of` is excluded and appears for a later date
- a reload with one changed weight shows the old weight before the reload time and the new one after
- an edge removed from the YAML is present before the reload time and gone after it
- reloading an unchanged directory leaves every row count the same

### M4. Graph build and propagation

- [x] `build_graph(snapshot, min_confidence=0.5)` returns a node index, the sparse matrix `W`, and edge metadata keyed by matrix position. `W` holds `weight × polarity` only. Assert that no two edges map to one cell (rule 12 should already have caught it).
- [x] `propagate(W, shock, hops)` as sketched in PLAN.md
- [x] A shock is (theme ID, magnitude, direction); `down` flips the sign

**Done when** every number in the M2 tables matches to 1e-9.

### M5. Explanation paths

- [x] `top_paths(graph, source, target, k=3, max_hops=5)` using `networkx.shortest_simple_paths` on −log |w| (Yen's algorithm), returning each path's signed contribution, its edge IDs and the product of its confidences
- [x] `min_hops` per company by breadth-first search from the theme
- [x] Both use only edges at or above `min_confidence`

**Done when** the path-sum invariant from M2 passes and asml's top path is the 4-edge chain.

### M6. Scoring and CLI

- [x] `score(theme, direction, as_of, max_hops, hide_obvious, limit, min_confidence=0.5)` returns, per company: exposure, min hops, top paths, the number of bucket-sourced weights on the top path, and the product of confidences on the top path
- [x] `hide_obvious` hides companies with `min_hops <= obvious_hops` (default 3, D23). This placeholder is replaced by the attention metric in Phase 2.
- [x] `ripple exposed <theme> [--direction up|down] [--top 20] [--hide-obvious] [--as-of DATE] [--min-confidence 0.5] [--json]`
- [x] `ripple explain <theme> <company>` prints paths like `AI compute ↑ → AI servers (0.8) → accelerators (0.4) → leading-edge logic (0.8) → EUV → ASML (rev 0.5)`

**Done when** the table is readable and `--json` output matches the MCP result shape below.

### M7. MCP server

- [x] Before writing server code, read the current MCP Python SDK docs (py.sdk.modelcontextprotocol.io). `pip install mcp` now installs v2, and many v1-era examples online no longer match it.
- [x] Tools:
  - `search_entities(query, type=None, limit=10)`: fuzzy match on label, aliases and ticker; returns IDs, labels, types and tickers
  - `find_exposed(theme_id, direction="up", max_hops=5, hide_obvious=False, as_of=None, limit=20, min_confidence=0.5)`
- [x] stdio transport; a `ripple-mcp` console script
- [x] Tool descriptions tell the calling model three things: resolve names with `search_entities` first; exposure is revenue impact per unit of shock; exposures from different themes must not be added together
- [x] Register it with Claude Code: project scope, in `.mcp.json` (`claude mcp add --scope project ripple -- uv run ripple-mcp`)
- [ ] Try it with the MCP Inspector. Not done, because it is interactive. A real stdio client session and a headless Claude Code session covered the same ground.

Result shape for `find_exposed` (numbers are illustrative; edge IDs are the real
derived IDs for these keys):

```json
{
  "theme": "theme/ai-compute-demand",
  "theme_kind": "volume",
  "direction": "up",
  "as_of": "2026-09-26",
  "min_confidence": 0.5,
  "results": [
    {
      "company": "company/asml",
      "label": "ASML Holding",
      "ticker": "ASML",
      "exposure": 0.13,
      "min_hops": 5,
      "guessed_weights": 3,
      "path_confidence": 0.73,
      "weakest_confidence": 0.7,
      "paths": [
        {
          "contribution": 0.13,
          "confidence": 0.73,
          "weakest_confidence": 0.7,
          "nodes": ["theme/ai-compute-demand", "product/ai-servers", "product/ai-accelerators",
                    "product/leading-edge-logic", "product/euv-lithography", "company/asml"],
          "edges": ["e-932732349f", "e-15e8a2a588", "e-eae14fe1d4", "e-478f94e58d", "e-b84ff709f5"]
        }
      ]
    }
  ],
  "notes": "Exposure is the fractional revenue impact per unit of theme shock. Guessed weights are buckets, not measured values. Do not add exposures across themes."
}
```

**Done when** a Claude Code session asked "Which companies are exposed to AI compute demand that aren't the obvious ones? Show the paths." calls `search_entities`, then `find_exposed`, and cites the paths in its answer.

### M8a. Seed data, Stage A

Can run in parallel with M3 to M7 once the validator exists.

- [x] Grow from the fixture to the Stage A slice, one product group at a time
- [x] For every edge: find a source, write the note in your own words, choose a bucket or a sourced number, run `ripple validate`
- [x] Find a mostly-air-cooling company for `liquid-cooling-adoption`, or record that none could be sourced. Vertiv itself: its air bucket (0.25) outweighs its liquid bucket (0.05), so it comes out slightly negative (see the review).
- [x] Keep a `data/seed/TODO.md` of edges you believe exist but have not sourced yet, and of speculative (0.3) edges you would like to confirm
- [x] Run M9 on Stage A before starting M8b

Claude Code can draft candidate edges and find sources, but check each source
yourself before the edge goes in. A wrong edge here teaches you the wrong lesson
about the model.

### M8b. Seed data, Stage B

- [x] Add the Networking and Power groups and the Stage B companies
- [x] Add `datacenter-power-demand` and `custom-silicon-adoption`
- [x] Run M9 again and update the review

### M9. Sensitivity check and review

- [x] One golden file per theme under `tests/golden/`, for example `ai_compute.yaml` and `liquid_cooling.yaml` for Stage A. Each lists companies you expect in the top 10 and a few you expect near the bottom (or, for share_shift themes, with negative exposure). A test fails when the ranking violates them. Write each file before you look at the ranking.
- [x] `ripple sensitivity <theme> --trials 200 --jitter 0.5`: multiply each bucket-sourced weight by a random factor in [0.5, 1.5] and rerun. Report the median Spearman correlation of the top 10 against the baseline, the median Kendall tau over all companies with nonzero exposure, and the five least stable companies (D17).
- [x] `docs/phase-0-review.md`: what looked right, what looked wrong, which edge types were missing, and the schema changes Phase 1 needs (including regional concentration, D16). Write it after Stage A and update it after Stage B.

## Exit criteria

- Every company with nonzero exposure to each theme has a readable path.
- You agree with the ranking, or you know which edges to fix.
- Under jitter, the median top-10 Spearman correlation is at least 0.8, or the review explains why not.
- All tests pass.

## Decisions

Add to this list as you go.

- **D1.** Edges are stored in their natural direction and mapped to demand flow when the graph is built.
- **D2.** Growth rates propagate linearly. Hops count edges. The cap is 5.
- **D3.** `SUBSTITUTES` edges are not traversed by volume shocks. If total datacenter capacity grows, liquid and air cooling can both grow, so a substitute edge would wrongly turn growth into a loss. Substitution is modeled as a share-shift theme with a positive `DRIVES` edge to the winner and a negative one to the loser. This refines the diagram in PLAN.md.
- **D4.** Do not add a company-level `SUPPLIES` edge for a flow the product layer already captures. Both edges would count the same demand twice.
- **D5.** `hide_obvious` uses a hop-count threshold until the attention metric exists (was `min_hops <= 2`, now 3 by default, D23).
- **D6.** The store is bitemporal from the start, and loading never overwrites.
- **D7.** Themes have `kind: volume | share_shift`. A volume theme DRIVES only nodes at the top of the chain. The validator computes this: it is an error if one of a volume theme's `DRIVES` targets can be reached through `REQUIRES` from another. No `final` flag on products.
- **D8.** A share_shift theme may DRIVE any node and needs at least one positive and one negative `DRIVES` edge. Its weight is the fraction of the node's demand that moves per unit shock. Phase 0 does not check that winners' gains balance losers' losses in absolute volume.
- **D9.** `DRIVES` shares from different themes may sum above 1. Themes are separate lenses queried one at a time, so exposures from different themes must not be added together.
- **D10.** No theme-to-theme edges. AI compute reaches power through `datacenter capacity REQUIRES electricity demand`. `datacenter-power-demand` covers news that is specifically about power.
- **D11.** Confidence is not in `W`. `W = weight × polarity`, edges below `min_confidence` (default 0.5) do not propagate, and confidence is reported per path. A speculative 0.3 bucket is stored but not propagated. This matches PLAN §6, where low-confidence edges stay candidates.
- **D12.** An edge is identified by (`src`, `rel`, `dst`). Its ID is `e-` plus the first 10 hex characters of `sha256("src|rel|dst")`, derived and never written by hand.
- **D13.** The loader sets `recorded_at`, and the seed YAML never contains it. A hand-written `recorded_at` combined with a system-set `superseded_at` let two versions of one edge be visible at the same time. If an edge or node disappears from the directory, its row is closed and nothing is deleted. Nodes are versioned the same way as edges.
- **D14.** `theme/hbm-supply-tightness` is replaced by `theme/custom-silicon-adoption` (share_shift: custom AI ASICs +, AI accelerators −). Supply tightness mostly moves prices, which linear volume propagation cannot represent. Price effects wait for Phase 5.
- **D15.** Seed data comes in two stages. Stage A has about 20 companies, 80 edges and two themes (`ai-compute-demand`, `liquid-cooling-adoption`) and runs the full review loop. Stage B grows it to the full slice.
- **D16.** Regional concentration stays out of Phase 0. It goes into the Phase 1 schema notes in the review.
- **D17.** Sensitivity is measured with the top-10 Spearman correlation and the all-company Kendall tau. The top 20 of about 30 companies is nearly every company, so it tested almost nothing.
- **D18.** The package lives at `ripple/` in the repo root (uv_build `module-root = ""`), matching the CLAUDE.md layout, not uv's default `src/`. Ruff skips `docs/` because it would otherwise reformat the Python snippets inside the Markdown.
- **D19.** Pydantic checks shape only and rejects unknown fields, so a hand-written `id` or `recorded_at` in the seed is an `E0` error. The numbered rules live in `validate.py`, so every message carries its rule code (`E0`–`E13`, `W1`–`W7`). Fields that a numbered rule checks (`weight`, `polarity`, `kind`, evidence `url`/`accessed`) are loose in the models so the rule reports them.
- **D20.** Rules that depend on which edges coexist (E6, E7, E10, E12, W5) are checked at every `valid_from` date, so consecutive versions of an edge are never summed together. Reachability warnings (W3, W4) ignore valid time.
- **D21.** `EXPOSED_TO` and `CONSTRAINS` fail rule E3 in Phase 0, because they need region and policy nodes, which Phase 0 does not define.
- **D22.** The natural-direction to demand-flow mapping is `model.demand_flow`, shared by the validator and `graph.py`.
- **D23.** `hide_obvious` takes an `obvious_hops` threshold, default 3, in `score`, the CLI (`--obvious-hops`) and MCP. D7 moved volume themes to the top of the chain, so the makers of a theme's first-tier inputs (Nvidia under AI servers) sit 3 hops out, and a threshold of 2 hid only Super Micro and Dell. The mini fixture drives accelerators directly, so its tests pass `obvious_hops=2`.
- **D24.** Rankings sort by absolute exposure, so the losers of a share-shift theme appear next to the winners, with sign.
- **D25.** `snapshot(as_of, known_at=None)`. `known_at` defaults to `as_of`, which is what backtests need. Passing a later `known_at` asks what we know now about an earlier date.
- **D26.** `ripple load` validates first and refuses to load on any error. A stored edge row is identified by its key plus `valid_from`, so the seed may hold consecutive versions of one edge. "Today" is the UTC date everywhere (`model.today_utc`), because load times are UTC and a local date could hide a fresh load.
- **D27.** The MCP server opens the store read-only on each call, so `ripple load` can run while the server is up (DuckDB allows one writer). The database path comes from `RIPPLE_DB` (default `data/ripple.duckdb`). Fuzzy name search lives in the core library (`ripple/search.py`), not in the server.
- **D28.** `datacenter-power-demand` drives only `product/electricity-demand`. Driving transformers or switchgear as well would double count under D7. Transformers are folded into `product/grid-equipment`, because the transformer makers in the slice report them inside their grid segments. Backup generators and chillers are left out because no producer is in the slice (`data/seed/TODO.md`).
- **D29.** Sourced numbers replace buckets where a source gives one: KLA process control 0.9, and data centers' 4.4% of US electricity (LBNL) on both edges into `electricity-demand`.
- **D30.** `tests/golden/ai_compute.yaml` dropped Broadcom from `in_top` after Stage B, with the reason written in the file. The optics companies outrank it on AI revenue share. The review flags this for a human decision.
- **D31.** A `pure 0.9` revenue bucket covers companies with over about 75% of revenue in one product, so pure plays are no longer capped at core 0.6.
- **D32.** Sensitivity redraws each bucket weight log-uniformly inside its bucket's range (geometric midpoints to the neighbouring buckets, capped at a share of 1), because a bucket stands for a range, not a point. This is the exit metric. The old flat ±50% mode is still reported (`--mode flat`), and the output also gives the top-10 set overlap.
- **D33.** `weight_source: filing` means a company or official figure; `manual` means a number from a secondary source (analyst, research firm, press). Both replace buckets; confidence carries the source quality.
- **D34.** Paths report `weakest_confidence` (the lowest edge confidence) next to the product of confidences, which collapses with depth.
- **D35.** Evidence has `verified: bool` (default false), set by a human who has read the source. `ripple validate` prints the count. Adding the field changed every edge's content hash, so the first reload superseded every edge once.
- **D36.** Fast-moving revenue shares are stored as consecutive valid-time versions rather than one blended number (Dell AI servers: 0.22 for FY2026, 0.37 from FY2027).
- **D37.** Golden files may set lower bounds with `at_least`, used for memory makers on custom silicon, whose true effect is close to zero.

## Open questions

- ~~Is the 5-hop cap too short once themes attach at the top of the chain?~~ Resolved on the full seed: raising the cap from 5 to 8 changes no exposure in any theme. Ajinomoto sits exactly at 5.
- Equipment makers respond to changes in capacity additions, not to the level of demand (the bullwhip effect), so linear propagation makes semicap exposure look smoother than it is. Phase 0 accepts this, and the review notes it.
- AI compute still misses the 0.8 top-10 Spearman bar: 0.776 with bucket-range jitter, 0.739 flat, up from 0.72. The top-10 set is stable (9 of 10); the order within it depends on the optics buckets (Coherent, Lumentum, `ai-servers → optical-transceivers`). The next sources to read are Coherent's segment revenue and LightCounting's AI optics share.
- Shock units differ by theme (a unit of `datacenter-power-demand` is about 4.4% of US electricity). A shock scale per theme is proposed for Phase 1.
