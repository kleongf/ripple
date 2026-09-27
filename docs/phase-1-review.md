# Phase 1 review

## 1a: structured facts (M10 to M15), 2026-09-27

### What was built

| Milestone | Result |
|---|---|
| M10 EDGAR client | Latest annual filing of the 27 SEC filers in the seed (25 10-K, 2 20-F) cached in 37 s, 16 MB |
| M11 XBRL parser | 26 of 27 filers tag a product or segment split; US-GAAP and IFRS both read (D53–D55) |
| M12 sources | `seed` and `xbrl` load together, validated as one graph; consolidation by precedence (D56, D63) |
| M13 mapping | Codex (`gpt-6-astra`) mapped 26 revenue breakdowns onto the taxonomy, about 6.5k input tokens each; 24 accepted (3 after edits), 2 rejected (D57–D60) |
| M14 regions and customers | 27 region nodes, 105 `EXPOSED_TO` edges (not traversed); 1 customer edge, stored below threshold (D61, D62) |

The graph now has 37 companies, 78 `PRODUCES`, 51 `REQUIRES`, 7 `DRIVES`, 1 `SUPPLIES` and 105 `EXPOSED_TO` edges. All 229 tests pass offline, and all four golden files pass on the combined graph.

### Exit criterion: met

**84% of `PRODUCES` weights for SEC filers come from XBRL** (47 of 56; target 80%). The 9 seed rows that still win all have a reason:

| Company | Product | Seed weight | Why the seed row wins |
|---|---|---|---|
| Dell | AI servers | 0.37 (filing) | FY2027 run rate; the XBRL row is FY2026 (0.223), a year older |
| Micron | DRAM, HBM | 0.61, 0.18 (filing) | fiscal Q4 run rate, newer than the FY2025 XBRL rows |
| TSMC | leading-edge logic | 0.74 (filing) | 3Q25 management report, newer than the FY2025 row (0.636) |
| Lumentum | lasers, transceivers | buckets | no usable XBRL breakdown (segment facts use a different revenue concept) |
| Super Micro | AI servers | bucket | mapping rejected: the tagged breakdown holds only service lines |
| Cummins | backup generators | 0.11 (manual) | mapping rejected: overlapping product lines |
| Marvell | optical transceivers | bucket | XBRL does not split out the optics line |

**Caveat: "from XBRL" mostly means "anchored in XBRL".** Only 3 of the 47 `xbrl` rows are pure filing numbers, where a whole revenue line maps to one product (ASML's EUV, DUV and metrology). The other 44 multiply a filed line share by a Codex split of that line: `manual`, confidence 0.7. The revenue lines are facts; the splits are judgments. Examples: Nvidia's Compute line counted as 90% accelerators, and Vertiv's product line spread across UPS, air, liquid, switchgear and chillers.

### How the weights changed

Bucket-sourced weights dropped from 82% to 54% of traversed edges, and from 58% to 32% of `PRODUCES` edges.

| Edge | Before | After | Why |
|---|---|---|---|
| ASML EUV / DUV | 0.25 / 0.25 bucket | 0.355 / 0.369 filing | NXE + EXE, and ArF + KrF + i-line lines |
| Applied Materials deposition | 0.25 bucket | 0.40 | 55% of Semiconductor Systems (73%) |
| KLA inspection and metrology | 0.90 filing | 0.70 | product lines exclude the 23% service line (D58) |
| GE Vernova gas turbines | 0.60 bucket | 0.14 | the Power segment is half services, which stay unassigned |
| Constellation nuclear | 0.60 bucket | 0.46 | segments by region, split by Codex |
| Vertiv liquid / air cooling | 0.05 / 0.25 bucket | 0.064 / 0.18 | Codex split of an 80% product line |

Excluding service lines (D58) lowers weights across the board for equipment and power companies. That is deliberate: service revenue follows the installed base, not new demand. It also means a company's weights sum to well below 1.

### Rankings and stability

AI compute demand, top 10: Nvidia 0.51, Super Micro 0.48, Coherent 0.40, Marvell 0.35, Lumentum 0.31, Dell 0.30, TSMC 0.28, Arista 0.25, SK hynix 0.20, Broadcom 0.16.

| Theme | Top-N Spearman (bucket / flat) | Top-N overlap | Kendall (bucket) | Phase 0 end (bucket / flat) |
|---|---|---|---|---|
| ai-compute-demand | **0.867 / 0.806** (top 10) | 9 / 10 | 0.910 | 0.776 / 0.739 |
| custom-silicon-adoption | 0.855 / 0.879 (top 10) | 7 / 10 | 0.500 | 0.845 / 0.875 |
| datacenter-power-demand | 1.000 / 1.000 (top 5) | 5 / 5 | 1.000 | 1.000 / 0.500 (top 3) |
| liquid-cooling-adoption | 1.000 / 1.000 (top 3) | 3 / 3 | 1.000 | 1.000 (top 2) |

**AI compute now clears the 0.8 exit bar from Phase 0, in both jitter modes.** Replacing buckets with filing-anchored weights broke the ties that made the top 10 unstable. Custom silicon's Kendall tau fell because the theme now reaches more companies with near-zero second-order exposure (±0.0002), whose order is noise. Its top of the ranking (Marvell, Broadcom up; Nvidia, AMD down) holds.

Other changes:
- **Vertiv flipped to positive on liquid-cooling adoption (+0.008).** The flip comes from Codex's split of its product line, not from a disclosed number.
- **Johnson Controls is slightly negative (−0.001).** It now has an air-cooling edge.
- **The power theme reaches five companies.** Eaton (grid equipment) and Caterpillar (gas turbines) join Constellation, Siemens Energy and GE Vernova.

### What XBRL could not supply

- **Splits inside a revenue line.** 44 of 47 rows depend on a Codex fraction. Better numbers need text (MD&A tables, Phase 1b) or a human check.
- **Demand shares (`REQUIRES`, `DRIVES`).** 1a does not touch them; they remain buckets and secondary-source numbers. They are now the largest source of guesswork in the graph.
- **Customers.** Filers name almost no customers that are in the graph (one edge in 27 filings), because customers are anonymized ("Customer A") or outside the slice (PACCAR, Lenovo, HP).
- **Non-SEC companies.** SK hynix, Samsung, Ibiden, Ajinomoto, Tokyo Electron, Shin-Etsu, SUMCO, Tokyo Ohka, Schneider and Siemens Energy keep their hand-seeded edges (D39).
- **Three filers with no usable split.** Lumentum (concept mismatch), Super Micro (only service lines tagged) and Cummins (overlapping lines).

### Corrections made during 1a

- **Precedence, D59 then D63.** Source-only precedence let Dell's older filing override a newer run rate. Plain recency then let seed bucket guesses beat filings, because hand-written dates are approximate. The final rule: trusted first, then sourced numbers before buckets, then the newer fact when more than 90 days newer, then source order.
- **Aggregate detection (M11).** The search stopped too late and dropped a detailed line that happened to equal a sum of others. It now stops once the lines partition the total.
- **Customer matching (M14).** Corporate suffixes ("Dell Inc.") blocked matches until they were stripped.

### Waiting for the user

- **Review decisions.** Claude Code reviewed all 26 mappings (`reviewer: claude-code` in `data/review-queue/mappings/`). Re-review any with `ripple review show` and `accept` or `reject` (D60).
- **Three new-product proposals** (D41): Marvell's optical DSPs, Micron's non-enterprise NAND, and Cummins' generator alternators.
- **Evidence verification.** 0 of 127 seed evidence items are verified. `data/seed/TODO.md` lists what to check first.
- **Before the full 1b pass (M20):** confirm that bulk Codex use through the ChatGPT login is acceptable under OpenAI's terms.

## 1b: text extraction (M16 to M21), 2026-09-27

### What was built

- `ripple/jobs.py`: resumable Codex batches (concurrency 4, stop on quota) and `ripple usage`.
- `ripple/sections.py`: item sections with offsets. Fallback chunks where a filer has no item headings (Intel, ASML).
- `ripple/growth.py` and `ripple grow`: one-hop universe growth from the companies filings name.
- `ripple/judge.py`: an LLM judge in batches of 20, used by `judge-growth`, `judge-mappings` and `judge-extract`.
- `ripple/extract.py` and `ripple extract`: `REQUIRES` proposals, disclosed numbers, and `PRODUCES`/`SUPPLIES` evidence, all span-verified and stored as offsets. `requires-edges` writes the llm source; `extract-report` gives the numbers below.
- Evidence has an optional `span` (D80). Every llm edge's evidence has a filing URL and a span.

### Exit criteria: partly met

| Measure (D73) | Result | Bar |
|---|---|---|
| `REQUIRES` proposals, judge-estimated precision | **67%** (65 of 97) | 85%: **not met** |
| Disclosed numbers, judge-estimated precision | 95% (505 of 531) | 85%: met |
| Universe additions, judge-estimated precision | 79% (19 of 24), entities 23 of 24 | 85%: **not met** |
| Seed `REQUIRES` recall | 46% (24 of 52 links rediscovered) | reported |
| Universe size | 61 (24 added) | about 150: **not met** |

All items were judged, since each pool is under 100.

**Why the `REQUIRES` number is low, and what it means.** The judge and Claude Code disagree in both directions:
- **Judge rejected, review accepted (28).** 10 are "datacenter capacity requires electricity demand", where the judge objected to the node's label (a demand node) rather than to the link. 9 are inspection and metrology links the seed already holds, where the ±400-character excerpt didn't spell out the dependency.
- **Judge accepted, review rejected (6).** These are links that double-count demand the seed routes elsewhere, such as AI servers → liquid cooling (the seed routes cooling through datacenter capacity) or AI servers → HBM (it goes through accelerators). The judge can't see graph structure.

After review, 87 were accepted and 10 rejected. Of the 87 accepted, only **7 are new links**, all at the minor bucket (for example enterprise SSDs → DRAM and HBM → inspection); 24 repeat seed links and 2 were skipped under E7. Text extraction mostly confirms the hand-built structure rather than extending it.

**Universe additions.** One hop from 27 filers yields only 24 eligible SEC companies after review (D76–D79). The 5 the judge still rejects are contract manufacturers and mixed cases (Amkor, Arm's supplier role, Extreme's campus switches). Reaching about 150 needs a second hop or a list-based source.

### Costs

A full pass took about 68k input tokens per filer, 3.5M for 51 SEC filers (D82). The whole of 1b used about 7M input tokens through the ChatGPT login, with no quota stops.

### Rankings and stability

AI compute demand, top 12: Nvidia 0.51, Super Micro 0.48, Fabrinet 0.44, Coherent 0.39, TSMC 0.31, Lumentum 0.31, Credo 0.31, Marvell 0.30, Dell 0.29, Arista 0.24, SK hynix 0.20, Onto Innovation 0.19.

| Theme | Top-N Spearman (bucket / flat) | Top-N overlap | Kendall (bucket) | End of 1a (bucket / flat) |
|---|---|---|---|---|
| ai-compute-demand | 0.758 / 0.673 (top 10) | 9 / 10 | 0.917 | 0.867 / 0.806 |
| custom-silicon-adoption | 0.745 / 0.764 (top 10) | 8 / 10 | 0.484 | 0.855 / 0.879 |
| datacenter-power-demand | 1.000 / 1.000 (top 6) | 6 / 6 | 1.000 | 1.000 / 1.000 (top 5) |
| liquid-cooling-adoption | 1.000 / 1.000 (top 3) | 3 / 3 | 1.000 | 1.000 / 1.000 |

**AI compute fell back below the 0.8 bar.** The additions (Fabrinet, Credo, Onto) land in a crowded band, with ranks 5–12 within 0.12, and their weights are Codex fractions inside XBRL lines. The top-10 set is still stable (9 of 10). The golden files pass; `ai_compute.yaml` widened `top_n` to 12 with a written reason (D81).

### Corrections made during 1b

- **Name resolution (D76).** A short name matched a longer title for a different business: Sumitomo became a bank, Westinghouse became Wabtec. The prefix rule now needs generic extra words, and stale annual reports (deregistered filers) are refused.
- **Growth judge (D78).** It first judged product claims from world knowledge it wasn't given. It now checks the selection rule against the company's own filing passages.
- **Review state lost on rerun.** The full `extract` pass rewrote the dev set's queue files and reset reviewed items to pending. `keep_review` now carries status, reviewer and judge verdict over by item ID, with a test. The review was redone.
- **Oracle (D79).** Cloud revenue was mapped to datacenter capacity, which cloud providers buy rather than sell. It was removed, consistent with AWS and Azure.

### Waiting for the user

- **The exit bar.** `REQUIRES` (67%) and additions (79%) miss 85% on the judge alone, and the universe is at 61, not 150. Options: accept the reviewed results as they are; give the judge the graph's structure and node descriptions and re-measure; or run a second growth hop.
- **New products (D67: yours to decide).** Extraction proposed 423 new products (332 distinct labels). The most frequent: cloud computing services (7), network routers (6), battery energy storage (10 across two labels), co-packaged optics (5), general-purpose servers (5), optical circuit switches (4), DPUs/IPUs (6), photonic integrated circuits (3), PCBs (3). Mapping proposals add enterprise storage (HPE, Oracle), PCIe/CXL connectivity (Astera), RF and analog chips (MACOM), processor IP (Arm), and flip-chip/SiP packaging (Amkor). Everything is in `data/review-queue/extract/*.yaml` under `new_products`.
- **Non-SEC companies** named in filings (246) are listed in `data/seed/TODO.md` for later.
