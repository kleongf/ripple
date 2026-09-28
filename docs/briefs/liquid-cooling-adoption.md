# Liquid cooling: gains offset by air-cooling displacement

**As of 2026-09-28.** Ripple classifies `theme/liquid-cooling-adoption` as a **share shift**, not a general cooling-volume increase. Its three-company ranking identifies two net beneficiaries and one net loser, detailed below. The important distinction is between liquid-cooling gains and displaced air-cooling sales: suppliers participating in both markets retain only the net benefit. These are graph-model sensitivities, not revenue forecasts.

## What the news signal says

Ripple’s stored news data show **no qualifying liquid-cooling coverage burst** in the returned 90-day window, at a minimum surprise threshold of 1.75. The theme is not listed as unmeasured. This establishes only the absence of a qualifying burst—not stalled adoption.

Separately, the verified evidence note supporting adoption cites TrendForce estimates of approximately 33% liquid-cooling penetration of AI chips in 2025 and 53% in 2026 ([source](https://iconnect007.com/article/151221/liquid-cooling-to-reach-53-of-highend-ai-infrastructure-in-2026/151218/ein)). The graph routes gains into `product/liquid-cooling` through `e-2071f000b2` and losses into `product/air-cooling` through `e-770bc7d259`. Their respective weights, 0.4 and 0.1, are **bucket guesses**, not measured demand changes; the latter has negative polarity.

## Most exposed companies—and why

**Schneider Electric (`company/schneider-electric`) has exposure 0.015**, the highest net positive result. Its liquid-cooling path contributes 0.02 through `e-2071f000b2` and `e-b87ecc069c`; its air-cooling path subtracts 0.005 through `e-770bc7d259` and `e-43c9c7e910`.

The liquid-cooling evidence attributes participation to a controlling stake in Motivair ([source](https://www.facilitiesdive.com/news/schneider-electric-to-buy-controlling-stake-in-motivair/730423/)). The air-cooling evidence confirms sales of air-based datacenter systems ([source](https://www.facilitiesdive.com/news/data-centers-remain-standout-industry-for-schneider-electric/813362/)). Both company-product weights are 0.05 and explicitly **bucket guesses**. Thus, the leading position depends on estimated product shares as well as estimated substitution strength.

**Vertiv (`company/vertiv`) has exposure 0.0056**: a larger positive liquid-cooling contribution, but also a much larger offset. Liquid cooling contributes 0.0257 through `e-2071f000b2` and `e-94f718c610`; air cooling subtracts 0.0201 through `e-770bc7d259` and `e-c131dc633f`.

The evidence identifies **CDUs and cold-plate systems** on the gaining side and air-based thermal management on the losing side ([filing](https://www.sec.gov/Archives/edgar/data/1674101/000167410126000008/vrt-20251231.htm)). The active product weights are 0.0642 for liquid cooling and 0.2006 for air cooling, both marked `manual`. They are allocations derived from a broader filing category, not separately disclosed product revenue shares. Vertiv illustrates why gross liquid-cooling participation should not be mistaken for net share-shift benefit.

## Less obvious name: Johnson Controls

**Johnson Controls (`company/johnson-controls`) has exposure -0.0014**, the returned net loser. The negative route runs through air cooling, using `e-770bc7d259` and `e-8951eb857c`; the company-product weight is a manual 0.0137 ([filing](https://www.sec.gov/Archives/edgar/data/833444/000083344425000097/jci-20250930.htm)).

That result should not be generalized to all its cooling products. Its profile separately maps datacenter chillers, `product/datacenter-chillers`, through `e-66d909b7ea`, with a manual weight of 0.1025 supported by the same filing. The returned share-shift paths do **not** show a chiller loss. “Less obvious” here denotes this product-level distinction, not demonstrated investor neglect.

## Caveats

- Exposure measures fractional revenue sensitivity per unit theme shock, not expected growth.
- All opened evidence items were marked verified; verification does not make bucket or manual allocations measured facts.
- Attention and novelty are null for all three companies: **unknown**, not overlooked.
- Coverage is not demand. No burst statistic is multiplied by exposure, and no exposures across themes are added.