# Read-only UI

An off-roadmap tool built while Phase 3 waits on GDELT. It does not use the phase milestone
numbering. The UI makes the graph and every analytical feature explorable in a browser, so
rankings, paths, provenance, coverage and bursts can be judged directly instead of through CLI
tables.

```bash
uv run ripple ui --port 8765     # then open http://127.0.0.1:8765
```

## Rules

- **Read-only.** The store is opened read-only for each request. The UI runs no pipeline and
  writes no review decision or ruling, so it cannot corrupt data. A long `ripple signals fetch`
  or `ripple load` only blocks it during a write.
- **No analytics of its own.** Every number comes from the library functions the CLI and MCP
  server use: `score.score`, `explain.explain_link` and `get_evidence`,
  `profile.company_profile`, `signal.day_stats`, `bursts`, `trending_themes` and
  `calibrate_threshold`, `events.check_events`, `verify.status`. Anything new goes in the
  library first.
- **No new dependencies.** starlette and uvicorn ship with `mcp`. The page is hand-written
  HTML, CSS and JavaScript with inline SVG and no build step. There is no vendored graph
  library: the flow is laid out in columns by hop depth, which needs none.
- **Weakness is visible.**
  - Bucket weights are drawn dashed.
  - Edges below the confidence threshold are ghosted, not hidden.
  - Unknown attention and novelty read "unknown", never 0 (D105).
  - Every edge panel shows its source layer, its evidence with verification state, and the
    rows that lost precedence (D63).
  - `as_of` is always on screen, and a banner lists the series that are missing.

## Views

| View | What it shows | API |
|---|---|---|
| Flow | The strongest paths from a theme, product or material to its top 25 companies, in columns by hop depth. Edge width follows the share of the shown paths' shock the edge carries; red means it carries a loss, which on a share-shift theme includes the loser's positive-polarity PRODUCES edges. Click a company for its profile, a product to shock it, an edge for its evidence. | `/api/flow` |
| Ranking | The `find_exposed` table, sorted by exposure or novelty (unknown sorts last), with `hide_obvious`. Click a row for paths and evidence. | `/api/exposed`, `/api/paths` |
| Company | Revenue mix, customers, suppliers, parents, subsidiaries, regions, attention, and exposure per theme (never summed). | `/api/company` |
| Signals | A theme's coverage share with its baseline, burst windows shaded, and the pre-registered event dates as ticks for display only. The default threshold is the calibrated one. | `/api/signals`, `/api/calibration`, `/api/events` |
| Events | The pre-registered event set against detected bursts, with nearest-burst gaps, twins and tone flags. | `/api/events` |
| Quality | Evidence rulings, coverage held, weight sources by relation, the confidence histogram. | `/api/quality` |

Every route takes `as_of`, and graph routes take `min_confidence`. Bad input returns a 400 with
a message.

## Layout

```
ripple/ui/app.py            starlette routes, JSON only
ripple/ui/static/index.html
ripple/ui/static/app.js     views, fetch calls, SVG rendering
ripple/ui/static/style.css  light and dark themes
tests/test_ui.py            every route against temp stores (starlette TestClient)
```

## Found while building it

- **Coverage mixed two queries (D120).** `Store.coverage` took the newest row per day across
  all query hashes, so a re-fetched series silently kept the old query's days wherever the new
  one did not reach them. The UI showed it as a zero on the last day of `ai-compute-demand`.
- **Calibration took 277 s.** `calibrate_threshold` recomputed every day's statistics for each of
  its 80 thresholds. It now computes them once per series (`signal.bursts_from_stats`) and takes
  about 3.5 s, with identical results.
