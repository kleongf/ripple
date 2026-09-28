# Phase 3: Scoring and the full MCP surface

## Goal

Make Ripple usable by a model with no other help. `PLAN.md` §12 sets the bar: **a model using
only the MCP tools can write a sourced thematic brief.**

Phase 0 built the graph, propagation, paths and two MCP tools (`search_entities`,
`find_exposed`). Phase 1 anchored the weights in filings. Phase 2 added coverage, bursts,
attention and novelty. Three things still stand between that and a sourced brief:

- **Four of the six §10 tools are missing.** A model can rank companies but cannot open a path's
  evidence, profile a company or ask what is moving. Everything it would need already exists in
  the library or the CLI, except the company profile.
- **No evidence has been checked by a person.** The store holds 432 evidence items (394 on the
  388 winning edge rows, the rest on rows that lost precedence), and none is marked `verified`. A brief that cites them is "sourced" only in the sense
  that a URL is attached.
- **Signals are not ready to serve.** Only one of the 15 theme series is stored, and it was
  fetched with a query that D108 has since replaced. `MIN_SURPRISE` is still the a priori 6.0,
  which gives zero bursts on real data (D109), so a `trending_themes` tool would return nothing.

Propagation, scoring and paths do not change. This phase puts them behind tools, checks the
sources those tools cite, and tests the result by having a model write briefs.

**Size:** medium (PLAN §12), about six sessions of code plus the user's verification time.
Each milestone is one Claude Code session. Work in order, tick tasks here, and record decisions
at the bottom.

**Done when** (PLAN §12) a model using only the MCP tools can write a sourced thematic brief.
Here that means three Codex briefs whose citations all resolve and which the user passes against
a rubric written before any brief was generated.

## Scope

**In scope**

- `explain_link`, `get_evidence`, `company_profile` and `trending_themes`, completing PLAN §10
- `find_exposed` on a product as well as a theme
- One result envelope for every tool: `as_of`, confidence, rounded scores, notes
- `ripple/profile.py`, the one genuinely new aggregation
- A ruling on every evidence item in the store, recorded in a ledger the loader applies
- Finishing Phase 2's live measurements, calibrating the threshold and fixing D110
- A citation checker and a pre-registered brief rubric, then the acceptance run

**Out of scope** (later phases, or deliberately not built)

- The priced-in check (PLAN §8), which needs price history: Phase 4 with the rest of the
  evaluation
- The local UI: an off-roadmap tool with its own plan. It can reuse `profile.py` later, and
  nothing in Phase 3 waits for it
- M28 theme proposals, which stay deferred as Phase 2 left them
- Splitting nodes for D99, a second universe growth hop, and re-judging `REQUIRES` (D95)
- Theme-paired attention as the default. M34 measures the divergence and records a decision;
  it does not build paired attention automatically

## Carried from Phase 2

Phase 3 starts before Phase 2's live measurements are in (D111). These items are carried and
closed in M34:

| Item | State at the start of Phase 3 |
|---|---|
| 15 theme series | 1 held (`ai-compute-demand`, 1,703 days), fetched with a superseded query |
| 61 company series | none, so attention and novelty are null and `hide_obvious` falls back to hops |
| M26 query precision | command built, never measured |
| M29 event hit rate | 1 of 6 on the one series held, at the calibrated threshold |
| M27 novelty ranking read by hand | needs company series |
| `MIN_SURPRISE` | 6.0 in code, 1.00 from `ripple calibrate` on one series (D109) |
| D110 merged bursts | open |
| M23 approval of the theme list and event set | not recorded |

Everything on the graph side (M30 to M33) can proceed without them.

## Design

### Tool surface

The server stays a thin wrapper (PLAN §10). Every number comes from a library function the CLI
also uses, so the two cannot drift.

| Tool | Inputs | Wraps | New code |
|---|---|---|---|
| `search_entities` | query, type, limit | `search.search_entities` | none |
| `find_exposed` | theme **or product** ID, direction, max_hops, hide_obvious, as_of, limit, min_confidence | `score.score` | accept product nodes |
| `explain_link` | from_id, to_id, k, max_hops, as_of, min_confidence | `paths.top_paths` | per-edge evidence summary |
| `get_evidence` | edge_id, as_of | `Store.snapshot` (`sources`, `alternatives`) | serialization |
| `company_profile` | company_id, as_of, min_confidence | `profile.company_profile` | **new module** |
| `trending_themes` | window, min_surprise, as_of | `signal.bursts` | theme kind and direction alongside |

**The envelope.** Every tool returns `as_of`, `min_confidence` where the graph is read, rounded
scores and a `notes` list. A tool that reads coverage also returns `known_at`. Rounding follows
`CompanyScore.to_dict`. Errors are `ToolError`s that name the tool to call next, the way
`find_exposed` points at `search_entities` today.

**`find_exposed` on a product.** `company_exposures` rejects any shock node that is not a theme
(`ripple/score.py:206`). The propagation itself does not care: a product is a row of `W` like
any other. The change allows theme and product nodes and keeps rejecting companies, regions and
materials. `theme_kind` becomes `node_type` plus `kind`. The mini fixture gets a product-shock
test with a hand-computed answer. A product shock ranks the product's producers and everything
upstream of it, but not the product's customers, because demand does not flow that way. The
tool description says so.

**`get_evidence` and `explain_link` show their weakness.** Each edge carries `weight_source`,
confidence, the source layer that won (D63 precedence) and the losing `alternatives`. Each
evidence item carries URL, access date, note, span offsets where they exist, and its
verification ruling. An unverified item says so rather than being hidden.

**`company_profile`** returns:

- revenue mix from `PRODUCES` edges, with weights and `weight_source`
- `SUPPLIES`, `SUBSIDIARY_OF` and `EXPOSED_TO` neighbours
- exposure to each theme, listed per theme with its top path. Exposures are **never summed
  across themes** (D9), and the tool says so
- attention and its date, or null when unknown (D105)

The last item scores every theme against one graph, which takes 15 propagations on a few hundred
nodes. That is cheap, and it is built once, in `profile.py`.

**`trending_themes`** returns, per burst: theme, window, peak day, peak surprise, peak ratio,
matched articles, `tone_flag`, the theme's `kind`, and the `query_hash` that produced the series.
Direction is the theme's definition, not a classification of the day (D90). The tool never
makes a GDELT request and returns no articles (D112). Themes with no series are listed as
unmeasured, so silence is never mistaken for calm.

### Evidence verification

The unit of work is the **document**, not the item. The 432 items point at 123 distinct URLs, and
many edges cite the same 10-K. Rows that lost precedence are counted too: verifying one can make
it win (D63), so it needs a ruling like any other.

| Source layer | Items | How each item is ruled on |
|---|---|---|
| `seed` | 171 | read by the user: does the page support the note and the weight? |
| `xbrl` | 250 | recomputed from the cached XBRL filing (all 250 match at the start of M33), plus one human check per filing that the right fact family was used |
| `llm` | 11 | read by the user against the stored span |

Every item still ends with a ruling: `verified`, `wrong-note`, `dead-link` or `unsupported`.
Rulings live in a ledger, `data/verified.yaml`, keyed by edge ID, URL and a hash of the note,
with a date. Keying on the note means a ruling lapses when the note changes. The
loader applies it (D113). The ledger is needed because `data/xbrl/` is generated and must not be
edited, so a flag written into a source file would be lost at the next `ripple review accept`.

**Verification changes precedence.** Under D63, a row with verified evidence ranks as
human-trusted and beats every unverified row for the same edge key (`model.rank_rows`). So a
verified seed bucket would now beat an unverified `xbrl` filing number. Rulings on edges that
have `alternatives` are therefore taken layer by layer, the filing layer first. After each sitting,
`ripple load` and the golden files are re-run, and any edge whose winning layer changed is listed.

A failed ruling is fixed where the fact lives: a corrected note, a replacement URL, lower
confidence, or a removed edge. The review lists every failure. A new validator warning, **W11**,
counts the propagating edges whose evidence is all unverified. It is one summary line rather than
one per edge, and it only fires once a ruling in the ledger applies to the graph being validated:
before verification starts it would fire on every edge and say nothing.

### The brief and how it is judged

The acceptance test has three parts, all fixed before any brief is generated (D114).

1. **Prompts and rubric.** Three prompts: `ai-compute-demand`, `datacenter-power-demand` and
   `liquid-cooling-adoption`, a share-shift theme with real losers. The rubric is written in
   `tests/golden/brief-rubric.md` before M35 runs. Its criteria:
   - every company claim cites a path
   - every path's edges resolve
   - bucket weights are disclosed as guesses
   - unverified evidence is disclosed
   - exposures are not added across themes
   - signal and exposure are not multiplied (D88)
   - `as_of` is stated
   - nothing is asserted that the tools did not return
2. **The writer.** Codex (gpt-6-astra, ChatGPT login) through `codex exec`, with the ripple MCP
   server as its only tool and shell and web search off. This extends `ripple/codex.py`, which
   otherwise disables all tools. The M35 spike confirmed it works, with one catch (D116).
3. **The checker.** `ripple check-brief FILE --theme ID --as-of DATE` extracts every node ID, edge ID, URL
   and exposure number in the brief. It resolves each one against the store as of that date.
   It reports what is unresolved, what does not match a tool's rounded output, and what cites
   unverified evidence. A brief passes the mechanical half at 100% resolved. The user grades
   the other half against the rubric.

## Milestones

Numbering continues from `docs/phase-2.md`. Each milestone adds its modules to the CLAUDE.md
layout and its commands to the CLAUDE.md commands block as it lands.

### M30. MCP groundwork

- [x] Read the current MCP Python SDK v2 docs (CLAUDE.md rule): `dict[str, ...]` returns are still
      structured content with no wrapper, and `ToolAnnotations` and `ToolError` are unchanged, so
      the existing pattern holds
- [x] One result envelope on every tool: `as_of` first, `notes` last as a list, `min_confidence`
      wherever the graph is read (`explain.envelope`)
- [x] `find_exposed` takes `node_id`: a theme, product or material (`score.SHOCKABLE`). Mini-fixture
      tests with hand-computed answers for accelerators and leading-edge logic; the existing mini
      numbers do not move. A product shock carries a note that it does not reach customers
- [x] In-process client tests: the six tools are listed and read-only, and every one returns the
      envelope against a temp store

**Done when** both existing tools return the envelope, a product shock ranks the right producers
on the mini fixture, and the four golden files still pass. **Met.**

### M31. `explain_link` and `get_evidence`

- [x] `explain_link` over `paths.top_paths`, in the new `ripple/explain.py`, which is also what
      `ripple explain --json` prints
- [x] `get_evidence(edge_id, as_of)`: the edge, its evidence with `verified` and span offsets, its
      source layer and its `alternatives`
- [x] `ripple evidence EDGE_ID [--json]`
- [x] Tests: an unknown edge ID, a sub-threshold edge (returned, `propagates: false`), an edge
      with an alternative from a losing layer, and an edge not yet valid on `as_of`

**Done when** every edge on every path `find_exposed` returns can be opened with `get_evidence`.
**Met.**

### M32. `company_profile`

- [x] `ripple/profile.py`: revenue mix with `mapped_share`, customers, suppliers, parents,
      subsidiaries, regions, per-theme exposure with rank and top path, attention
- [x] `company_profile` tool and `ripple profile COMPANY_ID [--json]`
- [x] Tests on the mini fixture: a company with no `PRODUCES` edge (empty mix, not an error), a
      company exposed to two themes (listed, never summed), and a company with no coverage
      (attention null)
- [x] The user reads `ripple profile company/vertiv` and agrees it is right (2026-09-28)

**Done when** `ripple profile company/vertiv` reads correctly to the user. **Met.**

### M33. Evidence verification

- [x] `ripple/verify.py`: the ledger `data/verified.yaml`, applied by `ripple load` and
      `ripple validate`, and validator warning W11. A test shows that a ruling feeds D63
      precedence and that `ripple load` reports the change of winning layer
- [x] `ripple verify status | next | rule | walk`: unruled items grouped by document, filing
      layer first; `rule --document URL` settles a whole filing at once; `walk` is interactive and
      saves after every document
- [x] `ripple verify recompute`: redoes every `xbrl` note's arithmetic from the cached filings.
      At the start: **250 of 250 match**, so the `xbrl` layer needs a per-filing human check of
      the fact family, not line-by-line reading
- [x] `ripple verify links`: a link-check pre-pass that records status only and rules on
      nothing. On 2026-09-28: 117 of 123 documents live; one genuine 404 (the networkworld
      server-memory article), two 403s and three timeouts that are probably servers refusing
      automated clients; report in `data/link-check.yaml`
- [ ] The user rules on every item in the store (432 at the start, across 123 documents), over
      several sittings
- [ ] Failures fixed at the source, or recorded with a reason
- [ ] Items added later in Phase 3 are ruled on before exit

**Done when** `ripple verify status` reports 0 left and every failure is fixed or listed in the
review.

### M34. Signals on the MCP surface

Gated on the Phase 2 fetch. The fetch now resumes across days and holds the store only while
appending (D117); run `ripple signals fetch --all --from 2022-01-01 --to 2026-09-27` until it
reports no `stopped:`.

- [ ] All 15 theme series and 61 company series in the store, fetched with the current queries.
      Check the store's query before trusting a run (CLAUDE.md). **10 of 76 held** on
      2026-09-28, all themes; three queries corrected first (D121)
- [ ] Phase 2's pending numbers measured and written into `docs/phase-2-review.md`: M26 query
      precision, M29 event hit rate, M27 novelty read by hand
- [ ] `MIN_SURPRISE` set from `ripple calibrate` across all 15 themes (D109), after the D115
      change, which moves every surprise
- [x] D110 fixed on synthetic series only, and the rule written down (D115) before `ripple events`
      is re-run: a trimmed dispersion estimate, and a split at an interior trough. Six new tests
- [x] `trending_themes` (`signal.trending`, `signal.trending_themes`) and its tests, including a
      theme with no series and point-in-time reading of coverage
- [ ] `ripple verify-attention` on the top 10 of three themes; the divergence recorded, and a
      decision on paired attention taken if it is large

**Done when** `trending_themes` over the last 90 days returns bursts at the calibrated threshold
across all 15 themes, and Phase 2's exit criteria are each met or explained.

### M35. Brief acceptance and review

- [x] `tests/golden/brief-rubric.md` and `tests/golden/brief-prompts.yaml`, written before any
      brief was generated, and committed in `1baf049` before any brief ran. **Approved by the
      user as written, 2026-09-28**
- [x] `ripple check-brief`, with tests on hand-written briefs: a bad edge ID, a mismatched
      number, a foreign URL, a sign error, an unknown node, precision as written, and a brief
      with no citations
- [x] Spike: `codex exec` with only the ripple MCP server, shell and web off. It works once
      `code_mode_host` stays enabled (D116); `CodexRunner.run_with_mcp` and `ripple brief THEME`
- [ ] Three briefs generated, checked and graded; kept in `docs/briefs/`. Waits for M34, so the
      briefs can use `trending_themes`, and for the rubric's approval
- [ ] `docs/phase-3-review.md`

**Done when** the exit criteria are met, or the review explains why not.

## Exit criteria

| Criterion | Measured by |
|---|---|
| All six PLAN §10 tools exist, are read-only, and return `as_of` and confidence | M30 client tests |
| Every evidence item in the store has a ruling; every failure is fixed or recorded | `ripple verify`: 0 unruled |
| `trending_themes` returns bursts at a calibrated threshold across all 15 themes | M34 |
| Three Codex briefs written with only the MCP tools, 100% of citations resolved | `ripple check-brief` |
| The user passes each brief against the pre-written rubric | M35 review |
| All tests pass offline, the mini fixture is unchanged, and the four golden files pass | `uv run pytest` |

## Risks

| Risk | Mitigation |
|---|---|
| GDELT's shared-IP limit keeps M34 blocked | M30 to M33 do not depend on it; the fetch resumes from its cache whenever the address is quiet (D102) |
| Verification is hours of reading | Grouped by document (123, not 432); `xbrl` notes are recomputed mechanically, so the user confirms a filing rather than re-reading every line |
| Verifying a bucket row lets it outrank a filing number (D63) | Rule filing-layer rows first on edges with `alternatives`; re-run `ripple load` and the golden files after each sitting and list every edge whose winner changed |
| A brief looks sourced but cites unverified or bucket edges | `check-brief` flags unverified citations; the rubric requires disclosing bucket weights |
| The writer invents facts the tools never returned | The writer has no web or shell; the checker resolves every ID and number against the store |
| Tuning D110's fix against the event set | The split rule is designed on synthetic series and written down before `ripple events` runs (D101) |
| Wire syndication inflates counts: one story republished by many outlets counts many times, a likely source of the overdispersion D103 corrects for | None possible from `TimelineVolRaw` daily counts, which cannot be deduplicated; a known limit, revisited only if M34's event measurement is poor (D119) |
| Codex cannot be restricted to one MCP server | Spike first in M35; if it fails, run the same prompts in a Claude Code session with only the ripple server, and record that change |
| Tool output grows too large for a model's context | `limit` on every list, `k` on paths, rounded numbers, IDs rather than prose |
| A product shock is read as a customer effect | The tool description says a product shock reaches producers and upstream inputs, not customers |

## Decisions

Numbering continues from `docs/phase-2.md`. D111 to D114 were taken in the Phase 3 planning
session (2026-09-28).

- **D111.** Phase 3 starts while Phase 2's live measurements are pending. The graph-side tools
  (M30 to M33) need no coverage. `trending_themes` and the acceptance run wait for the fetch. The
  carried items are listed above and closed in M34.
- **D112.** `trending_themes` takes `min_surprise`, not PLAN §10's `min_z`, because D89 made the
  Poisson surprise the threshold. It returns no sample articles and never makes a GDELT request:
  `ArtList` reaches back only 3 months, a live call from a tool would spend the shared-IP budget
  (D102), and article text is never stored (D104). Direction comes from the theme's definition
  (D90).
- **D113.** Every evidence item in the store gets a ruling before Phase 3 exits (user decision:
  all items, not only those on cited paths). Rulings live in a ledger, `data/verified.yaml`,
  keyed by edge ID and URL and applied at load. The generated `xbrl` source cannot hold flags
  across a regeneration. Work is grouped by document, and `xbrl` notes are recomputed from the
  cached facts before a human confirms them. Because verified evidence is human-trusted under
  D63, a ruling can change which layer's row wins an edge, so edges with `alternatives` are ruled
  filing layer first and every change of winner is reported.
- **D114.** The exit bar is judged by Codex with only the ripple MCP server, a citation checker
  that resolves every ID and number, and a rubric committed before any brief is generated. The
  three themes cover volume and share-shift kinds.

- **D115.** D110 fixed, designed on synthetic series only (D101). The review had called it a merge,
  but its own numbers say otherwise: DeepSeek's day was never flagged, and its nearest burst ended
  four days earlier. The mechanism is the D103 dispersion factor. Once a spike is more than three
  days old it sits in the trailing window, and variance over mean jumps about a hundredfold: on a
  synthetic series two identical 10x spikes eight days apart scored surprise 245 and 2.97. Two
  changes, both chosen a priori: (1) the dispersion estimate drops the largest 10% of counts in
  the window (`DISPERSION_TRIM`), about the share one or two recent 3 to 4 day bursts take of a
  90-day window, while chronic clustering covering far more of the window still shows; (2) a run
  of flagged days is split at its deepest interior trough when the trough falls to half the
  smaller peak on either side and both halves still meet the persistence rule
  (`SPLIT_FRACTION`). One Phase 2 test fixture moved with a written reason: the twin-event case
  needed its second event one day later, because the old factor had cut the 4-day burst's last
  day. Every surprise value changes, so the threshold must be recalibrated before `ripple events`
  runs.
- **D116.** Codex 0.155 routes MCP tool calls through its code-mode host. With `code_mode_host`
  disabled, as `ripple/codex.py` does for schema-constrained calls, a model given only the
  ripple server reports no tools. `run_with_mcp` therefore keeps that one feature on and
  disables every other tool feature, with web search off and a read-only sandbox. The spike
  called `search_entities` and returned `company/asml` with its `as_of`.
- **D117.** Two fetch defects found while restarting the Phase 2 pass. (1) The response cache key
  includes the end date, which defaulted to today, so a rerun on a later day missed every cached
  response; resuming now takes a fixed `--to`. (2) `signals fetch` held the store's write lock for
  its whole run, locking out `ripple load` and the MCP server for hours under GDELT's limit; it
  now opens the store only to read targets and to append each series, and skips series already
  held with the current query. A TLS timeout also crashed a round with a traceback; network
  failures now stop the batch cleanly like a refusal (`news.Unreachable`).
- **D118.** A product or material can be shocked as well as a theme (`score.SHOCKABLE`). The plan
  said products only; materials carry demand through `REQUIRES` exactly as products do, so there
  is no reason to refuse them. Companies and regions are outputs and stay refused. A product
  shock reaches producers and upstream inputs, never customers.

- **D119.** Google Trends' pipeline (sampling, filtering, normalization, peak-equals-100 scaling,
  "rising" and "breakout" terms) was reviewed as a model for spike detection and not adopted
  (user decision, 2026-09-28). Ripple already divides by total volume (share = matched / norm)
  and reports a ratio against the theme's own trailing baseline, which is what "rising" is; its
  article floor and `MIN_EXPECTED` guard against the near-zero "breakout" case (D106). Scaling to
  a 0-100 index is rejected: it discards the counts the Poisson surprise needs (5 against 1 is
  weaker evidence than 500 against 100) and makes every value depend on the chart window. The
  one transferable idea, dropping repeats, maps to wire syndication and cannot be done on daily
  counts. The detector stays as it is until `ripple events` gives a measured hit rate; any
  alternative must be compared on the same pre-registered set, never tuned against it (D101).

- **D120.** `Store.coverage` now reads only the query that produced the most recently recorded
  row (as of `known_at`). It used to take the newest row per day across all query hashes, so a
  series re-fetched with a new query kept the old query's days wherever the new fetch did not
  reach them: `ai-compute-demand` ended on a zero from the superseded query, found through the
  UI. This applies D87 to reading as well as writing. Calibration was also made about 80 times
  faster by computing day statistics once per series (`signal.bursts_from_stats`), with
  identical results.

- **D121.** GDELT rejects a query with a term under three characters ("The specified phrase is
  too short") as a plain-text 200, and the client cached that error as if it were a response, so
  `nuclear-for-datacenters` and `optical-interconnect-demand` could never be fetched. Only JSON
  is cached now. The two queries are corrected on face validity (`"AI"` becomes
  `"artificial intelligence"`, `"AI power"` and `"AI demand"`; `"1.6T"` becomes
  `"1.6 terabit"`), and `ai-server-budget-shift`'s bare `AI` gets the same treatment before
  its first request. None of the three had produced a series, so no measurement changes. A
  fetch round that met another process's write lock crashed; appends now retry for up to a
  minute (`_append_coverage`).

## Open questions

- Can `codex exec` run with one MCP server and nothing else? The M35 spike answers it.
- Should `company_profile` include `COMPETES_WITH` neighbours once any exist? None are stored yet.
- Does the D110 split rule change the calibrated threshold? If it does, re-run `ripple calibrate`
  after the split and before `ripple events`, and record both numbers.
- Is Google Trends worth adding as a second signal, measuring search interest rather than press
  coverage (D119)? Blocked today: the official API is an application-gated alpha and pytrends
  is archived (PLAN §5), and each request returns a resampled 0-100 index that would need
  stitching and anchoring.
- How are 10-Ks refreshed each year (carried from Phase 1 and Phase 2)? Once a year of filings
  rolls over, verification rulings keyed to an old URL will need carrying or re-ruling.
