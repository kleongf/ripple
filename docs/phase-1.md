# Phase 1: Structure from filings

## Goal

Replace hand-written edges and guessed weights with edges built from SEC filings.
Phase 0 showed that the weakest part of the graph is the weights, not the links: 82% of
them are buckets, and the rankings that move most under jitter depend on them. Filings
contain many of the missing numbers in structured form. Phase 1 therefore has two parts:

- **1a, structured facts.** Revenue by product line, revenue by region, and named major
  customers come from XBRL, the tagged financial data in each filing. An LLM helps only to
  map a company's revenue-line names onto the product taxonomy.
- **1b, text extraction** (refocused after 1a, D65). First, grow the universe to about 150
  companies from the ones the seed filers' own filings name; the new companies get their
  weights through the 1a pipeline. Then an LLM reads Items 1, 1A and 7 for two things XBRL
  cannot give: new value-chain links (`REQUIRES` proposals) and revenue shares the company
  discloses in prose. `PRODUCES` and `SUPPLIES` statements found in text are kept only as
  supporting evidence.

The propagation, scoring, paths, CLI and MCP server from Phase 0 do not change. Only the
source of the edges changes.

**Size:** large (PLAN §12). 1a is a few weeks and 1b a few more. Each milestone is sized for
one Claude Code session. Work in order, tick tasks here, and record decisions at the bottom.

## Scope

**In scope**

- An EDGAR client and a local filing cache
- XBRL revenue facts: product and service lines, business segments, geography and major customers
- Mapping revenue lines to product nodes, plus a review queue for proposals
- Separate sources in the store, with a precedence rule that decides between them
- Region nodes and `EXPOSED_TO` edges, stored but not traversed
- Universe growth to about 150 companies, `REQUIRES` proposals and disclosed numbers from text,
  and precision measured by an LLM judge

**Out of scope** (later phases)

- Companies that do not file with the SEC. They keep their hand-seeded edges, and
  their annual reports are left for a later phase.
- News, signals, themes from text, and backtests (Phases 2 to 4)
- `COMPETES_WITH` and `SUBSIDIARY_OF` extraction
- Traversing region edges, and profit weights
- GLiNER or other local NER models (D49)

## Universe

Start from the SEC filers in the seed: 10-K filers plus the 20-F filers ASML and TSMC (27).
In M18, Codex lists the companies their Items 1 and 1A name as suppliers, customers,
competitors or partners. SEC filers that make at least one taxonomy product are ranked by how
many seed filers name them, and the top ~120 join, for about 150 in total (one hop only, D71).
Non-SEC companies named this way are listed in `data/seed/TODO.md` for later. Non-SEC companies in the seed (SK hynix,
Samsung, Ibiden, Ajinomoto, Tokyo Electron, Shin-Etsu, SUMCO, Tokyo Ohka, Schneider, Siemens
Energy) stay in the graph with their hand-seeded edges.

## Sources and precedence

Every edge row records its `source`:

| Source | Produced by |
|---|---|
| `seed` | `data/seed/` YAML (Phase 0) |
| `xbrl` | 1a: XBRL facts, mapped to products |
| `llm` | 1b: Codex text extraction |
| `review` | accepted or edited in `ripple review` |

Loading is scoped by source. `ripple load data/seed` only closes and supersedes `seed`
rows, so it never removes an extracted edge, and the same holds for the other sources.

When several sources give the same edge key, the graph uses one row, chosen in this order:

1. Any row with human-verified evidence (`verified: true`, D35) or from `review`
2. `xbrl`
3. `seed`
4. `llm`

The other rows are kept and shown by `ripple explain` as alternatives with their evidence.
This is decision (b) from the grilling: evidence decides, and the hand seed is not
automatically right.

**Proposals.** New products and `REQUIRES` links from text wait in the review queue, not in
the graph. The judge plus Claude Code decide on `REQUIRES` links: accepted ones enter the
`llm` source at confidence 0.5 with the Codex bucket, and rejected ones are not stored (D67,
D69). New products are the user's decision. `PRODUCES` and `SUPPLIES` statements from text
are stored in `llm` at confidence 0.3: below the threshold (D11), so they are evidence only
(D65). The `review` source stays reserved for human decisions, because it outranks everything
(D63).

## Codex integration

Codex is both the extractor and the judge, running `gpt-6-astra` through the ChatGPT login
(D46). The probe on 2026-09-26 established:

- `codex exec` runs non-interactively. `--output-schema` constrains the reply to a JSON
  Schema, which needs `additionalProperties: false`, every field required, and nullable
  fields written as `["number", "null"]`.
- `-o FILE` writes the final message, and `--json` streams events that include token usage.
- Lean flags (a read-only sandbox, approvals off, web search and tools disabled, ephemeral,
  user config ignored) cut the fixed overhead to about 5k input tokens per call.
- Four concurrent runs worked, with prompt-prefix caching between them.
- There is no batch discount, and Astra costs 5× Sol per token against the Pro plan
  allowance.

The runner calls `codex exec` as a subprocess with at most 4 concurrent calls. It records the
`codex` version with every call and warns when it differs from the last tested version,
instead of downloading a second pinned binary (about 235 MB) onto the nearly full disk
(D66). Jobs are recorded in DuckDB so that a run stopped by a rate limit resumes where it
left off. Every call logs its token usage, and M19 projects the cost of a full pass before
it runs.

Tests never call Codex or SEC. The runner takes the path of the `codex` executable, and tests
pass a stub script that returns recorded JSON. EDGAR tests use recorded fixtures.

## Validation changes

- E3 allows `EXPOSED_TO` from company to region. Region nodes use `type: region` and IDs
  `region/<iso-3166>`, for example `region/tw` and `region/us`. EDGAR also reports groups
  such as "Asia Pacific"; those get their own `region/<slug>` nodes.
- **W9 (D4 check):** warn on a `SUPPLIES` edge whose customer already reaches the supplier
  through the product layer within 3 hops. Both edges would count the same demand, so the
  loader stores such a `SUPPLIES` edge at confidence 0.3 (not propagated).
- Every `llm` edge's span must appear verbatim in its section text and contain a mention
  or alias of both endpoints. Otherwise the edge is rejected before it is stored
  (PLAN §6).

## Milestones: 1a, structured facts

### M10. EDGAR client and filing cache

- [x] `ripple/edgar.py`: CIK lookup (`company_tickers.json`), the submissions API, and the
      latest 10-K or 20-F per company. Reads `RIPPLE_SEC_USER_AGENT` and refuses to run
      without it. At most 5 requests per second.
- [x] Cache under `data/raw/<cik>/<accession>/` (gitignored): the primary document, the XBRL
      instance and the label linkbase, gzipped, plus `meta.json`. Exhibits, rendered R pages and
      the XBRL zip are skipped (D51).
- [x] `ripple fetch <company-id>... | --universe`
- [x] A universe file `data/universe.yaml` listing node ID, CIK and form type

**Done when** the seed's SEC filers fetch into the cache, and tests run offline against
recorded fixtures.

### M11. XBRL revenue facts

- [x] Spike first: check whether `edgartools` reads dimensional facts (revenue by axis
      member) for both US GAAP and IFRS filings, with Arelle as the fallback. The SEC
      `companyfacts` API does not include dimensional facts, so it is not enough on its own.
      Record the outcome as a decision.
- [x] Extract total revenue, and revenue by `srt:ProductOrServiceAxis`,
      `us-gaap:StatementBusinessSegmentsAxis` and `srt:StatementGeographicalAxis`. Extract
      major customers from `srt:MajorCustomersAxis` with concentration percentages. For 20-F
      filers, use the IFRS equivalents.
- [x] Normalize to shares of total revenue per fiscal year. Keep the fact name, context and
      filing URL for evidence.

**Done when** coverage is reported: for how many universe companies a product or segment
split exists. Also the shares for each company sum to at most 1, and fixtures from two filers
(one 10-K, one 20-F) are under test.

### M12. Sources in the store

- [x] A `source` column on edge rows, with supersession scoped per source
- [x] Consolidation in `snapshot`: one row per key by the precedence above, and alternatives
      kept for `explain`
- [x] `ripple explain` shows each edge's source and any alternatives

**Done when** loading the seed never touches `xbrl` or `llm` rows, precedence is tested, and
every M2 fixture number is unchanged.

### M13. Revenue lines to products, and the review queue

- [x] The Codex runner, minimal version: subprocess, lean flags, schema, stub-tested (hardened in M16)
- [x] For each company, Codex maps each revenue line to existing product nodes with the share
      assigned to each, or proposes a new product. The product list is the closed
      taxonomy (D41).
- [x] Accepted mappings become `PRODUCES` edges (`source: xbrl`, `weight_source: filing`),
      with valid time set to the fiscal year and the fact name and filing URL in the evidence.
- [x] `ripple review`: list, accept, reject and edit proposals and mappings

**Done when** the seed's SEC filers have XBRL-sourced `PRODUCES` weights and every mapping
has been through review.

### M14. Regions and major customers

- [x] Region nodes, and `EXPOSED_TO` edges carrying geographic revenue shares, stored and
      not traversed
- [x] Named major customers become `SUPPLIES` edges with filing weights, checked by W9. Customers
      the filing only anonymizes ("Customer A") are logged and skipped.

**Done when** regions load, `graph.py` ignores `EXPOSED_TO`, and W9 is tested.

### M15. 1a review

- [x] Rerun the golden files and sensitivity in both modes, and compare with Phase 0
- [x] `docs/phase-1-review.md`, section 1a: coverage, how the weights changed, and what XBRL could not supply

**Done when** at least 80% of `PRODUCES` weights for SEC filers in the graph come from XBRL.

## Milestones: 1b, text extraction

### M16. Taxonomy update and a hardened runner

- [x] Add the two accepted products to `data/seed`: `product/optical-dsps` (electro-optics DSPs
      inside optical transceivers) and `product/consumer-nand` (NAND outside enterprise SSDs).
      Re-map Marvell and Micron with the larger taxonomy (`ripple map --force`) and review.
- [x] At most 4 concurrent Codex calls
- [x] A `jobs` table in DuckDB (job, kind, input hash, status, attempts, output, usage), so
      any batch resumes where it stopped (`--resume`). Stop a batch cleanly on a rate-limit or
      quota error.
- [x] A usage log per call (tokens, model, effort, `codex` version), and `ripple usage` to sum it
- [x] A version check instead of a second binary (D66)

**Done when** stub tests cover success, schema failure, rate limit, resume and the concurrency
limit.

### M17. Sections

- [x] Cut each cached primary document into sections, with character offsets into its plain
      text: 10-K Items 1, 1A and 7; 20-F Items 3.D (risk factors), 4 and 5.
- [x] Detect body headings, not the table of contents, including the layouts a first
      measurement missed (Dell, Intel, Applied Materials, ASML, TSMC).
- [x] Cache the section text (gzipped, gitignored). The repo only ever stores offsets (D72).

**Done when** every universe filer yields Items 1 and 7 (or the 20-F equivalents), Item 1A is
found in at least 90% of 10-Ks, and fixtures from three layouts are under test.

### M18. Universe growth, one hop

- [x] Codex reads each seed filer's Items 1 and 1A and lists the companies named as suppliers,
      customers, competitors or partners, with the taxonomy product involved and span offsets.
- [x] Resolve names to CIKs (SEC ticker file, corporate suffixes stripped as in D62). Keep SEC
      filers with an annual report and at least one taxonomy product. List non-SEC companies,
      with the naming filer, in `data/seed/TODO.md`.
- [x] Rank by the number of seed filers that name a company, and add the top ~120 to
      `data/universe.yaml` (about 150 in total).
- [x] Run the 1a pipeline for the new companies: `fetch`, `map`, review (judge plus Claude
      Code, D67), `xbrl-edges`. New-product proposals from their mappings go to the user.
- [x] The judge checks 100 sampled additions: the company is named in the span, and the role
      and product are right.

**Done when** the universe holds about 150 companies (61 reached; the one-hop pool is exhausted, D77), their mappings are reviewed, and the
precision of the additions is reported.

### M19. Extraction schema, prompt and dev set

- [x] One Codex call per section. The schema returns:
  - `REQUIRES` proposals: product to input, both from the taxonomy (or a new-product
    proposal), a bucket (minor, major or dominant) with a one-line reason, and span offsets
  - disclosed numbers: a share of the company's revenue (by product, end market or customer),
    its value and period, the taxonomy products it concerns, and span offsets
  - `PRODUCES` and `SUPPLIES` statements, kept as evidence only (D65)
- [x] Span verification. The offsets must point into the cached section text; the text there
      must name both endpoints (aliases allowed); and a disclosed number must appear in it
      verbatim. Anything that fails is dropped before storage.
- [x] A dev set of 20 filings (mixed layouts, both 10-K and 20-F). Iterate the prompt, measure
      tokens per filing, and write the projected cost of a full pass (~150 filings x 3
      sections) into the Decisions before M21. If it is too large, fall back to Items 1 and 7
      (D70).

**Done when** the dev set runs end to end with span verification, and the projection is
written down.

### M20. Judge and review

- [x] Judge batches of about 20 items, each with its span text and the surrounding paragraph.
      Labels: correct or incorrect for the relation or number, and for each entity
      resolution, with a one-line reason.
- [x] `REQUIRES` proposals (D67, D69). Claude Code reads every proposal the judge rejects and
      a sample of those it accepts, then accepts or rejects. Accepted links go to the `llm`
      source with the Codex bucket at confidence 0.5; rejected ones are not stored.
- [x] Disclosed numbers (D68) go to the review queue. When one is better than Codex's split,
      Claude Code edits that company's XBRL mapping so the product weight matches it, cites
      the span offsets in the line note, and accepts the mapping again.
- [x] New products found in text go to the user.
- [x] A short summary for the user after each batch: counts accepted and rejected, the new
      links, and the numbers applied.

**Done when** the flow works on the dev set and the judge method is fixed.

### M21. Full pass, consolidation and 1b review

- [x] Extract across the universe (resumable), judge, review, and load the `llm` source.
- [x] Precision on 100 samples each of `REQUIRES` proposals, disclosed numbers and universe
      additions (all, if fewer); entity-resolution accuracy; and recall of the seed's
      `REQUIRES` links (the share of seed product-to-input links that extraction proposes).
- [x] Golden files and sensitivity in both modes; `docs/phase-1-review.md` section 1b.

**Done when** the exit criteria are met, or the review explains why not.

## Exit criteria

- 1a: at least 80% of `PRODUCES` weights for SEC filers come from XBRL.
- 1b: judge-estimated precision of at least 85% on `REQUIRES` proposals, disclosed numbers
  and universe additions (100 samples each, all if fewer). Entity-resolution accuracy and
  the recall of seed `REQUIRES` links are reported. The universe holds about 150 companies.
- Every `llm` row's evidence carries span offsets verified against the cached text, and a
  filing URL. No filing text is stored in the repo.
- The golden files pass, and all tests pass offline.

## Risks

| Risk | Mitigation |
|---|---|
| Bulk use of Codex output through a ChatGPT login | The user checked OpenAI's terms on 2026-09-27 and confirmed it is fine. The runner can still switch to an API key with a config change. |
| Plan quota: Astra costs 5× Sol, with no batch discount; a full text pass is about 7M tokens | Lean flags, batched judge calls, and the cost projection in M19, with Items 1 and 7 as the fallback. Resumable jobs mean a run stopped by quota continues later. |
| The judge and the extractor are the same model (D46), so precision measures self-agreement | Recorded as a known limit. The recall check against the seed gives an independent signal. |
| The Codex CLI is an alpha and changes quickly | Record and check the version per call (D66), and cover the runner with stub tests. |
| Section headings vary by filer (Dell, Intel, Applied Materials, 20-Fs) | M17 tests three layouts, and reports per filer which sections were found. |
| Companies tag product and segment revenue in XBRL inconsistently | Measure coverage in M11 and revisit the 80% target if coverage is lower. |
| Disk: about 5 GB free | Cache only the XBRL instance and section text. The cache is gitignored and can be deleted and fetched again. |

## Decisions

Numbering continues from `docs/phase-0.md`.

- **D38.** Phase 1 is split into 1a (structured XBRL facts) and 1b (LLM text extraction), and 1a comes first, because weights, not links, were Phase 0's main weakness.
- **D39.** The universe is the value chain of the Phase 0 slice, about 150 SEC filers. Companies that do not file with the SEC keep their hand-seeded edges and are reported on later.
- **D40.** Hand-seeded and extracted edges coexist as separate sources. Evidence decides between them, in the order verified or `review`, then `xbrl`, then `seed`, then `llm`.
- **D41.** The product taxonomy is closed. New products, and every `REQUIRES` edge from text, are proposals at confidence 0.3 until accepted in `ripple review`.
- **D42.** 1b extracts `PRODUCES`, `SUPPLIES` and `REQUIRES` (the last as proposals only). `COMPETES_WITH` is not extracted, because it is not traversed.
- **D43.** Region nodes (`region/<iso>`) and `EXPOSED_TO` edges are stored but not traversed.
- **D44.** No weight-range fields. Bucket ranges come from `model.bucket_range`, and sourced numbers are point values.
- **D45.** The SEC User-Agent comes from the `RIPPLE_SEC_USER_AGENT` environment variable, set in `~/.zshenv` so that non-interactive shells (Claude Code, cron) see it. The EDGAR client refuses to run without it, or if it lacks an email.
- **D46.** Codex `gpt-6-astra`, through the user's ChatGPT Pro login, is both the extractor and the judge. There is no human calibration set, so precision is as the judge measures it.
- **D47.** The integration runs `codex exec` as a subprocess, with lean flags, `--output-schema`, a pinned CLI binary, at most 4 concurrent calls, and resumable jobs.
- **D48.** Tests never call Codex or SEC. They use a stub `codex` executable and recorded EDGAR and XBRL fixtures.
- **D49.** No GLiNER. Candidate lists come from alias matching, and the LLM does the rest. This departs from PLAN §6 to keep the pipeline small.
- **D50.** Raw filings are cached in `data/raw/` (gitignored). The store keeps facts, URLs and character offsets, not full text.
- **D51.** The cache keeps three files per filing, gzipped: the primary document (M17 cuts sections from it), the XBRL instance (`<doc>_htm.xml`, extracted from inline XBRL) and the label linkbase (`_lab.xml`, readable names for revenue lines in M13). The 27 seed filers take 16 MB.
- **D52.** `data/universe.yaml` lists every company in the graph. `sec: true` means the latest annual form (10-K, 20-F or 40-F, amendments ignored) was found. A listed company without such a filing keeps its CIK and gets `sec: false`. `ripple universe` keeps entries it did not generate, so later milestones can add companies.
- **D53.** XBRL is read by a small standard-library parser (`ripple/xbrl.py`), not `edgartools` or Arelle. Instance documents are plain XML, the parser needs only revenue and concentration facts, and the heavy libraries do not fit the nearly full disk. It reads US-GAAP and IFRS filings alike (TSMC and ASML verified).
- **D54.** Breakdown rules: facts cut along exactly one axis count (a `ConsolidationItemsAxis` of operating segments is ignored). Aggregate lines are dropped, largest first, stopping once the rest partition the total. If no partition emerges, the largest subset that sums to the total is kept, and ties go to company-specific members. A breakdown whose lines do not sum to the total within 0.5% is flagged partial (for example, segments before intersegment eliminations). The currency is kept, since TSMC reports in TWD and ASML in EUR.
- **D55.** M11 coverage: 26 of 27 SEC filers tag a product or segment split for their latest fiscal year. Lumentum is the exception: its segment facts use a different revenue concept from its total. Major-customer members are often anonymous ("Customer A") or groups ("Four Customers", "US and Europe based end customers"), so M14 must filter them.
- **D56.** Each source is a YAML directory listed in `data/sources.yaml`: `seed` now, then `xbrl`, `llm` and `review`. `ripple load` validates all listed sources together and writes each one scoped, and `ripple load DIR --source NAME` loads one. Validation checks duplicates (E1, E8) within a source, and runs the summing and cell rules (E6, E7, E10, E12, W5) on the consolidated edges. The store gained a `source` column on nodes and edges, and existing stores migrate with every old row set to `seed`. `snapshot` returns the consolidated edges plus `sources` and `alternatives`, which `ripple explain` prints.
- **D57.** The Codex runner (`ripple/codex.py`) calls `codex exec` with a read-only sandbox, approvals off, web search off, instructions passed as a file, and every tool feature disabled. It disables only the features the installed `codex` lists, so an alpha release that renames one cannot break a call. The executable comes from `RIPPLE_CODEX_BIN`, then `codex` on PATH, then the copy inside the ChatGPT app. A mapping call costs about 6.5k input tokens, of which about 5k is fixed overhead.
- **D58.** Mapping rules: map the complete product breakdown, else the complete segment breakdown, else a partial one. Service, spare-parts and support lines stay unassigned. Fractions are clipped to [0, 1], and scaled down if a line's fractions sum above 1. An edge is `filing` (confidence 0.9) only when every line behind it maps whole; otherwise it is `manual` (confidence 0.7), because Codex's split is a judgment. Proposals live in `data/review-queue/mappings/`; accepting one writes `data/xbrl/edges/<company>.yaml`, which is generated and not edited by hand.
- **D59.** Amends D40: among untrusted rows, the newer fact (later `valid_from`) wins before source order. Without this, Dell's FY2026 filing mapping (0.22, open-ended) would override the seed's newer FY2027 version (0.37). The order is now: human-trusted (`review`, or verified evidence), then newest `valid_from`, then `xbrl`, `seed`, `llm`.
- **D60.** Claude Code reviewed the first 26 mappings (reviewer `claude-code` in each queue file). 24 were accepted, 3 of them after edits: Carrier's chiller fraction came from the CEO's ~$1B, Coherent's industrial lasers were removed, and Constellation's `electricity-demand` was removed because it is a demand node. 2 were rejected: Super Micro's breakdown held only service lines, and Cummins' product lines overlap. Lumentum has no breakdown. Three new-product proposals await the user (D41): Marvell's optical DSPs, Micron's non-enterprise NAND, and Cummins' generator alternators. The golden tests now run on all sources in `data/sources.yaml`.
- **D61.** Regions come from the XBRL geography breakdown via `ripple xbrl-edges`. Country members become `region/<iso>` (`country:TW` becomes `region/tw`), named groups become `region/<slug>`, and catch-alls ("Other countries", "Rest of Asia", "Non-US") are skipped. The run produced 27 region nodes and 105 `EXPOSED_TO` edges (`filing`, confidence 0.9), in `data/xbrl/nodes/regions.yaml` and `data/xbrl/edges/<company>-regions.yaml`. E3 allows company to region, regions are exempt from W3, and `graph.py` does not traverse them (D43).
- **D62.** Customers: anonymous or grouped members ("Customer A", "Four Customers", "... end customers", "Distributor A") are skipped. Named ones are matched to company nodes after stripping corporate suffixes ("Dell Inc." becomes "Dell"), requiring a search score of at least 0.9. When a product path already links the pair (W9, up to 3 `REQUIRES` steps), the edge is stored at confidence 0.3 and does not propagate. XBRL rarely names customers that are in the graph: the only edge is Intel `SUPPLIES` Dell (19%), stored at 0.3 because Dell's AI servers already require Intel's server CPUs. W9 warns only about edges that would propagate.
- **D63.** Amends D40 and D59. Rows for one edge key are ranked: (1) human-trusted rows (`review`, or verified evidence); (2) sourced numbers (`filing`, `manual`) before bucket guesses; (3) the newest fact, when it is more than 90 days newer; (4) the source order `xbrl`, `seed`, `llm`. The plain recency of D59 let seed buckets with approximate dates ("2025-01-01") beat filings from the same fiscal year. `model.rank_rows` is shared by the store and the validator.
- **D64.** 1a exit: 84% of `PRODUCES` weights for SEC filers come from `xbrl` (47 of 56). Only 3 are pure filing numbers; 44 multiply a filed line share by a Codex split (`manual`, confidence 0.7). AI-compute sensitivity now clears the 0.8 top-10 Spearman bar (0.867 bucket, 0.806 flat). See `docs/phase-1-review.md`.
- **D65.** 1b is refocused after 1a (grilling, 2026-09-27). The weak points are now the 44 Codex line splits and the guessed `REQUIRES` shares, and text-extracted `PRODUCES` would mostly duplicate XBRL, since `llm` rows rank last. So 1b grows the universe first, then extracts `REQUIRES` proposals and disclosed revenue shares. `PRODUCES` and `SUPPLIES` found in text are stored in the `llm` source at confidence 0.3, as evidence that shows up in `ripple explain`.
- **D66.** Amends D47: no second pinned `codex` binary, which would take about 235 MB on a nearly full disk. The runner records the `codex` version per call and warns when it differs from the last tested version.
- **D67.** Amends D41. The judge plus Claude Code accept or reject mappings and `REQUIRES` proposals, and the user gets a summary per batch. New products stay the user's decision, because the taxonomy decides what the graph can express. (User decisions on 1a's proposals: accept `optical-dsps` and `consumer-nand`; reject generator alternators.)
- **D68.** Revenue shares disclosed in text do not change the precedence: the source order stays. They go to the review queue, and Claude Code applies a better one by editing the company's XBRL mapping, not by adding a competing row.
- **D69.** A new `REQUIRES` link from text gets a Codex bucket (minor, major or dominant) with a one-line reason. After the judge and review accept it, it enters the `llm` source at confidence 0.5.
- **D70.** Text extraction reads 10-K Items 1, 1A and 7 (20-F Items 3.D, 4 and 5): about 46k tokens per filing, about 7M per 150-company pass. It falls back to Items 1 and 7 if the M19 projection is too large for the plan allowance.
- **D71.** The universe grows by one hop: companies named in the seed filers' Items 1 and 1A, kept if they file with the SEC and make a taxonomy product, and ranked by how many seed filers name them, up to about 150 in total. Non-SEC companies named this way are listed for later.
- **D72.** Spans are stored as offsets (section and character range) in an optional `span` field on evidence, never as text: CLAUDE.md forbids copying filing text into the repo. The section text lives only in the gitignored cache, and the judge and verification read it from there.
- **D73.** 1b exit criteria: judge-estimated precision of at least 85% on `REQUIRES` proposals, disclosed numbers and universe additions (100 samples each). Entity-resolution accuracy and the recall of seed `REQUIRES` links are reported, not gated.
- **D74.** M16: batches run through `ripple/jobs.py`, with its own DuckDB file `data/jobs.duckdb` (gitignored). A job is (kind, key) plus an input hash. A done job with the same input is skipped, a failed one is retried on the next run, and a rate-limit or quota error ("usage limit", "rate limit", "quota", "429") marks it `stopped`. No new job starts after a stop, and the batch raises `BatchStopped`. `ripple usage` sums tokens by kind. Seed clean-up after the taxonomy update: `Marvell PRODUCES optical-transceivers` (a stand-in for its DSPs) was removed, since `optical-dsps` now carries it (0.26). Micron's seed DRAM and HBM rows (fiscal Q4 run rate) were removed too: mixed with the FY2025 XBRL NAND split, they summed to 1.02 (E6). One consistent period is better than a newer partial one.
- **D75.** M17 sections (`ripple/sections.py`, `ripple sections`). Offsets point into `to_text` output: one line per block element, with a rules version in `sections.json` that invalidates old caches. A body heading is the last short "Item N" line that is not a table-of-contents row, and a 20-F's 3.D is "D. Risk Factors", or a plain "Risk Factors" line between Items 3 and 4. A target under 2,000 characters is not a section. Only Item 1 (20-F: Item 4) is required: Eaton incorporates its MD&A by reference, so it has Items 1 and 1A only. When the required item is missing, the document is cut into fallback chunks (≤128k characters each, at most 5). Result on the 27 seed filers: items for 25, with 1A in all 24 standard 10-Ks; fallback chunks for Intel (a report layout with only a cross-reference index) and ASML (an integrated annual report). Section text lives in the cache (`text.txt.gz`), never in the repo.

- **D76.** M18 growth fixes after the first run. The resolver's prefix rule now accepts only generic extra words ("Cisco" → Cisco Systems, but not "Sumitomo" → Sumitomo Mitsui Financial or "Westinghouse" → Wabtec). A filer counts only if its latest annual report was filed within 550 days, which drops deregistered filers (Hitachi 2011, Canon 2023, ABB 2024). Each `grow` run regenerates its additions from scratch. Review rejections, keyed by CIK, live in `data/review-queue/growth-review.yaml`.
- **D77.** One hop from 27 seed filers yields only 29 eligible SEC additions: 349 candidates, of which 246 are non-SEC and 42 make no taxonomy product. The universe has 66 entries, not ~150. Reaching 150 would need a second hop; that is left for the user.
- **D78.** The growth judge checks the selection rule itself: the role, from the naming filer's text, and "makes at least one of" the products, from passages of the company's own filing (`judge.product_passage`). First pass: 21 of 29 correct (72%), entities 29 of 29 before the fix and 24 of 29 after. Five were rejected in review (Jabil, Sanmina, Meta, Tesla, Thermo Fisher). Amkor, TE and Extreme were kept against the judge: they do sell 2.5D packaging, copper interconnect and switches.

- **D79.** M18 pipeline for the 24 additions. The judge accepted 11 mappings outright. Claude Code reviewed the 13 flagged ones and accepted 12, most of them flagged only because a line's fraction can't be read from the excerpt. It edited two. Generac: residential standby generators are home backup, not the data-center genset market, and C&I stationary generators were cut to 0.6. Oracle: its cloud's 0.31 of `datacenter-capacity` was removed, because cloud providers buy build-out rather than sell it, consistent with the AWS and Azure rulings. 12 additions get no `PRODUCES` edge: hyperscalers, Qualcomm, Semtech, NextEra and Alphabet (no usable breakdown), plus four whose lines proposed new products. The new-product proposals go to the user: enterprise servers and storage (HPE, Oracle), PCIe/CXL connectivity (Astera), RF and analog chips (MACOM), processor IP (Arm), flip-chip and SiP packaging (Amkor), and cloud compute as a product the hyperscalers sell.

- **D80.** M20. Evidence gets an optional `span` (section, start and end offsets into the cached filing text), stored as JSON in a new evidence column. An absent span is left out of the content hash, so the field's arrival superseded nothing. `ripple judge-extract` judges pending `REQUIRES` proposals and disclosed numbers against ±400 characters of cached text. It marks each `accepted` (reviewer `judge`) or `flagged`, and Claude Code settles the flagged ones by editing the queue file. The `REQUIRES` claim judged is the relation only; the bucket stays a Codex estimate held at confidence 0.5. `ripple requires-edges` writes one edge per accepted (product, input) link to `data/llm/edges/requires.yaml`. The bucket is the most common one proposed (the smaller on a tie), with up to 3 evidence entries, each with a URL and span. It skips links the other sources already have, and any link that would push the demand shares into an input over 1 (E7).
- **D81.** Golden `ai_compute.yaml` `top_n` widened from 10 to 12 after growth (reason written in the file): Fabrinet and Credo, both concentrated in AI data center hardware, moved SK hynix to 11th.

- **D82.** M19 dev set: 20 filers, including TSMC, ASML and Arm (20-F or fallback layout) and Intel (fallback). 64 section calls used 1.54M input tokens, about 77k per filer. A full pass over 51 SEC filers projects to 3.9M input tokens, under D70's 7M, so Items 1, 1A and 7 stay. Span verification dropped 104 items whose quote wasn't in the text or whose endpoints weren't named near it.
- **D83.** M20 on the dev set. `REQUIRES`: the judge passed 37 of 40 (92%). Claude Code rejected 4 that would double-count demand the seed already routes (capacity→ASICs, accelerators→CPUs, CPUs→DRAM, AI servers→HBM). It accepted the 3 flagged electricity links, where the judge objected to the node label rather than the link. 32 of the 36 accepted proposals repeat seed links, which checks the extraction against the hand-built graph. Only 1 new edge was written (enterprise SSDs require DRAM, minor). Two links were skipped under E7 (switches→transceivers, CPUs→packaging): their inputs' shares already sum close to 1. Disclosed numbers: 205 of 208 judged correct. The judge only checks that the number is in the text; Codex's product tags on these rows are loose (a regional revenue share tagged with lithography products). The one number that beats a split, TSMC's 74% of wafer revenue from 7nm and below, confirms the existing mapping; its span is cited in the mapping note and the number is marked `applied`.

- **D84.** M21 full pass. 97 `REQUIRES` proposals: the judge passed 65 (67%); after review, 87 were accepted and 10 rejected. That produced 7 new minor links in `data/llm/edges/requires.yaml`, 24 repeats of seed links and 2 E7 skips. Disclosed numbers: 505 of 531 judged correct (95%). The 27 flagged ones had a correct number and an embellished description, so they were accepted. Seed `REQUIRES` recall is 46%. A rerun of `extract` now keeps review state by item ID (`keep_review`). Exit bar: met for disclosed numbers, not met for `REQUIRES` (67%), additions (79%) or universe size (61). See `docs/phase-1-review.md` 1b.

## Open questions

- ~~Do OpenAI's terms allow bulk extraction through a ChatGPT login?~~ The user confirmed it on 2026-09-27.
- What does a full pass cost against the plan allowance? The M19 dev set answers it.
- ~~What share of universe companies tag product or segment revenue in XBRL?~~ 26 of 27 (D55).
- How are 10-Ks refreshed each year? Presumably a new valid-time version per fiscal year, possibly on a schedule in Phase 2.
