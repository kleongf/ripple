# Phase 4: Evaluation

## Goal

Find out whether the rankings carry information. `PLAN.md` §12: **done when you know whether
novelty-ranked companies beat the baselines, and by how much.**

Phases 0 to 3 built a graph that ranks companies by exposure to a theme, a signal that says when
a theme is moving, and a novelty score that discounts companies the news already covers. None of
that has been checked against what markets did afterwards. Phase 4 does that, and the answer is
allowed to be "no" or "inconclusive".

**For now the test is backward only (D131).** It scores 2022 to 2026 bursts against returns that
already happened. A forward test, which freezes rankings as bursts fire and scores them later,
is deferred. Everything below serves the backward test, and its result carries the bias stated in
the next section.

**Size:** large (PLAN §12), about five sessions from here. Work in order, tick tasks here, and
record decisions at the bottom.

**Prerequisites:** Phase 3's M34 finished its theme work (14 of 15 series, threshold 1.75, D129).
The company coverage series are still being fetched; they feed attention, and without attention
novelty is unknown. M37 and M38 do not need them. M40 does.

## What the backward test can and cannot say

`PLAN.md` §9 wants each burst ranked with the graph as recorded on its date. Two facts rule that
out.

1. **There is no point-in-time graph for the past.** Every edge was recorded on 2026-09-27 or
   later, and nearly every `valid_from` is in 2025. A snapshot as of a 2023 date is almost empty.
   So every burst is ranked with **today's graph**, and only attention and novelty are measured
   as of the burst's own day (D137).
2. **The universe was chosen with hindsight.** All 61 companies were picked in 2026 as today's
   AI data-center value chain.

What survives is a **relative** question. For the same burst, the same companies and the same
returns, does ranking by novelty line up with outcomes better than ranking by exposure alone?
Both rankings share the same hindsight graph and universe, so the comparison between them is
fairer than either ranking's absolute performance. It is still not clean, because the graph's
exposures were written knowing which companies became AI names. The review states the result
under that caveat and never as proof.

## Scope

**In scope**

- Daily adjusted prices and three FX series, in the existing store (M36, M37)
- Buy-and-hold abnormal returns in USD against the universe and against each market's index
- A pre-registered analysis plan, committed and approved before any real return is computed
- The backward study over every eligible burst, with its bias stated
- The priced-in check (PLAN §8, moved from Phase 3)

**Out of scope**

- **The forward test (deferred, D131).** The `predictions` ledger, the daily job and
  forward scoring. The design stays here under "Deferred" so it can be picked up unchanged.
- Transaction costs, position sizing and trading simulation. Ripple is a research tool that
  surfaces leads (PLAN §9), so the question is whether rankings carry information, not whether a
  strategy is profitable.
- Rebuilding past graphs from historical filings, an open question for after M40.
- Graph-quality labelling (PLAN §9), carried from Phase 1.
- Magnitude and per-theme shock sizes (Phase 5).

## Design

### Which bursts are scored

- **Bursts:** every burst at the threshold of record, `MIN_SURPRISE = 1.75` (D129), on themes
  with a coverage series.
- **Detection day:** the burst's start day plus `PERSISTENCE − 1`, which is when the
  persistence rule is first met. It is a UTC date.
- **Eligible companies:** a burst is scored only if at least **5** exposed companies have a
  known novelty and prices across the window (D132). Fewer, and the burst is recorded as
  ineligible with the reason. On today's graph that rules out Arm adoption, energy storage,
  nuclear, liquid cooling and China export controls, which have 1 to 4 exposed companies.
- **No overlap within a theme (D136):** a burst whose detection day falls inside the 60-day
  window of an earlier scored burst on the same theme is recorded but not scored. Scored bursts
  then never share return days within a theme.
- **Reversal-flagged bursts are included (D136):** tone never sets direction (D90). The flagged
  subset is also reported separately.
- **Room for the long window:** a burst needs a full 60-day window before the last price
  (2026-09-25), or it is recorded as "window incomplete".

### Timing and the calendar

- **Master calendar:** the US trading calendar, from `^GSPC`'s trading days. A horizon of h
  days means h US trading days.
- **Entry:** for each listing, its first close dated **after** the detection day. The detection
  day's coverage is only complete once that UTC day ends, so no close on it could have acted on
  the burst.
- **Exit:** the listing's last close on or before the master calendar's entry date plus h
  trading days, for h = 20 and h = 60.
- **Exclusions:** a listing with no close within 5 calendar days after the detection day (not
  yet listed, or suspended) is excluded from that burst, as is one with no close within 5 days
  before the exit. A missing price is never a zero return.

### Returns (D134, D135)

- **Currency: USD.** A non-US close is converted at the FX close on the same date: `JPY=X`,
  `KRW=X` and `EURUSD=X`, fetched with the price client and stored in `prices` like any listing.
  The yen moved about 25% against the dollar over 2022 to 2024; left in local currency, that
  would swamp the Japanese names' results.
- **Buy-and-hold return:** exit ÷ entry − 1 on adjusted closes in USD, which gives the total
  return.
- **Primary abnormal return:** the stock's buy-and-hold return minus the equal-weighted mean
  buy-and-hold return of every universe listing with valid entry and exit prices for the burst,
  the stock included. This removes AI-chain beta.
- **Secondary abnormal return:** the stock's local-currency buy-and-hold return minus its market
  index's (`^GSPC`, `^N225`, `^KS11`, `^GDAXI`, `^FCHI`) over the same dates, in the same
  currency. Reported, never decisive.
- **Sign adjustment (D133):** every abnormal return is multiplied by the sign of the company's
  exposure to the burst's theme. A loser on a share-shift theme that falls then counts as a
  correct call.

### The statistic (D132)

Per scored burst, over its eligible companies:

- **IC(novelty):** the Spearman rank correlation between novelty magnitude and the
  sign-adjusted 60-day abnormal return.
- **IC(exposure):** the same with |exposure|.
- **Primary number:** the mean over scored bursts of **IC(novelty) − IC(exposure)**. Positive
  means discounting crowded names improved the ranking.

Uncertainty and verdict:

- **Interval:** a two-sided 95% block bootstrap, 10,000 draws. Blocks are calendar months of
  the detection day, so bursts on different themes in the same month move together.
- **Verdict, fixed in advance:**
  - **Beats:** the whole interval is above 0.
  - **Worse:** the whole interval is below 0.
  - **Inconclusive:** otherwise, or with **fewer than 30 scored bursts**.

**Secondary results** are reported and never decide:

- The same at 20 days.
- IC(novelty) and IC(exposure) on their own.
- Five-name portfolios where a burst has at least 10 eligible companies: novelty top 5,
  exposure top 5, most covered top 5, all eligible, and random (10,000 draws).
- The reversal-flagged subset.
- The market-index version.

### No parameter is fit to returns (D126)

The threshold is calibrated from coverage alone (D109, D129). The novelty formula (D91), the
eligibility floor, the horizons, the statistic and the bootstrap are all fixed in
`tests/golden/evaluation.yaml` before any real return is computed. Changing one after returns
are seen voids the result, and the review has to say so.

### Novelty as of the burst (D137)

- Exposure comes from today's graph: `as_of` and `known_at` are both today, because a snapshot
  as of a past date is nearly empty.
- Attention comes from company coverage in the 90 days before the detection day. Its
  percentile, and so novelty, is taken within the burst's exposed set, as `score` already does.
- `score()` gains an `attention_as_of` date, separate from the graph's `as_of`. This replaces
  the earlier plan to pass `known_at` to the snapshot, which would still have given an empty
  graph for past dates because of `valid_from`.
- Coverage is read with `known_at` = today. GDELT's counts for past days were recorded in 2026,
  and backfill makes little difference to them. The review says so.

### Priced-in check

PLAN §8: a company whose price already moved with the theme has less left to move. For each
eligible company and burst:

- Compute the Spearman correlation between the company's daily abnormal return and the theme's
  daily coverage share over the 60 trading days **before** the detection day.
- Report it next to novelty. It is never multiplied into novelty, in the spirit of D88.
- A secondary question for the review, not a parameter.

## Milestones

Numbering continues from `docs/phase-3.md`. Each milestone adds its modules to the CLAUDE.md
layout and its commands to the CLAUDE.md commands block as it lands.

### M36. Price source spike and store

- [x] Spike on 2026-09-28 against all 61 listings plus five country indices, 2022-01-01 to
      2026-09-27 (D128):
      - **Stooq: 0 usable.** Every request returned a JavaScript browser-verification page
        instead of CSV, and later requests timed out. It blocks automated clients.
      - **Yahoo chart endpoint: 61 of 61 listings and 5 of 5 indices.** One transient upstream
        error (4186.T) succeeded on retry. Currencies: USD, JPY, KRW and EUR. Gaps of 6 to 8
        days are exchange holidays (Golden Week, Chuseok), not missing data.
      - **Adjustment:** `close` is split-adjusted (Nvidia's close is ×1.007 across its 10:1 split
        on 2024-06-10). `adjclose` also adds dividends back (NextEra 0.83 on closes, 0.945
        adjusted, over the window), so returns on it are total returns.
      - **Late starts, all listings or spin-offs:** CEG 2022-01-19, CRDO 2022-01-27, ARM
        2023-09-14, ALAB 2024-03-20, GEV 2024-03-27, SNDK 2025-02-13.
- [x] `ripple/prices.py` (the client, response cache in `data/prices/`, local trading days from
      the exchange's UTC offset), the append-only `prices` table, and
      `ripple prices fetch [--listing] [--from] [--to] [--refetch]` and `ripple prices show`.
      Resumable: listings held through `--to` are skipped, and a failed listing is reported
      while the batch goes on
- [x] Recorded fixtures in `tests/fixtures/prices/`, trimmed from the spike: Nvidia across its
      split and a dividend, Tokyo Electron over Golden Week, Yahoo's not-found error and the
      plain-text upstream error. 21 tests, none touching the network
- [x] The listing-to-node map is each company node's `ticker`; each listing's market and index
      come from its suffix (none means a US listing, ADRs included)
- [x] First full fetch: 66 listings, 75,755 daily rows, no failures

**Done when** every universe company has a price series or a written reason why not. **Met:**
61 of 61.

### M37. Returns and benchmarks

Build the return machinery and nothing that looks at results. No real burst is scored in this
milestone.

- [x] FX series: `JPY=X`, `KRW=X` and `EURUSD=X` join `ripple prices fetch` (1,231 days each).
      The quote direction was checked against the source, not assumed: yen and won per dollar
      (divide), dollars per euro (multiply). The rule is `prices.FX_BY_CURRENCY`
- [x] `ripple/evaluate.py`, pure functions over `PriceRow`s and dates:
      - the master calendar and `window()`
      - `leg()`, which takes entry and exit closes under the five-day exclusion rule
      - `to_usd()`
      - the buy-and-hold return and the universe equal-weight benchmark in `outcomes()`
      - the market-index benchmark
      - `sign_adjust()`
- [x] `score()` gains `attention_as_of`. A mini-fixture test shows exposures unchanged while
      attention follows the date
- [x] `ripple evaluate window LISTING --detected DATE --horizon 60 [--theme ID]`
- [x] 15 tests with hand-computed answers:
      - a flat universe gives zero abnormal return
      - one stock up 10% of four gives +7.5% abnormal
      - a stock listed mid-window is excluded
      - entry is strictly after the detection day
      - a US holiday keeps h trading days
      - a Tokyo holiday moves entry to the next Tokyo close
      - a 10% weaker yen gives −9.09% in USD
      - a stronger euro gives +10% in USD
      - missing FX excludes the listing
      - the local index comparison
      - sign adjustment
      - attention as of a day
      - the window command's output
- [x] One real window checked by hand, the only real return computed before pre-registration,
      as this milestone required. Nvidia after the GTC 2024 burst (detected 2024-03-20), 60
      days to 2024-06-17 (Memorial Day skipped), gives +43.26% in USD. Against the S&P 500 that
      is +38.84%; against the universe benchmark (+9.75% over 59 listings, GE Vernova and
      SanDisk excluded as not yet listed) it is **+33.51%**. Plain SQL with independently
      written FX conversion reproduced all three numbers exactly

**Done when** the synthetic cases pass and one real window is checked by hand against
`ripple evaluate window`. **Met.**

### M38. Pre-registration

- [x] `tests/golden/evaluation.yaml` fixes:
      - the threshold of record (1.75), the persistence rule and the detection day
      - the overlap, window and reversal rules
      - the graph and attention settings, and the eligibility floor (5)
      - the calendar, entry, exit and slack rules, and USD conversion with the three FX rules
      - buy-and-hold returns, the universe benchmark and sign adjustment
      - the primary statistic, its expected sign, ties (average ranks) and degenerate bursts
        (excluded and counted)
      - the bootstrap (95% two-sided, calendar-month blocks, 10,000 draws, seed 20260928), the
        verdict rule and the minimum N (30)
      - every secondary result
- [x] `tests/test_evaluation_plan.py`: the file loads, and every value the code also defines
      matches it (threshold, persistence, attention window and minimum days, confidence, hops,
      calendar listing, slack, FX rules, horizons)
- [x] **Approved by the user on 2026-09-29 and committed before any burst was scored.** The
      only real return computed before it is M37's single hand-check window

**Done when** the file is approved and committed, and `git log` shows it predating any real
return computation. **Met.**

### M39. Forward ledger: deferred (D131)

Not built for now. The design is kept under "Deferred" below, so it can be picked up without
re-deciding anything.

### M40. The backward study

Needs the company coverage series for attention. It runs once they are complete, or at least
complete for every company exposed to an eligible theme.

- [ ] `ripple evaluate run`: lists every burst at 1.75, applies eligibility, overlap and window
      rules (each exclusion counted with its reason), and scores every eligible burst
- [ ] The primary number, its interval and the verdict, straight from the pre-registered rules,
      plus every secondary result
- [ ] Every table printed under "backward test: ranked with today's graph and universe"
- [ ] The counts in the review: bursts found, and bursts excluded as ineligible, overlapping or
      incomplete, by theme

**Done when** the numbers and the verdict are in `docs/phase-4-review.md` under a heading that
says how they are biased.

### M41. Priced-in check

- [ ] The pre-burst correlation in `ripple/evaluate.py`, reported per company and burst in the
      study's output
- [ ] A column on `ripple exposed` and an optional `priced_in` field on `find_exposed`, computed
      as of today
- [ ] A secondary question in the review: do low-priced-in names score higher? It is reported
      and not tuned

**Done when** the check is computed for every scored burst and reported.

### M42. Review

- [ ] `docs/phase-4-review.md`: the verdict under its bias statement, every secondary result,
      exclusions by reason, and what would make the result trustworthy: the forward test, and a
      point-in-time company layer
- [ ] A decision on the open questions: forward test and historical filings

**Done when** the exit criteria are met, or the review explains why not.

## Exit criteria

| Criterion | Measured by |
|---|---|
| Every universe company has prices or a written reason why not | M36: met |
| Returns match hand-computed synthetic cases, and one real window is checked by hand | M37 |
| The analysis plan is approved and committed before any real return is computed | `git log` on `tests/golden/evaluation.yaml` |
| The primary number, its 95% interval and the pre-registered verdict (or "inconclusive" below 30 scored bursts) | M40 |
| Every result is stated under its bias: today's graph and a 2026 universe | M40, M42 |
| All tests pass offline, the mini fixture is unchanged, the four golden files pass | `uv run pytest` |

## Risks

| Risk | Mitigation |
|---|---|
| The backward result is read as proof | Every table carries the bias statement; the review says what would make it trustworthy (D131) |
| The comparison is still contaminated: exposures were written knowing the outcome | Only the relative question (novelty against exposure, same graph) is primary; absolute returns are secondary |
| Too few eligible bursts | The floor is 5 companies, not 10; below 30 scored bursts the verdict is "inconclusive", written in advance |
| Bursts cluster in time and co-move | No overlap within a theme; the bootstrap resamples calendar months |
| FX conversion errors (quote direction) | A synthetic test per currency; the FX rule is explicit about direction |
| Missing prices and late listings | Excluded with a counted reason, never zero-filled |
| A result tempts a parameter change | Everything fixed in the approved pre-registration; a change voids the result and is stated (D126) |
| The price source changes | Cached, with `source` recorded on every row (D128) |

## Deferred: the forward test (D131)

Kept so it can be picked up unchanged.

- An append-only `predictions` table, holding the rankings frozen on each burst's detection
  day with the threshold and `known_at` in force.
- `ripple evaluate freeze` and `ripple evaluate score`.
- A daily local job (launchd, which runs a missed job on the next wake) that refreshes the 15
  theme series, freezes new bursts, fetches prices, and scores what has matured. Company coverage
  refreshes weekly.
- The same statistic and verdict rule as the backward test, with N counted from the first
  freeze.

## Decisions

Numbering continues from `docs/phase-3.md`. D123 to D128 were taken in the Phase 4 planning
session; D131 to D138 in the milestone-planning session (both 2026-09-28).

- **D123.** *(Amended by D131.)* Phase 4 runs a forward test and a retrospective study, and only
  the forward test decides.
- **D124.** *(Detail in D134.)* The primary abnormal return is against the equal-weighted
  universe, not the market. It removes AI-chain beta. The market-index return is reported
  alongside.
- **D125.** *(Amended by D132.)* k = 5 names per portfolio, with five portfolios.
- **D126.** No parameter is fit to returns. Everything is fixed in `tests/golden/evaluation.yaml`
  before any return is computed. A change after returns are seen voids the result, and the review
  says so.
- **D127.** The retrospective study ranks past bursts with today's graph and today's universe,
  and every table it produces says so.
- **D128.** The price source is Yahoo's chart endpoint, chosen by the M36 spike (user decision:
  spike first). It covered 61 of 61 listings and all five indices from 2022, with split-adjusted
  closes and dividend-adjusted closes. Stooq answered every request with a browser-verification
  challenge, and that is not worked around. The endpoint is unofficial, with no published
  terms for programmatic use, so it is used at research volume (about 66 requests for a full
  refresh), every response is cached, and every stored row records its `source`. A licensed API
  (Tiingo, EODHD) is the replacement if it changes or refuses; a new source gets its own
  `source` value, so series are never mixed. The client calls the endpoint directly over
  `httpx`, the way the GDELT client does, rather than adding `yfinance` as a dependency.
- **D131.** Phase 4 is a **backward test only, for now** (user decision). It scores past bursts
  against realized returns. The forward test is deferred, with its design kept. The backward
  result is Phase 4's answer, stated under its bias: today's graph and a 2026 universe. Only the
  relative comparison (novelty against exposure, on the same graph and companies) is primary.
- **D132.** The primary statistic is a rank correlation, not a portfolio return (user decision).
  Only 2 of 15 themes expose 40 or more companies, and nine expose 11 or fewer, so five-name top
  and bottom portfolios would coincide on most themes. Per burst, it takes the Spearman
  correlation of novelty and of |exposure| with sign-adjusted 60-day abnormal returns across
  eligible companies (at least 5 with known novelty and prices). The primary number is the mean
  of IC(novelty) − IC(exposure), with a two-sided 95% block bootstrap over calendar months. The
  verdict is "beats" if the whole interval is above 0, "worse" if it is below, and otherwise
  "inconclusive", which is also the verdict with fewer than 30 scored bursts. Five-name
  portfolios remain a secondary result where a burst has at least 10 eligible companies.
- **D133.** Returns are sign-adjusted by the company's exposure sign (user decision), and
  rankings use magnitudes. That lets share-shift losers, and themes like co-packaged optics
  whose exposed companies are all losers, be tested.
- **D134.** Returns are in USD (user decision). Non-US closes are converted at same-day FX closes
  (`JPY=X`, `KRW=X`, `EURUSD=X`) from the price client. The universe benchmark is the
  equal-weighted USD return of every listing with valid prices for the burst, the stock
  included. The market-index comparison stays in local currency.
- **D135.** Abnormal returns are buy-and-hold differences, not summed daily differences (user
  decision). They match what holding would have returned over 60 days.
- **D136.** Within a theme, a burst inside an earlier scored burst's 60-day window is recorded
  but not scored (user decision), so scored bursts do not share return days. Reversal-flagged
  bursts are scored like the rest and also reported as a subset (user decision), because the flag
  has caught 0 of 1 real reversals so far.
- **D137.** The backward test ranks with today's graph (`as_of` and `known_at` = today), because
  nearly every edge's `valid_from` is in 2025 and a past snapshot is nearly empty. Attention and
  novelty are measured as of each burst's detection day, through a new `attention_as_of`
  argument to `score()`. This replaces M37's earlier plan to pass `known_at` to the snapshot.
- **D138.** The timing rules: a US-trading-day master calendar; entry at each listing's first
  close strictly after the UTC detection day; exit at its last close on or before the master
  date h trading days later; a listing with no close within 5 calendar days of either end is
  excluded for that burst.

## Open questions

- Once M40 shows whether the backward numbers are interesting, is it worth rebuilding the
  company layer from historical filings for a point-in-time version of at least that layer?
- When should the forward test start? It needs the daily job and months of data; M42 records a
  recommendation.
- Do the company coverage series finish in time for M40, or should M40 run on the companies of
  eligible themes first?
