# Phase 2 review

## M22 to M25, M27, M29: what was built, 2026-09-27

| Milestone | Result |
|---|---|
| M22 taxonomy top-up | 10 products accepted, 21 sourced `REQUIRES` edges, 23 companies re-mapped, 5 inert companies activated (D96 to D99) |
| M23 themes | 15 themes, all with GDELT queries; 21 `DRIVES` edges; validator warning W10; 16 pre-registered events (D100, D101) |
| M24 GDELT client | `ripple/news.py`, the append-only `coverage` table, `ripple signals fetch/show`, 17 offline tests (D102) |
| M25 burst detection | `ripple/signal.py`, Poisson surprise with an overdispersion correction, `ripple trending`, 18 synthetic tests (D103) |
| M26 query precision | `ripple/queries.py` and 7 tests; the measurement itself is pending (see below) |
| M27 attention | `ripple/attention.py`, novelty in `score` and `find_exposed`, `ripple attention`, `--by-novelty`, `--verify-attention`, 14 tests (D105) |
| M28 theme proposals | not built; the plan marks it cut-first, and it needs bursts from live data |
| M29 event runner | `ripple/events.py` and 14 tests; the measured hit rate is pending (see below) |

371 tests pass offline, ruff is clean, and all four golden ranking files still pass.

## The graph after M22

37 companies in the seed plus the Phase 1 additions; 51 products, materials and themes; 79
`REQUIRES`, 153 `PRODUCES`, 21 `DRIVES` from 15 themes, 162 `EXPOSED_TO` and 4 `SUPPLIES` edges.

Five companies that were inert now carry exposure: Arm (processor IP, 0.977 of revenue), Astera
Labs (PCIe/CXL, 1.0), Amkor (flip-chip packaging, 0.828), MACOM (RF and analog, 0.95) and Oracle
(general-purpose servers and enterprise storage, 0.018 between them).

### Rankings and stability after M22

AI compute demand, top 8: Astera Labs 0.64, Nvidia 0.51, Fabrinet 0.48, Super Micro 0.48,
Coherent 0.39, Lumentum 0.32, TSMC 0.31, Dell 0.31.

| Theme | Top-N Spearman (bucket / flat) | Overlap | End of 1b |
|---|---|---|---|
| ai-compute-demand | **0.800** / 0.673 (top 10) | 9 / 10 | 0.758 / 0.673 |
| custom-silicon-adoption | **0.903** / 0.915 (top 10) | 8 / 10 | 0.745 / 0.764 |
| datacenter-power-demand | 0.943 / 0.943 (top 6) | 6 / 6 | 1.000 / 1.000 |
| liquid-cooling-adoption | 1.000 / 1.000 (top 3) | 3 / 3 | 1.000 / 1.000 |

AI compute is back at the 0.8 bar and custom silicon improved sharply, because the new pure-play
nodes broke ties in a crowded band. Power slipped from 1.000 because GE Vernova's battery-storage
edge gives it a second path to the theme.

**Astera Labs outranking Nvidia is the model working as designed, not a bug.** The two sit on
identical demand-share hops from the theme (0.8 x 0.8); the ordering comes entirely from revenue
concentration, 1.00 against roughly 0.80. That is the grocery-versus-Amazon argument of PLAN
section 3 run forwards. It did surface a real structural limit, recorded as D99: a `REQUIRES`
weight is a market-level demand share applied uniformly to every producer of a node, so the graph
cannot say that one producer sells only into the AI slice of a mixed market.

## What GDELT actually gives, and what it cost to learn

`TimelineVolRaw` returns **3,525 daily points for 2017-01-01 to 2026-09-27 in a single request**,
with `value` (matched articles) and `norm` (all articles monitored). That is much better than the
plan assumed: one request per series carries all of history, so a full pass is about 76 requests
rather than one per series per day, and the 3-month `ArtList` cap only constrains M26 and M28.

Two things about the rate limiter cost most of an hour:

- **A refusal can be an HTTP 200** whose body is plain text asking you to slow down. Checking
  status codes alone silently treats it as a broken payload.
- **Retrying into a refusal extends the penalty.** A backoff loop kept the block alive for the
  better part of an hour; a single 90-second gap with no requests at all cleared it immediately.
  The client now makes one polite retry and then stops the batch cleanly, which is the D74 pattern
  from Phase 1, and the response cache makes the rerun nearly free.
- **The binding constraint is a shared IP, not our pacing.** Over one session roughly five
  requests succeeded in total while every *batch* was refused on its own first request, no matter
  how long the preceding gap — 90 s, 180 s or longer. GDELT's own documentation explains it: the
  5-second limit is **per IP**, and "cloud fetchers and AI tools share their outgoing IP addresses
  with many other users, so their GDELT calls are often refused with HTTP 429 before they even
  start". The budget is being spent by other traffic on the same address, so no interval we pick
  helps, and an isolated request only succeeds when the shared address happens to be quiet.

  This makes the **resumption loop the right design after all**, for a reason different from the
  one it was built for: each attempt is an independent draw on whether the shared IP is free, and
  the response cache means every success is kept. What it needs is many attempts over a long
  period, not longer sleeps. GDELT's suggested route for genuine high-volume use is its Web NGrams
  3.0 download, which is far too large for the 1.2 GB of free disk here (D93), so patient
  resumption is the only option open to this machine.
- **The tolerated rate is far below the published one.** GDELT documents one request per 5 s, but
  12 s spacing was refused on the first request of a batch while calls about 90 s apart succeeded
  every time. `MIN_INTERVAL` is therefore 90 s, which puts a full 76-series pass at a couple of
  hours of wall time.

## First real series, and what it revealed

The fetch completed one theme before GDELT's limiter stopped it: `ai-compute-demand`, 1,703 days
from 2022-01-01, 28,514 matching articles. That single series was enough to find two problems that
no synthetic test could have caught.

**A zero baseline made the detector report phantom bursts (D106).** The first run found three
"bursts" (2022-05, 2023-04, 2023-05). All three were artifacts: the theme is silent on most days in
2022, so the median daily share is exactly 0, `expected` is 0, the ratio is infinite and the
surprise is pinned to its cap. ChatGPT's launch day scored a surprise of 109 on **one** matched
article. The baseline now falls back to a half-article-smoothed pooled rate when the median is
unusable, and the phantom bursts are gone. Getting the fallback right took a second pass: taking
the larger of median and pooled defeated the median's whole purpose, because one spike inside the
window dragged the pooled rate up with it.

**After the fix, the detector finds nothing on this theme: 0 of 5 events.**

| Event | Matched | Expected | Ratio | Surprise |
|---|---|---|---|---|
| ChatGPT launch 2022-11-30 | 1 | 0.85 | 1.2x | 0.09 |
| Nvidia guidance 2023-05-24 | 7 | 0.90 | 7.7x | 1.70 |
| GTC Blackwell 2024-03-18 | 6 | 2.61 | 2.3x | 0.54 |
| Stargate 2025-01-21 | 39 | 14.49 | 2.7x | 1.18 |
| DeepSeek 2025-01-27 | 30 | 10.31 | 2.9x | 0.48 |

Threshold: 6.0. Two independent causes, and they need different fixes:

1. **The query has almost no recall.** One matched article on the day ChatGPT launched, 30 on the
   day of the DeepSeek selloff. The query measures a narrow trade-press phrase
   (`"AI data center"`, `"AI capex"`) that barely existed in 2022, not the events themselves. M26's
   precision bar cannot detect this: precision counts false positives, and this query's problem is
   the opposite. **The exit criterion is missing a recall measure**, which is a gap in the plan
   rather than in the code.
2. **`MIN_SURPRISE = 6.0` was chosen a priori and is far too strict for real series.** Genuine
   2.7x to 2.9x jumps score 0.5 to 1.2, because the overdispersion correction divides by 6 to 13 on
   a series this noisy. The correction is right in principle; its scale was never calibrated.

### Calibration, done blind, then measured

`ripple calibrate` picks the threshold from how often bursts should fire, reading only the coverage
series and never an event date (D107). On this one series the threshold giving about 4 bursts per
theme-year is **1.00**. The a priori 6.0 gives **zero** bursts — a six-fold miscalibration, now
measured rather than guessed.

Calibrating blind and *then* measuring is the legitimate order, so the run below is a genuine
out-of-sample test. At threshold 1.00, on the single theme that has data:

- **Stargate 2025-01-21: hit**, a burst at 7.6x the theme's normal.
- **DeepSeek 2025-01-27: missed by 4 days.** Its nearest burst is the Stargate burst six days
  earlier — the detector merged two distinct events into one window. Two events that close together
  in one theme are a case neither the tolerance nor the persistence rule handles, and a real design
  question for Phase 3.
- The other four ai-compute events miss: ChatGPT and GTC on query recall, Blackwell delay and the
  lease-cancellation reports on threshold.
- The tone reversal flag was correct on 100% of matched events, but only one reversal event
  matched, so that number rests on a single case.

The headline `ripple events` figure is **13% of 15**, but that is not a measurement of the
detector: 10 of the 15 events name themes with no coverage fetched at all, so they are automatic
misses. The honest statement is **1 of 6 events on the one theme with data**, and even that is on a
series fetched with the *old* narrow query — the rebuilt queries change every series, so all 15 need
re-fetching before any number is final.

**Neither may be fixed by tuning against the event set.** That file was pre-registered precisely so
this could not happen (D101). The legitimate route for the threshold is to calibrate it against a
target burst *frequency* — pick the value that yields roughly one burst per theme per quarter using
only the coverage series, never the event dates — and then re-measure. The query needs to be
rebuilt on face validity (does it return articles a human would call on-topic, and does it return
anything at all on days the theme obviously moved), which is a judgement about vocabulary, not
about the events.

## Pending: the measured numbers

Everything above is built and tested offline. Three numbers the exit criteria ask for need a
completed live pass and are **not yet measured**:

- the event hit rate (M29), which needs the 15 theme series;
- per-theme query precision (M26), which needs one `ArtList` request per theme plus a Codex batch;
- the novelty rankings read by hand (M27), which need the 61 company series.

Nothing about them is blocked on design or code: `ripple events`, `ripple trending` and
`ripple attention` run as soon as the coverage table is filled, and the fetch resumes from its
cache, so rerunning `ripple signals fetch --all --from 2022-01-01` until it stops reporting
`stopped:` is all that is required. At 90 s per request that is roughly two hours of wall time for
themes and companies together.

The event set was fixed before any theme series was fetched, so the measurement stays honest
whenever it is taken (D101).

## Corrections made during Phase 2

- **Re-mapping silently undid three human rulings (D97).** `ripple map --force` with a larger
  taxonomy brought back Constellation's `electricity-demand` assignments, Generac's residential
  generators and a Broadcom `rf-analog` edge. Caught by diffing every proposal against a backup
  rather than reading the new files; that diff is now the procedure. The same diff showed that
  re-running an identical prompt on an identical filing moves accepted splits by 2 to 5 points of
  revenue share, which puts a number on how soft the Phase 1a weights are.
- **A raw Poisson tail is the wrong threshold for busy themes (D103).** Coverage clusters, so its
  variance exceeds a Poisson's and the tail fires constantly. Dividing by an overdispersion factor
  estimated from the same trailing window fixes it and leaves thin themes untouched, which is
  where Poisson was the right model to begin with.
- **Missing attention must not read as novel (D105).** A company with no coverage series has
  unknown attention, not zero. `novelty` stays null, `hide_obvious` falls back to hop count, and
  `--by-novelty` sorts unknowns last.
- **Editing a query in YAML does not change what the CLI queries.** Every signals command reads
  the theme's query from the **store**, not from `data/seed/nodes/themes.yaml`, so rewriting the
  queries and then running `ripple query-precision` or `ripple signals fetch` silently measured the
  old ones. Caught by comparing the store's query against the file before trusting a run. Under a
  rate limit this is expensive: every request spent that way is unrecoverable for the rest of the
  day. **Run `ripple load` after touching any query, and check the store if a result looks stale.**
- **Broadening for recall introduced four precision hazards**, found by inspecting the queries
  rather than by measuring: bare `"Arm-based"` (matches unrelated senses of the word), bare
  `"optical switch"` (telecom switching that is not an OCS fabric), `TPU` (also thermoplastic
  polyurethane, common in materials news) and `SMR` (also shingled magnetic recording, which
  co-occurs with "data center"). All four narrowed to unambiguous forms before any measurement ran.
- **The taxonomy cannot express an Arm-versus-x86 shift (D100).** `server-cpus` is one node
  covering both, so the promised share-shift theme became a volume theme on `processor-ip`.

## Known limits carried forward

- **Company-wide attention** cannot tell "famous" from "famous for this theme". `--verify-attention`
  checks the top few with paired queries; how far the two diverge is still unmeasured.
- **Co-packaged optics and photonic ICs have producers only below the propagation threshold**
  (D98), so a CPO share-shift theme prices the loser side properly and reaches nobody on the
  winner side. That is honest rather than broken, but it makes the theme read as one-sided.
- **Tone is sentiment.** It never sets a sign; it only raises `possible_reversal`. Whether that
  flag actually fires on the three reversal events is exactly what M29 will measure.
- Phase 1's gaps are unchanged (D95): 61 companies, not 150, and the `REQUIRES` judge was not
  re-run with graph structure.
