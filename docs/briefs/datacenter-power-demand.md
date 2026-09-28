# Datacenter power demand: generation, equipment and less-obvious exposure

**As of 2026-09-28.** Ripple’s `theme/datacenter-power-demand` ranking identifies six exposed companies, led by nuclear generation and gas-turbine/grid suppliers. The results support an electricity-infrastructure brief, not a news-driven demand forecast. Baker Hughes and Caterpillar offer additional turbine exposure beyond the three highest-ranked companies, but the tools cannot establish that either is overlooked: attention and novelty are unknown for every company returned.

## What the news signal says

`trending_themes` returned **no qualifying burst** for this theme over its 90-day window, at a minimum surprise threshold of 1.75. That means no detected burst under those settings—not no news, or weak demand.

The underlying demand connection is better grounded: Ripple’s verified [Berkeley Lab evidence](https://newscenter.lbl.gov/2025/01/15/berkeley-lab-report-evaluates-increase-in-electricity-demand-from-data-centers/) says datacenters consumed approximately 4.4% of US electricity in 2023, with 6.7–12% projected for 2028. Edge `e-eed728c48e` connects the theme to `product/electricity-demand`, using a filing-sourced weight of 0.044. All company paths below begin with this edge. Coverage ratios and surprise scores are not demand changes.

## Most exposed companies—and why

- **Constellation Energy (`company/constellation`), exposure 0.0178**, ranks first through nuclear generation. Its complete path uses `e-eed728c48e` → `e-6352c61b5f` → `e-2a18e29ae7`. The evidence describes demand supporting contracted electricity volume and price rather than necessarily increasing nuclear capacity. The company’s nuclear revenue allocation is manual, supported by its [2025 filing](https://www.sec.gov/Archives/edgar/data/1868275/000186827526000032/ceg-20251231.htm).

- **Siemens Energy (`company/siemens-energy`), exposure 0.0132**, combines gas turbines/Gas Services and grid equipment, including transformers and HVDC. The turbine path is `e-eed728c48e` → `e-86578fecc7` → `e-5d8051da91`; the grid path is `e-eed728c48e` → `e-c8eb180d65` → `e-1197ebaf9a`. Its [FY2025 release](https://www.siemens-energy.com/global/en/home/press-releases/earnings-release-q4-fy-2025.html) supports these businesses, but both company-product weights of 0.25 are bucket guesses.

- **GE Vernova (`company/ge-vernova`), exposure 0.0094**, has three channels: turbines (`e-eed728c48e`, `e-86578fecc7`, `e-f555e1847a`), grid equipment (`e-eed728c48e`, `e-c8eb180d65`, `e-09b5917168`) and battery storage (`e-eed728c48e`, `e-804989d6fa`, `e-7f5eb92098`). Its [results evidence](https://www.gevernova.com/news/press-releases/ge-vernova-reports-fourth-quarter-full-year-2025-financial-results) describes turbines and associated services, grid solutions and power conversion. Storage is a small, bucket-estimated allocation; services are not separately scored.

- **Eaton (`company/eaton`), exposure 0.0017**, connects through grid equipment: `e-eed728c48e` → `e-c8eb180d65` → `e-44809c6c34`. Its [filing evidence](https://www.sec.gov/Archives/edgar/data/1551182/000155118226000007/etn-20251231.htm) supports a manual allocation from Electrical Americas.

## Two less-obvious candidates to investigate

These are alternatives beyond the leading three—not statistically demonstrated neglected names.

**Baker Hughes (`company/baker-hughes`), exposure 0.0035**, ranks fourth through gas turbines: `e-eed728c48e` → `e-86578fecc7` → `e-9b566fe6e2`. Its [filing evidence](https://www.sec.gov/Archives/edgar/data/1701605/000170160526000007/bkr-20251231.htm) supports a manual turbine revenue allocation of 0.0985.

**Caterpillar (`company/caterpillar`), exposure 0.0013**, follows the same electricity-to-turbine route, ending at `e-b9e624b51c` after `e-eed728c48e` and `e-86578fecc7`. Its [filing evidence](https://www.sec.gov/Archives/edgar/data/18230/000001823026000008/cat-20251231.htm) supports a manual allocation of 0.0379. This theme’s returned exposure is turbine-based, not a backup-generator score.

## Caveats

- Exposure is modeled fractional revenue impact per unit theme shock—not a forecast or stock-return estimate.
- Electricity-to-nuclear, turbine, grid and storage weights have **weight_source bucket**: guessed size classes, not measurements.
- All cited evidence was marked verified; none was marked unverified. Verification does not make estimated weights measured.
- Null attention/novelty means unknown. The default “hide obvious” query returned no companies.
- These are graph-specific rankings; exposures across themes must never be added.