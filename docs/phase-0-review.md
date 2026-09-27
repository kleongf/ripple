# Phase 0 review

Written 2026-09-26 after Stage A and updated after Stage B (M9). The seed has 28 companies,
30 product and material nodes, 4 themes and 104 edges. 101 weights are buckets and 3 are
filing numbers. 52 edges are at confidence 0.5. Claude Code drafted every edge, and none
has been checked by a human yet (`data/seed/TODO.md`), so read the rankings below as a test
of the machinery, not of the data.

## Improvement pass (2026-09-26)

A second pass aimed at stability, sign correctness and coverage, with revenue weights and no
new model mechanics (plan: more data, plus small code changes). The sections below this one
describe the seed *before* the pass; items fixed by the pass are marked there.

**Code (small):**
- a `pure 0.9` revenue bucket, with validator warning W8 for off-bucket values
- sensitivity that redraws each bucket weight inside its bucket's range (the flat ±50% mode is
  kept for comparison)
- top-10 set overlap in the sensitivity output
- `weakest_confidence` on every path
- a `verified` flag on evidence

**Data:**
- 22 weights now come from sources:
  - 13 are `filing` (company reports): Nvidia compute and networking, Dell FY2026 and FY2027 as
    two valid-time versions, KLA, Micron HBM and DRAM, TSMC, Arista, Corning, Carrier.
  - 9 are `manual` (secondary sources): HBM and CoWoS demand splits, the AI share of data center
    capacity, AI servers' share of server CPUs, fiber demand from data centers, Ajinomoto, Cummins.
- 9 companies and 5 product or material nodes were added, with 20 edges: generators, data center
  chillers, silicon wafers, photoresist, coater/developer, and Intel.

| Measure | Before | After |
|---|---|---|
| Edges / companies | 104 / 28 | 125 / 37 |
| Bucket-sourced weights | 97% | 82% (target was under 60%; missed) |
| AI compute top-10 Spearman, flat ±50% | 0.720 | 0.739 |
| AI compute top-10 Spearman, bucket ranges | 0.778 (metric change alone) | 0.776 |
| AI compute Kendall tau (all), bucket ranges | 0.822 | 0.885 |
| AI compute top-10 set overlap, bucket ranges | 8.5 / 10 | 9 / 10 |
| Custom silicon: SK hynix, Micron | −0.010, −0.010 | +0.001, +0.001 (sign fixed) |
| Golden files | 4 pass | 4 pass, with new coverage and sign expectations |

**What changed in the rankings:**
- **Nvidia is now first** (0.53, sourced 0.75 compute share), ahead of Super Micro (0.48).
- **Dell rises to 6th** on the FY2027 run-rate version (0.37).
- **Vertiv drops from 13th to 22nd.** The AI share of data center capacity fell from a guessed
  0.4 to 0.1 (the midpoint of the IEA's 5–15%), so every cooling, power and fiber exposure shrank
  about 4x. This is the largest single change, and it came from replacing one guess with a
  source.
- **The new diversified names come in far down, as expected.** Caterpillar, Carrier, Johnson
  Controls and Trane are all below 0.011. SUMCO, a pure-play wafer maker (bucket 0.9), comes in
  16th.

**Still open:**
- **Top-10 Spearman stays just under 0.8.** Ranks 3–12 (Coherent 0.38 down to Micron 0.16) still
  depend on bucket products such as `ai-servers → optical-transceivers 0.8 × Coherent 0.6`.
  Coherent's Networking segment ($3.42B of $5.81B, from a search summary) and the LightCounting
  AI optics share could not be read at the source (timeouts, 403), so they stayed buckets.
  The top-10 *set* is stable (median 9 of 10), so the remaining noise is in the order within the
  top ten.
- **Vertiv's sign on liquid-cooling adoption is still −0.005.** No source splits Vertiv's thermal
  revenue into liquid and air, so the golden file does not assert it. An estimate from a blog
  (11.3% share of a $4.8B market, about 0.05 of revenue) supports the liquid bucket; nothing
  supports the air bucket.
- **Coverage is 125 edges against a target of 140 or more.** Specialty gases and copper were not
  added because no source gives a share (`data/seed/TODO.md`).

## Rankings

AI compute demand (top 12 of 28, all positive):

| # | Company | Exposure | Hops | Top path (via) |
|---|---|---|---|---|
| 1 | Super Micro | 0.480 | 2 | AI servers |
| 2 | Nvidia | 0.474 | 3 | AI servers → AI accelerators |
| 3 | Coherent | 0.384 | 3 | AI servers → optical transceivers |
| 4 | Marvell | 0.320 | 3 | AI servers → custom ASICs, and → optical transceivers |
| 5 | Lumentum | 0.314 | 3 | AI servers → optical transceivers (→ lasers) |
| 6 | TSMC | 0.226 | 4 | AI servers → AI accelerators → leading-edge logic |
| 7 | Arista | 0.216 | 3 | AI servers → datacenter switches |
| 8 | Dell | 0.200 | 2 | AI servers |
| 9–10 | Micron, SK hynix | 0.196 | 3 | AI servers → AI accelerators → HBM |
| 11 | AMD | 0.180 | 3 | AI servers → AI accelerators |
| 12 | Broadcom | 0.178 | 3 | AI servers → custom ASICs |

The deep chains all come through: KLA 0.125, Lam 0.098, Tokyo Electron 0.098, Ibiden 0.082,
ASML 0.078, Applied Materials 0.066. Ajinomoto is last at 0.0054, 5 hops away through ABF
substrates and ABF film. The power companies are reached through
`datacenter capacity → electricity demand` (D10) at about 0.01.

The other themes:

- `datacenter-power-demand`: GE Vernova 0.026, Constellation 0.021, Siemens Energy 0.013.
- `custom-silicon-adoption`: Broadcom and Marvell +0.10, Nvidia −0.06, AMD −0.025, then small
  second-order effects (memory makers −0.01, Ibiden +0.018).
- `liquid-cooling-adoption`: Schneider +0.015, Vertiv −0.005.

## What looked right

- **The core loop works end to end.** A Claude Code session with only the MCP tools answered
  "which non-obvious companies are exposed to AI compute demand?" It called `search_entities`
  and then `find_exposed`, and cited paths such as Ibiden getting paid through both the GPU
  and the custom-ASIC branch.
- **Dilution does what PLAN §3 promised.** Ajinomoto is reached but ranks last, because ABF film
  is about 5% of its revenue. Samsung ranks 20th although it makes HBM, DRAM and logic, because
  each is a small share of a very large company.
- **Non-obvious links surface with readable paths.** Ibiden, the equipment makers and Corning each
  come with a one-line reason. The paths answer "why is this company here?" better than the scores do.
- **Share-shift themes produce winners and losers with the right signs where the data is clear.**
  Broadcom and Marvell gain on custom silicon, and Nvidia and AMD lose.
- **Golden expectations written before any ranking held on the first run**, for all four themes,
  with one exception after Stage B (below).

## What looked wrong

1. **Broadcom fell out of the AI-compute top 10 after Stage B** (golden failure, since amended).
   The optics companies (Coherent, Lumentum, Marvell's optics line) have a larger share of
   revenue in AI data center products than Broadcom, whose software business is a large part of
   its revenue. The model is arguably right and the expectation was an "obvious names" bias. This
   was a judgment call made on your behalf; revert `tests/golden/ai_compute.yaml` if you disagree.
2. **Bucket ceilings distort pure plays.** *(Partly fixed: `pure 0.9` bucket, and KLA's peers now sit closer after Micron, TSMC and other filings.)* Revenue buckets stop at core 0.6, so a company with 90%
   of revenue in one product scores the same as one with 55%. KLA is the only equipment maker with
   a filing weight (0.9), so it outranks Lam, Tokyo Electron and Applied Materials (all bucketed at
   0.25 or 0.05) by up to 2x. The gap comes from the data source, not from the economics.
3. **Bucket rounding flips signs on share-shift themes.** *(Memory makers fixed with sourced HBM shares; Vertiv still open.)*
   - On `custom-silicon-adoption`, HBM demand moves by 0.4 × 0.1 (ASIC share of HBM, bucketed)
     − 0.1 × 0.8 (merchant share, bucketed) = −0.04, so SK hynix and Micron come out as losers.
     With TrendForce's unrounded 2024 shares (about 0.19 and 0.72) the same product is +0.004. The
     sign of every second-order effect on a share-shift theme is inside bucket noise.
   - On `liquid-cooling-adoption`, Vertiv is net negative (+0.4 × 0.05 liquid − 0.1 × 0.25 air).
     Its air-cooling bucket outweighs its liquid bucket, which is the reverse of how the market
     treats it. This is either right (most of Vertiv's thermal revenue is still air) or a bucket
     artifact; a filing split of Vertiv's thermal revenue would settle it.
4. **`hide_obvious` misfires in both directions.** After D7 moved volume themes to the top of the
   chain, makers of first-tier inputs sit 3 hops out, so the threshold became 3 (D23). With it,
   TSMC (4 hops) passes as "non-obvious" and Vertiv (3 hops) is hidden, which is backwards on both.
   Hop count is a poor stand-in for attention. The Phase 2 attention metric is needed before the
   filter means anything.
5. **Path confidence collapses with depth.** *(Addressed: `weakest_confidence` is now reported next to the product.)* Four to five edges at 0.5–0.7 give path confidences
   of 0.03–0.11 for every equipment maker. Multiplying confidences treats each edge's doubt as
   independent, which overstates the doubt when the same analyst guess sits on every path.
   Consider reporting the minimum edge confidence alongside the product.
6. **Theme units are not comparable.** A unit shock on `datacenter-power-demand` moves US
   electricity demand by about 4.4%, so power exposures come out around 0.02. A unit shock on
   `ai-compute-demand` moves AI server demand by 0.8. Numbers across themes cannot be compared, and
   the MCP notes already say not to add them. The next step is to calibrate shock size per
   theme (PLAN §3, "shock size").
7. **Nuclear and HBM tightness are price stories.** `electricity-demand REQUIRES nuclear-generation`
   treats more demand as more nuclear volume, but nuclear output is capacity-bound and gains
   through price. This is the same issue D14 avoided for HBM. It stays at confidence 0.5.

## Sensitivity (200 trials, bucket weights × U[0.5, 1.5])

| Theme | Median top-N Spearman | Median Kendall (all) | Least stable |
|---|---|---|---|
| ai-compute-demand | 0.720 (top 10) | 0.796 | Vertiv, Arista, Broadcom, TSMC, Amphenol |
| datacenter-power-demand | 0.500 (top 3) | 0.333 | Constellation, GE Vernova, Siemens Energy |
| custom-silicon-adoption | 0.890 (top 10) | 0.771 | TSMC, Ibiden, Micron, SK hynix, Tokyo Electron |
| liquid-cooling-adoption | 1.000 (top 2) | 1.000 | (only two companies) |
| ai-compute-demand, Stage A only | 0.686 (top 10) | 0.759 | Vertiv, Marvell, Broadcom, Dell, TSMC |

**AI compute misses the 0.8 exit bar.** Ranks 6 to 12 sit within 0.178–0.226: TSMC, Arista,
Dell, Micron, SK hynix, AMD and Broadcom, seven companies within ±12% of each other. Micron and
SK hynix, and Lam and Tokyo Electron, are exact ties, because identical buckets give identical
exposures. Independent ±50% jitter reorders any band this tight. Measured directly:

- The top-10 *set* keeps a median of 8 of its 10 members (minimum 6).
- The bottom 5 is unchanged in 171 of 200 trials.
- The top 2 is **not** stable. Super Micro (0.480) and Nvidia (0.474) swap often, and the pair
  survives in only 78 of 200 trials.

So the ranking is reliable at the level of tiers (top ten, middle, tail) and not at the level of
positions within the top ten. Better weights would fix this: filing numbers break the ties.
Loosening the test would not.

**Datacenter power** has three companies, so its top-3 Spearman can only be −1, −0.5, 0.5 or 1.
A median of 0.5 means one swap. GE Vernova and Constellation differ by 20%, and that gap depends
on two buckets (gas turbines core 0.6, nuclear core 0.6), so the order of the two is not
meaningful.

## Missing edge types and nodes

- **Supply constraints and pricing.** HBM, CoWoS, nuclear and transformers all gain mostly through
  price when supply is tight. A `CONSTRAINS` or price-elasticity channel is needed (PLAN §3 later
  refinements, D14).
- **Capex timing (bullwhip).** Equipment makers respond to changes in capacity additions, not to
  demand levels. Linear propagation gives semicap the same shape as demand. This is still open in
  `phase-0.md`.
- **Profit rather than revenue.** Ajinomoto's ABF film is about 5% of revenue, but the functional
  materials segment that contains it earns about 20% of group business profit (per the source).
  A revenue share understates its exposure, and a segment-profit weight would be the fix.
- **Product nodes left out for lack of a producer in the slice:** backup generators, chillers,
  silicon wafers, photoresist, specialty gases, copper and coater/developer tools
  (`data/seed/TODO.md`).
- **Company-to-company `SUPPLIES` edges.** None were added. D4 makes them rare where the product
  layer exists, but 10%-customer disclosures would add real, filing-backed links.

## Schema changes Phase 1 needs

1. **Regional concentration (D16).** Add `region` nodes and let `EXPOSED_TO` carry a revenue or
   capacity share. Most leading-edge logic and CoWoS capacity sits in Taiwan, and the graph cannot
   say so. Validation rule E3 must then allow `EXPOSED_TO` company → region.
2. **Profit weights.** Add an optional `profit_share` next to `weight` on `PRODUCES`, and let
   scoring choose revenue or profit exposure.
3. **Weight ranges instead of single buckets.** Store `weight_low` and `weight_high` (a bucket
   becomes a range) so sensitivity samples within evidence-backed bounds rather than a flat ±50%.
4. **A shock scale per theme.** Record what one unit of shock means for each theme, so exposures
   become comparable across themes.
5. **Evidence quality fields.** Add `source_type` (filing, IR, press, analyst, blog) and
   `verified_by` on evidence, so the "Claude drafted, human checked" state is visible in the data
   rather than only in `TODO.md`.
6. **A minimum edge confidence per path** in the result shape, next to the product of confidences.
