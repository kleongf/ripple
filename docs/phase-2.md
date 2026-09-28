# Phase 2: Signals

## Goal

Tell the system what is happening, and make "non-obvious" a measurement instead of a guess.

Phase 0 built the graph, propagation, paths, CLI and MCP server. Phase 1 replaced hand-written
weights with filing-anchored ones. Two gaps are left, and they are the same gap twice:

- **The shock is typed in by hand.** `PLAN.md` §7 calls for a shock vector per theme, derived
  from news coverage: which themes are moving, how unusually, and in which direction.
- **`hide_obvious` is a placeholder.** It hides companies within 3 hops (D5, D23), a structural
  proxy for a behavioural quantity. `PLAN.md` §8 defines novelty as
  `exposure × (1 − percentile of attention)`, and attention only exists once news does.

Both come from one source, GDELT, and both are validated against events that already happened.

The propagation, scoring and paths from Phase 0 do not change. What changes is that the system
now has an opinion about *when* to look and *who is already crowded*.

**Size:** medium (PLAN §12), about eight sessions. Each milestone is sized for one Claude Code
session. Work in order, tick tasks here, and record decisions at the bottom.

**Done when** (PLAN §12) known past events show up as bursts on the right themes with the right
direction.

## Scope

**In scope**

- A GDELT DOC 2.0 client with a response cache, and a `coverage` table in the existing store
- About 15 hand-written themes, each with a query, a `kind` and `DRIVES` edges with evidence
- A shortlist of new products from the 1b proposals, so those themes have something to attach to
- Burst detection: coverage share, a trailing baseline, Poisson surprise, ratio, persistence
- Standardized tone as a reversal flag
- Attention per company, novelty per (theme, company), and `hide_obvious` switched to attention
- A pre-registered event set and a measured hit rate
- Theme proposals from Codex into the review queue

**Out of scope** (later phases)

- Price data, abnormal returns, the priced-in check and backtests (Phases 3 and 4)
- `trending_themes` and the rest of the MCP surface (Phase 3, `docs/phase-3.md`)
- A per-theme shock scale, and price effects of supply constraints (Phase 5, D14)
- BERTopic, embeddings, any local model (D93: about 1.2 GB of disk free)
- A second universe growth hop, and re-judging Phase 1's `REQUIRES` proposals (D95)

## What GDELT gives

Established from the DOC 2.0 documentation on 2026-09-27, to be confirmed by the M24 spike:

- `mode=TimelineVolRaw` returns, per day, the count of articles matching a query and a `norm`
  field holding all articles GDELT monitored that day. Coverage **share** = matched / norm,
  which cancels the weekend dip and GDELT's growth in sources over the years.
- **Timeline modes search from 2017-01-01 to now.** `ArtList` and every other non-timeline mode
  only consider the most recent 3 months of the requested window. This single fact decides the
  direction design (D90): no LLM can read the headlines behind a 2023 burst, because they cannot
  be retrieved.
- Query syntax: quoted phrases, `(a OR b)`, `-` negation, `domain:`, `sourcelang:`,
  `sourcecountry:`, `near`, `repeat`. Terms are ANDed implicitly — there is no `AND` keyword.
  `maxrecords` up to 250 on `ArtList`. `format=json`.
- No published rate limit. Stay at 1 request / 5 s with a descriptive User-Agent.

Sources: [DOC 2.0 API debut](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/),
[1.5-year searching](https://blog.gdeltproject.org/doc-2-0-updates-1-5-year-searching-and-updated-mobile-interface/).

## Design

### Store

One new table in `data/ripple.duckdb`. DuckDB's single-writer rule already applies to
`ripple load`, and the MCP server opens read-only per call (D27), so a long fetch blocks a load
but never a reader.

```sql
CREATE TABLE coverage (
    kind VARCHAR NOT NULL,        -- 'theme' | 'company'
    key VARCHAR NOT NULL,         -- node ID
    day DATE NOT NULL,
    matched BIGINT NOT NULL,      -- articles matching the query
    norm BIGINT NOT NULL,         -- all articles GDELT monitored that day
    tone DOUBLE,                  -- mean tone of matching articles, null when unfetched
    query_hash VARCHAR NOT NULL,  -- so a query change is visible in the data
    recorded_at TIMESTAMP NOT NULL
);
```

**Append-only, never updated** (D86). GDELT backfills, so a past day's count can change; a
refetch inserts a new row and readers take the latest row at or before `known_at`. That is the
same bitemporal discipline as edges, and it is what a Phase 4 backtest needs.

Bursts are **derived, not stored**. Fifteen themes over ninety days is nothing to recompute, and
a stored burst would be a second thing to keep point-in-time.

### Theme queries

A theme's query is part of its definition, and changing it changes every series it produced. So
it lives on the node, where the store already versions it (D87):

```yaml
- id: theme/liquid-cooling-adoption
  type: theme
  kind: share_shift
  label: Liquid cooling adoption
  query: '("liquid cooling" OR "direct-to-chip" OR "immersion cooling") (datacenter OR "data center")'
```

One optional `query` field on `Node` is the only schema change in Phase 2. Company queries are
built from label, aliases and ticker, and are not stored. New validator warning **W10**: a theme
with no `query` cannot be measured.

### Burst statistic

Per theme, per day, from the stored series:

1. `share = matched / norm`.
2. **Baseline**: the median share over the previous 90 days, excluding the last 3 days, so a
   building burst does not raise its own baseline.
3. **Poisson surprise**: expected count `λ = baseline_share × norm(today)`, and surprise is
   `−log10 P(X ≥ matched | Poisson(λ))`. This is the threshold. It is the right statistic here
   because a thin theme gets 0–3 articles a day, where a Gaussian z-score on counts is noise.
4. **Ratio** = `share / baseline_share`, reported because it is what a human reads
   ("5.2× its 90-day normal").
5. **Gaussian z** on log share over the same window, reported for comparison with PLAN §7.
6. A burst needs all three: surprise above the threshold, at least 5 matched articles, and
   persistence over 2 consecutive days. Its start and end are the first and last day above the
   threshold.
7. **Tone flag**: mean tone standardized against the theme's own trailing 90-day tone.
   Semiconductor news is mildly negative in absolute terms, so only the deviation carries
   information. A deviation past the threshold sets `tone_flag: possible_reversal`. It never
   flips a sign (D90).

Tested on synthetic series with hand-computed answers, in the spirit of `tests/fixtures/mini/`:
a flat series (no burst), a step (one burst with the right window), a one-day spike (no burst,
fails persistence), a thin theme going 1 → 2 articles (no burst, fails the floor), and a weekend
dip (no burst, which is what using share instead of counts buys).

### Direction

Direction is a property of the theme, not of the day (D90). The existing themes are already
written directionally: a burst on `liquid-cooling-adoption` means more adoption, and the
`DRIVES` polarity already says who wins and who loses. So a burst means "this theme is
intensifying", and nothing has to classify it.

The case PLAN §7 warns about — "shortage of power transformers" is negative sentiment and good
news for transformer makers — is handled by giving supply tightness its own theme node, whose
definition carries the sign, and by leaving the price effect to Phase 5 (D14).

Tone is kept for the one thing definition cannot cover: a burst that happens *because the theme
is reversing* ("AI capex pause" articles inside an `ai-compute-demand` query). That raises a
flag for a human, not a sign flip.

### Attention and novelty

`A(c)` is the company's share of all GDELT coverage over the trailing 90 days, from one query
per company built from its label, aliases and ticker: about 61 requests, five minutes (D91).

```
novelty(c, θ) = |exposure(c, θ)| × (1 − percentile of A(c) among companies with nonzero
                                     exposure to θ)
```

with the exposure's sign kept. The percentile is taken **inside the theme's exposed set**, so
the factor stays theme-relative even though `A` is not: Nvidia sits at the top of every set,
Ajinomoto and Fabrinet at the bottom.

**The limitation this accepts.** `A(c)` cannot distinguish "famous, but not for this theme" from
"famous because of this theme". Constellation Energy has modest total coverage and is the poster
child for AI power; a company with heavy unrelated coverage looks crowded when it is not. The
mitigation is opt-in rather than the default path: `ripple exposed --verify-attention` runs
paired `(theme query, company aliases)` queries for the top ~10 ranked companies only (about ten
requests, under a minute) and prints the paired share next to the company-only one, so a lead
can be checked before it is acted on. The review reports how far the two diverge.

`hide_obvious` becomes "hide companies above the Nth attention percentile" (default 80), falling
back to the hop-count rule for any company with no attention row (D92). That retires the D5 and
D23 placeholder as Phase 0 planned.

### Signal and exposure, side by side

They are never multiplied (D88). A burst gives a unit of *surprise*; exposure is a fractional
revenue impact per unit of theme *shock*. News volume is attention, not demand — coverage
quadrupling does not mean demand quadrupled, and there is no honest conversion.

So `ripple trending` reports (theme, window, surprise, ratio, tone flag), `ripple exposed` keeps
exposure per unit shock and gains attention and novelty columns, and the combined reading is
"these themes are hot; under each, these companies are exposed and under-covered" — two numbers
in two units. This closes the Phase 0 open question about a per-theme shock scale by declining
it.

## Milestones

Numbering continues from `docs/phase-1.md`. Each milestone adds its own modules to the CLAUDE.md
layout and its own commands to the CLAUDE.md commands block as it lands.

### M22. Taxonomy top-up

The 1b extraction proposed 423 new products (332 distinct labels). Only the ones a Phase 2 theme
needs come in. New products are the user's decision (D41, D67); this is the shortlist, each
chosen because it unlocks a theme:

| Product | Unlocks | Named by |
|---|---|---|
| co-packaged optics | CPO adoption (share_shift against pluggable transceivers) | 5 filings |
| photonic integrated circuits | the same theme's input layer | 3 filings |
| optical circuit switches | optical switching in AI fabrics | 4 filings |
| battery energy storage | datacenter power, grid firming | 10 filings |
| processor IP | Arm in the datacenter (share_shift against x86) | Arm mapping |
| general-purpose servers | separates AI servers from the rest of the box market | 5 filings |
| enterprise servers and storage | HPE and Oracle get a `PRODUCES` edge at all | HPE, Oracle mappings |
| PCIe / CXL connectivity | accelerator-to-memory interconnect | Astera mapping |
| flip-chip and SiP packaging | packaging below 2.5D | Amkor mapping |

Borderline, listed so the user can pull them in rather than have them dropped silently: DPUs and
IPUs (6 filings), network routers (6), cloud computing services (7 — but cloud providers buy
datacenter capacity rather than sell it, and D79 removed exactly this for Oracle and AWS), PCBs
(3), RF and analog chips (MACOM).

- [x] The user accepts or trims the shortlist: all nine accepted plus RF and analog; every
      borderline candidate rejected (D96)
- [x] Add the accepted products to `data/seed/nodes/products.yaml` and place each one in the
      chain with `REQUIRES` edges above and below it, sourced and bucketed
      (`data/seed/edges/phase2-products.yaml`, 21 edges, 0 validation errors)
- [x] `PRODUCES` edges from the companies that make each one: 23 companies re-mapped with
      `ripple map --force` (146k input tokens), reviewed, 21 accepted and 2 rejected (D97).
      Four products needed hand-written seed producers (D98).
- [x] `ripple validate`, then the golden files and sensitivity, to show nothing broke: 298 tests
      pass, all four golden files pass, and AI compute returned to the 0.800 bucket-mode bar

**Done when** every accepted product has at least one producer and at least one edge into the
existing chain, and the four golden files still pass. **Met**, with three producers held below
the propagation threshold (D98).

### M23. Themes and the pre-registered event set

- [x] 15 theme nodes with `kind`, aliases and `query`: the 4 from Phase 0 plus 11 new, spanning
      memory, equipment, export controls, grid, storage, nuclear, optics, processor IP and the
      server mix. Three new `share_shift` themes each have a real loser (D100).
- [x] `DRIVES` edges with bucket weights and evidence (`data/seed/edges/themes-phase2.yaml`,
      21 edges), clean under E10 and E11, with two expected W5 warnings (D9)
- [x] Validator warning W10: a theme with no `query`, with three tests
- [x] `tests/golden/events.yaml`: 16 events over 2022 to 2025, written before any coverage was
      fetched, with three reversal cases for the tone flag (D101)
- [ ] The user approves the theme list and the event set

Candidates to draft the event set from, dates to be checked while writing: ChatGPT's launch,
Nvidia's May 2023 guidance, the 2022-10 and 2023-10 export controls, the 2023 CoWoS and HBM
squeeze, the 2024 Blackwell schedule reports, the Microsoft–Three Mile Island power deal, GB200
and the liquid-cooling ramp, Stargate, DeepSeek R1, the 2025 datacenter-lease reports (a
demand-down case), and transformer and turbine lead times.

**Done when** the themes validate with no errors, every theme has a query, and the event set is
committed before M24 runs.

### M24. GDELT client, coverage store and CLI

- [x] Spike done (D102): `TimelineVolRaw` returns **3,525 daily points for 2017-01-01 to
      2026-09-27 in one request**, with `value` (matched) and `norm` (all monitored) exactly as
      documented. So one request per series covers all history.
- [x] `ripple/news.py`: query building, `format=json`, 12 s pacing, User-Agent from
      `RIPPLE_SEC_USER_AGENT`, one polite retry then a clean stop, gzipped cache in `data/news/`
- [x] The `coverage` table, append-only with `recorded_at` and `query_hash`, and a reader that
      takes the latest row at or before `known_at`
- [x] `ripple signals fetch [--theme ID | --company ID | --all] [--from DATE] [--to DATE]` and
      `ripple signals show [node] [--days N]`
- [x] Offline fixtures in `tests/fixtures/gdelt/`: thick and thin series, tone, an article list
      and the plain-text rate-limit refusal; 17 tests

**Done when** every theme and company has a series from 2022-01-01 to today, the row count and
date coverage are reported, and all tests run offline.

### M25. Burst detection

- [x] `ripple/signal.py`: share series, trailing median baseline excluding the last 3 days,
      Poisson surprise with an overdispersion correction (D103), ratio, Gaussian z, 5-article
      floor, 2-day persistence, burst windows, standardized tone and the reversal flag
- [x] `ripple trending [--window N] [--min-surprise N] [--theme ID] [--json]`
- [x] 18 synthetic tests including all five named cases, plus dispersion and tone behaviour
- [x] A missing day is a gap, not a zero, with a test

**Done when** the synthetic cases pass and `ripple trending` over the last 90 days is readable.

### M26. Query precision

- [x] `ripple/queries.py`: samples `ArtList` per theme and judges relevance in batches through
      `ripple/judge.py` and `ripple/jobs.py`; stores URL, verdict and reason, never titles (D104)
- [x] `ripple query-precision`: samples, judges, prints a per-theme table with the 85% bar and
      writes `data/query-precision.yaml` (URLs, verdicts and reasons only)
- [ ] Report per-theme precision, refine the queries that miss, and re-measure — **pending**, the
      measurement run is blocked on GDELT's rate limiter
- [x] Themes with too few recent articles are reported as "too thin" rather than scored

Tuning is on **article relevance**, never on burst alignment with the event set, so the M29 event
test stays independent. Write that down where the numbers are reported.

**Done when** per-theme query precision is reported and every theme reaches 85%, or the review
says why not.

### M27. Attention and novelty

- [x] `ripple/attention.py`: 90-day company coverage share, the percentile inside a theme's
      exposed set, and `novelty = |exposure| × (1 − percentile)` with the exposure's sign
- [x] `ripple attention [--theme ID]`, and attention, novelty and percentile columns on
      `ripple exposed`, plus `--by-novelty` to sort by it
- [x] `hide_obvious` uses the attention percentile (default 0.8), falling back to hop count when a
      company has no attention row; `--obvious-hops` stays for that fallback
- [x] `find_exposed` returns `attention`, `attention_percentile` and `novelty` per company, and
      its notes say attention is company-wide coverage rather than theme-paired, and that null
      means unknown rather than none
- [x] Paired queries for the top ~10 only: built as the top-level `ripple verify-attention`
      command rather than an `exposed` flag; the divergence is measured in Phase 3, M34
- [ ] A novelty ranking per theme, read by hand and written into the review

**Done when** every universe company has an attention row, `hide_obvious` no longer uses hops by
default, and the novelty ranking for `ai-compute-demand` is one the user agrees with or can say
which input is wrong in.

### M28. Theme proposals

**Not built.** This milestone needs bursts from a completed live pass, and the plan marks it
cut-first with nothing depending on it. Deferred with the reason recorded here and in the review.

- [ ] For bursts no existing theme explains, send the burst window's headlines to Codex and have
      it propose candidate themes with the products they would drive, into
      `data/review-queue/themes/`
- [ ] `ripple review --queue themes` lists, shows, accepts and rejects them, reusing the M13
      review machinery
- [ ] Accepted themes are written by hand into `data/seed/nodes/themes.yaml` with a query and
      `DRIVES` edges, then measured

**Done when** a proposal round runs end to end on real bursts and the user has a list to decide
on. Cut this milestone first if the Codex allowance is tight; nothing else depends on it.

### M29. Event validation and review

- [x] `ripple/events.py` and `ripple events`: hit rate, misses with the nearest-burst gap and the
      twin event that shares the burst (D110), tone flag accounting, approximate dates reported
      separately, and a false-positive count. 16 tests.
- [x] `ripple calibrate`: threshold from burst frequency, blind to the event set (D109)
- [ ] Run it on all 15 re-fetched series and report the numbers — **partly done**: on the one
      series held, 1 of 6 events hit at the calibrated threshold (see the review)
- [x] The golden files and sensitivity in both modes (M22 results are in the review)
- [x] `docs/phase-2-review.md`, with the measured numbers still marked pending

**Done when** the exit criteria are met, or the review explains why not.

## Exit criteria

| Criterion | Status |
|---|---|
| At least 80% of pre-registered events burst on the expected theme within ±3 days, false positives reported | **pending a completed live pass**; the runner and event set are done (`ripple events`) |
| Per-theme query precision of at least 85% on ~20 sampled articles | **pending**; `ripple/queries.py` and its tests are done |
| Every universe company has an attention row, and `hide_obvious` uses the attention percentile | code done; the 61 company series are **pending** |
| Signal and exposure reported side by side, never multiplied | met (D88): `ripple trending` and `ripple exposed` are separate commands and no code multiplies them |
| No article text in the repo: URLs, counts and offsets only | met; `coverage` holds counts, and query precision stores URL, verdict and reason only (D104) |
| All tests pass offline, and the four golden files still pass | met: 371 tests, ruff clean |

**Why three are pending.** GDELT tolerates a far lower request rate than it documents (D102), so a
full 76-series pass takes roughly two hours of wall time and only one process may be requesting at
a time. Nothing is blocked on design: rerun `ripple signals fetch --all --from 2022-01-01` until it
stops printing `stopped:` (it resumes from the response cache), then run `ripple events`,
`ripple trending` and `ripple attention`. The event set was fixed before any theme series was
fetched, so the measurement is honest whenever it is taken.

## Risks

| Risk | Mitigation |
|---|---|
| `ArtList` covers only 3 months, so no LLM can classify a historical burst | Direction comes from the theme's definition; tone, which is historical, is a reversal flag only (D90) |
| Tone is sentiment, and "transformer shortage" is negative tone but good news for the maker | Tone never sets a sign; supply tightness gets its own theme node, and the price effect waits for Phase 5 (D14) |
| Company-only attention cannot tell "famous" from "famous for this theme" | Written as a known limit; `--verify-attention` runs a paired check on the top ~10 |
| GDELT backfills past days | `coverage` is append-only with `recorded_at`; readers take the latest row at or before `known_at` |
| Thin themes produce meaningless statistics | Poisson surprise instead of a Gaussian z, plus a 5-article floor; M26 records which themes are too thin |
| A theme query is imprecise, so the burst is about something else | M26 measures per-theme precision against an 85% bar before M29 judges anything |
| Tuning queries against the event set would be lookahead | Queries are tuned on article relevance only, and the event set is committed in M23 before any coverage is fetched |
| GDELT API drift or an outage | Every response is cached, tests use fixtures, and a shape check fails loudly |
| Disk, about 1.2 GB free | Aggregates only, gzipped, a few MB. No BERTopic, no embeddings, no local model |
| Codex allowance | Only query-relevance labels and theme proposals: hundreds of items, not the 7M tokens 1b used |

## Decisions

Numbering continues from `docs/phase-1.md`. D85 to D95 were taken in the Phase 2 planning
session (2026-09-27).

- **D85.** GDELT DOC 2.0 is the only signal source. Timeline modes reach back to 2017-01-01 and
  `ArtList` is capped at 3 months, which is what decides the direction design. Wikipedia
  pageviews, BigQuery GKG and EDGAR 8-K signals were considered and dropped: one client, one
  rate limit, one set of fixtures.
- **D86.** Coverage is stored as `matched` and `norm` per day in an append-only `coverage` table
  with `recorded_at`, inside the existing store. Bursts are derived, not stored.
- **D87.** A theme's GDELT query lives on its node, in one new optional `query` field, so the
  bitemporal store versions it and an `as_of` snapshot says which query produced a series.
  Company queries are built from label, aliases and ticker.
- **D88.** The signal and the exposure are reported side by side and never multiplied. A burst is
  a unit of surprise; exposure is per unit of demand shock; news volume is attention, not demand.
  There is no per-theme shock scale, which closes the Phase 0 open question by declining it.
- **D89.** The burst statistic is a Poisson surprise against a trailing median baseline, with a
  5-article floor and 2-day persistence. Ratio-to-median and a Gaussian z are reported alongside.
  This departs from PLAN §7's CUSUM or Kleinberg, which need more parameters than there is data to
  fix them on.
- **D90.** Direction is a property of the theme, not of the day. A burst means the theme is
  intensifying, and `DRIVES` polarity carries the winners and losers. Tone, standardized against
  the theme's own trailing tone, sets a `possible_reversal` flag and never flips a sign.
- **D91.** Attention is company-wide coverage share over 90 days, with the percentile taken
  inside each theme's exposed set. Theme-paired queries exist only as an opt-in check on the top
  ~10 (`--verify-attention`). The accepted cost is that a company famous for something else looks
  crowded.
- **D92.** `hide_obvious` uses the attention percentile (default 80), falling back to hop count
  when attention is missing. This retires D5 and D23.
- **D93.** No BERTopic and no embeddings: torch alone is larger than the free disk. Theme
  discovery is Codex proposing into the review queue, decided by the user like new products
  (D41, D67).
- **D94.** Phase 2 adds no MCP tool. `find_exposed` gains attention and novelty;
  `trending_themes` waits for Phase 3.
- **D95.** Phase 1's open gaps are carried, not closed: the universe stays at 61 companies, the
  `REQUIRES` judge is not re-run with graph structure, and only a product shortlist is accepted.
  Phase 2's bottleneck is themes, not companies.

- **D96.** M22 taxonomy: ten products accepted from the 1b proposals — processor IP, PCIe/CXL
  connectivity, flip-chip and SiP packaging, RF and high-speed analog, co-packaged optics,
  photonic ICs, optical circuit switches, battery energy storage, general-purpose servers and
  enterprise storage. Every borderline candidate was rejected: cloud computing services (it would
  invert the chain, D79), network routers, PCBs, DPUs and IPUs (its revenue is inside lines
  already mapped to accelerators), nuclear fuel and wind turbines. AMD's re-mapping reproposed
  FPGAs and adaptive SoCs; rejected, because AMD's Embedded segment is industrial, comms and
  automotive, outside the slice. Cummins reproposed generator alternators, already rejected in
  D67. Chain placement is 21 sourced `REQUIRES` edges in
  `data/seed/edges/phase2-products.yaml`.
- **D97.** Re-running `ripple map --force` with a larger taxonomy silently undid three earlier
  human rulings, so every re-mapped proposal is now diffed against the previous version before
  review. Constellation's `electricity-demand` assignments came back (removed again: a company
  cannot produce a demand node, D60); Generac's residential standby generators came back and its
  C&I fraction rose to 0.85 (restored to excluded and 0.6, D79); Broadcom gained `rf-analog`
  (removed: its RF revenue is smartphone filters, while the node is anchored on optical drivers
  and TIAs, so the edge would invent an AI-optics path). Cummins and Super Micro were rejected
  again for their M13 reasons. Re-running the same prompt on the same filing also moved several
  accepted splits by 2 to 5 points of revenue share (Vertiv air cooling 0.177 to 0.201, Eaton
  switchgear 0.207 to 0.244, Fabrinet transceivers 0.70 to 0.75), which puts a number on how soft
  the 44 Codex splits behind the 1a weights are.
- **D98.** Four products had no XBRL line to map, so their producers are hand-written seed edges
  (`data/seed/edges/phase2-producers.yaml`). Lumentum's optical circuit switches propagate at a
  filing-derived 0.015 ($10.0M of a $665.5M quarter, backlog above $400M). GE Vernova's battery
  storage is a minor bucket inside a $9.64B Electrification segment. Broadcom's and Coherent's
  co-packaged optics and Coherent's photonic ICs sit at confidence 0.3, stored as evidence and
  not propagated, because the revenue is reported inside lines already mapped to switches,
  transceivers and lasers, so a propagating weight would count the same dollars twice (the D4
  principle). Consequence for M23: a CPO share-shift theme will price the loser side (pluggable
  transceivers) properly and reach no company on the winner side until someone discloses a
  number. That is the honest answer, not a bug.
- **D99.** A `REQUIRES` weight is a market-level demand share applied uniformly to every producer
  of that node, so the graph cannot express that one producer sells only into the AI slice of a
  mixed market. A pure play is under-credited and a broad-market supplier over-credited. Surfaced
  by `ai-servers REQUIRES pcie-cxl-connectivity` at 0.8, where Astera Labs (revenue share 1.0)
  becomes the top AI-compute name at 0.64, ahead of Nvidia at 0.51. Kept at 0.8: the ordering
  comes from revenue concentration, which is what PLAN §3 intends, and Astera is 98.6% of the
  node's mapped revenue so the uniform share costs nothing here. Splitting a node when a second
  producer with a different end-market mix arrives is the fix, and it is not needed yet.

- **D100.** M23 themes: 15 in total, the 4 from Phase 0 plus `ai-memory-demand`,
  `semicap-spending`, `china-export-controls`, `grid-buildout`, `energy-storage-deployment`,
  `nuclear-for-datacenters`, `optical-interconnect-demand`, `arm-datacenter-adoption`,
  `cpo-adoption`, `ocs-adoption` and `ai-server-budget-shift`. Two notes on what the taxonomy
  could not express. An Arm-versus-x86 share shift is impossible, because `server-cpus` is a
  single node covering both, so Arm adoption is a volume theme driving `processor-ip` instead.
  And supply-tightness themes stay out under D14, since a shortage moves prices rather than
  volumes; `china-export-controls` is modelled as a volume theme with **negative** `DRIVES`
  edges, which E10 and E11 both allow.
- **D101.** The event set (`tests/golden/events.yaml`) holds 16 events over 2022 to 2025, each
  naming the theme that should burst and the direction the theme is really moving. Three are
  reversals (Blackwell delay reports, DeepSeek, the lease-cancellation reports): coverage spikes
  while the theme weakens, which is what the tone flag exists to catch. Five dates are marked
  `approximate` because they were recalled rather than checked, and M29 reports those separately
  so a wrong date is not scored as a detector failure. The file was written before any GDELT
  request was made for a theme series.
- **D102.** M24 spike: `TimelineVolRaw` returned 3,525 daily points for 2017-01-01 to 2026-09-27
  in a single request, with `value` and `norm` as documented, so one request per series carries
  all of history and a full pass is about 76 requests rather than one per day. Two facts about
  the rate limiter cost an hour to learn. First, a refusal can arrive as HTTP 200 whose body is
  plain text rather than JSON, so status codes alone are not enough. Second, **retrying into a
  refusal extends the penalty**: a backoff loop kept the block alive for most of an hour, while a
  single 90-second gap with no requests at all cleared it immediately. The client therefore makes
  one polite retry and then stops the batch cleanly, in the spirit of D74, and leans on the
  response cache so a rerun is nearly free. Pacing is 12 s. Company series skip the tone request,
  since tone only feeds the theme reversal flag.
- **D103.** Amends D89. The burst threshold is the Poisson surprise **divided by an
  overdispersion factor** (variance over mean of the trailing counts, floored at 1). A raw
  Poisson tail assumes variance equals mean, but coverage clusters, so on a busy theme it would
  fire constantly. For a thin theme the factor is about 1 and the correction does nothing, which
  is exactly where Poisson is the right model. Tested both ways: the same relative jump bursts on
  a steady series and does not on a noisy one.
- **D104.** Query precision (M26) is measured on **article relevance only**, never on whether a
  burst lines up with the event set. Tuning a query against burst alignment would turn the M29
  measurement into lookahead, since the event set was fixed first. Judged headlines are sent to
  Codex but never written to disk; the stored record is the URL, the verdict and the judge's
  own-words reason, the same rule spans follow (D72).
- **D105.** Attention is read from the store with the same `known_at` bound as the graph, so a
  backtest sees the attention that was measurable on the day rather than a later GDELT backfill.
  A company with fewer than 30 days in the window has **unknown** attention, not zero, and
  `ripple exposed --by-novelty` sorts unknowns last: missing data must never look like a
  discovery.

- **D106.** Amends D103. A theme silent on most days has a median daily share of exactly 0, which
  made `expected` zero, the ratio infinite and the surprise pin to its cap: on the first real series
  that turned one matched article into a burst scoring 109. The baseline now uses the median
  whenever it implies at least half an article on a typical day, and otherwise a pooled rate over
  the window with half an article added. Taking the *larger* of the two was tried first and
  rejected, because a single spike inside the window then dragged the pooled rate, and with it the
  baseline, upwards, which is exactly what the median was there to prevent. `expected` is floored at
  half an article, so the zero branch is gone.
- **D107.** The first real series (`ai-compute-demand`, 1,703 days) detects **0 of 5** of its
  pre-registered events, for two separate reasons that need separate fixes. The query has almost no
  recall — one matched article on the day ChatGPT launched — so it measures a narrow trade phrase
  rather than the theme; and `MIN_SURPRISE = 6.0`, chosen a priori, is far too strict once the
  overdispersion correction divides by 6 to 13 on a noisy series. **Neither may be tuned against the
  event set** (D101, D104). The threshold is to be calibrated against a target burst frequency,
  using only the coverage series, and the queries rebuilt on face validity. This also exposes a gap
  in the exit criteria: they bar query *precision* but never measure *recall*, and recall is what
  failed here.

- **D108.** All 15 theme queries were rebuilt for recall (user decision, 2026-09-27): broader
  vocabulary covering how the press wrote about each theme across 2022 to 2026, including named
  products where they identify the theme (TPU, Trainium, Graviton, HBM4). Drafted from vocabulary
  reasoning and deliberately **not** checked against the event dates, so recall comes from drafting
  and precision from M26's measurement. Changing a query changes its `query_hash` and therefore
  every series it produced, so all 15 series must be re-fetched; that is by design (D87).
- **D109.** The burst threshold is chosen by `ripple calibrate`, which picks the value giving about
  four bursts per theme-year from the coverage series alone (user decision, 2026-09-27). On the
  first real series that value is **1.00** against the a priori `MIN_SURPRISE = 6.0`, which produces
  zero bursts — a six-fold miscalibration. `MIN_SURPRISE` is left at 6.0 in code until the
  re-fetched series give a calibration across all 15 themes rather than one. Measuring the event set
  *after* a blind calibration keeps it out-of-sample.
- **D110.** Open defect, carried to Phase 3 (`docs/phase-3.md`, M34): two events in the same theme within a few days merge into one
  burst window. Stargate (2025-01-21) and DeepSeek (2025-01-27) are six days apart on
  `ai-compute-demand`; the detector finds one burst, scores Stargate a hit and DeepSeek a miss at
  4 days. Neither the ±3-day tolerance nor the 2-day persistence rule separates them, and a
  reversal hiding inside a positive burst is exactly the case the tone flag was meant to catch.

## Open questions

- Does `TimelineTone` cover the same 2017-to-now range as `TimelineVolRaw`, at daily resolution?
  The M24 spike answers it. If tone is shorter, direction for older events falls back to the
  theme definition alone and the reversal flag is reported only for recent bursts.
- How far do company-only and theme-paired attention diverge? M27's `--verify-attention` sample
  gives the first number; Phase 3's M34 measures it and records a decision if it is large.
- How are 10-Ks refreshed each year (carried from Phase 1)? A new valid-time version per fiscal
  year, possibly on a schedule, once the fetch loop here shows what scheduling looks like.
