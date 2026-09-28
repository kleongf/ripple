"""Starlette app for the read-only UI: JSON routes over the library, plus the static page.

The UI adds no analytics. Every number comes from a library function the CLI and the MCP server
also use (`score`, `explain`, `profile`, `signal`, `events`, `verify`), so the three surfaces
cannot drift apart. The store is opened read-only for each request, so the UI can never write,
and a long `ripple signals fetch` or `ripple load` only blocks it for the moment of a write.
"""

from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ripple import events as events_module
from ripple import explain, profile, signal, verify
from ripple.graph import build_graph
from ripple.model import DEFAULT_MIN_CONFIDENCE, demand_flow, today_utc
from ripple.paths import min_hops, top_paths
from ripple.score import DEFAULT_MAX_HOPS, SHOCKABLE, score
from ripple.search import search_entities
from ripple.store import Store

STATIC = Path(__file__).parent / "static"
FLOW_COMPANIES = 25
FLOW_PATHS = 3


class BadRequest(Exception):
    pass


def create_app(db: Path, ledger: Path = verify.DEFAULT_LEDGER) -> Starlette:
    def open_store() -> Store:
        try:
            return Store(db, read_only=True)
        except FileNotFoundError as exc:
            raise BadRequest(f"no Ripple store at {db}; run `ripple load` first") from exc

    def endpoint(handler):  # type: ignore[no-untyped-def]
        async def route(request: Request) -> JSONResponse:
            try:
                with open_store() as store:
                    return JSONResponse(handler(request, store))
            except (BadRequest, ValueError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        return route

    def health(request: Request, store: Store) -> dict[str, Any]:
        as_of = _as_of(request)
        nodes = store.snapshot(as_of).nodes
        held = {key for _, key, *_ in store.coverage_summary()}
        themes = sorted(n for n, v in nodes.items() if v.type == "theme")
        companies = sorted(n for n, v in nodes.items() if v.type == "company")
        return {
            "as_of": as_of.isoformat(),
            "db": str(db),
            "rows": store.row_counts(),
            "themes": {"total": len(themes), "missing": [t for t in themes if t not in held]},
            "companies": {
                "total": len(companies),
                "missing": len([c for c in companies if c not in held]),
            },
        }

    def themes(request: Request, store: Store) -> dict[str, Any]:
        as_of = _as_of(request)
        nodes = store.snapshot(as_of).nodes
        held = {key: (days, first, last) for _, key, days, first, last in store.coverage_summary()}
        return {
            "as_of": as_of.isoformat(),
            "themes": [
                {
                    "id": n,
                    "label": v.label,
                    "kind": v.kind,
                    "query": v.query,
                    "series_days": held[n][0] if n in held else 0,
                }
                for n, v in sorted(nodes.items())
                if v.type == "theme"
            ],
        }

    def search(request: Request, store: Store) -> dict[str, Any]:
        query = request.query_params.get("q", "").strip()
        if not query:
            return {"results": []}
        kind = request.query_params.get("type") or None
        nodes = store.snapshot(_as_of(request)).nodes
        found = search_entities(nodes, query, type=kind, limit=_int(request, "limit", 15))  # type: ignore[arg-type]
        return {"results": [m.to_dict() for m in found]}

    def flow(request: Request, store: Store) -> dict[str, Any]:
        """The propagation subgraph for the hero view: the strongest paths from the shocked
        node to its top companies, laid out by hop depth, with every edge's provenance."""
        as_of = _as_of(request)
        node_id = _required(request, "node")
        min_conf = _float(request, "min_confidence", DEFAULT_MIN_CONFIDENCE)
        max_hops = _int(request, "max_hops", DEFAULT_MAX_HOPS)
        direction = "down" if request.query_params.get("direction") == "down" else "up"
        top = _int(request, "top", FLOW_COMPANIES)
        snapshot = store.snapshot(as_of)
        graph = build_graph(snapshot, min_confidence=min_conf)
        ranked = score(
            store,
            node_id,
            as_of=as_of,
            direction=direction,
            max_hops=max_hops,
            limit=top,
            min_confidence=min_conf,
        )
        hops = min_hops(graph, node_id, max_hops)
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}

        def add_node(nid: str, depth: int) -> None:
            if nid in nodes:
                nodes[nid]["depth"] = min(nodes[nid]["depth"], depth)
                return
            node = graph.nodes[nid]
            nodes[nid] = {"id": nid, "label": node.label, "type": node.type, "depth": depth}

        add_node(node_id, 0)
        for company in ranked.results:
            for path in top_paths(graph, node_id, company.company, k=FLOW_PATHS, max_hops=max_hops):
                for depth, nid in enumerate(path.nodes):
                    add_node(nid, hops.get(nid, depth) if nid != node_id else 0)
                for e in path.edge_records:
                    record = edges.setdefault(
                        e.id,
                        {
                            **explain.edge_record(e, snapshot, min_conf, evidence=False),
                            "flow": 0.0,
                            "signed_flow": 0.0,
                        },
                    )
                    # How much of the shown paths' shock the edge carries, and with which sign:
                    # on a share-shift theme the loser's edges carry a loss even though their
                    # own polarity is positive.
                    record["flow"] += abs(path.contribution)
                    record["signed_flow"] += path.contribution * (-1 if direction == "down" else 1)
        for company in ranked.results:
            nodes[company.company].update(
                exposure=round(company.exposure, 4),
                attention_percentile=company.attention_percentile,
                novelty=None if company.novelty is None else round(company.novelty, 4),
                guessed_weights=company.guessed_weights,
            )
        # Edges below the confidence threshold that touch a shown product: stored as evidence,
        # not propagated. Drawn ghosted so a missing company is explained, not hidden.
        shown_products = {n for n, v in nodes.items() if v["type"] in ("product", "material")}
        for e in snapshot.edges:
            if e.id in edges or demand_flow(e) is None or e.confidence >= min_conf:
                continue
            if e.dst in shown_products and e.src in graph.nodes:
                add_node(e.src, nodes[e.dst]["depth"] + 1)
                edges[e.id] = {
                    **explain.edge_record(e, snapshot, min_conf, evidence=False),
                    "flow": 0.0,
                    "signed_flow": 0.0,
                }
        for record in edges.values():
            record["flow"] = round(record["flow"], 5)
            record["signed_flow"] = round(record["signed_flow"], 5)
        return explain.envelope(
            as_of,
            list(ranked.to_dict()["notes"]),
            min_confidence=min_conf,
            max_hops=max_hops,
            direction=direction,
            node=node_id,
            shock_type=ranked.shock_type,
            kind=ranked.theme_kind,
            nodes=sorted(nodes.values(), key=lambda n: (n["depth"], n["id"])),
            edges=list(edges.values()),
        )

    def exposed(request: Request, store: Store) -> dict[str, Any]:
        return score(
            store,
            _required(request, "node"),
            as_of=_as_of(request),
            direction="down" if request.query_params.get("direction") == "down" else "up",
            max_hops=_int(request, "max_hops", DEFAULT_MAX_HOPS),
            hide_obvious=request.query_params.get("hide_obvious") == "true",
            limit=_int(request, "limit", 50),
            min_confidence=_float(request, "min_confidence", DEFAULT_MIN_CONFIDENCE),
        ).to_dict()

    def paths(request: Request, store: Store) -> dict[str, Any]:
        return explain.explain_link(
            store,
            _required(request, "from"),
            _required(request, "to"),
            as_of=_as_of(request),
            k=_int(request, "k", 3),
            max_hops=_int(request, "max_hops", DEFAULT_MAX_HOPS),
            min_confidence=_float(request, "min_confidence", DEFAULT_MIN_CONFIDENCE),
        )

    def edge(request: Request, store: Store) -> dict[str, Any]:
        return explain.get_evidence(
            store,
            request.path_params["edge_id"],
            as_of=_as_of(request),
            min_confidence=_float(request, "min_confidence", DEFAULT_MIN_CONFIDENCE),
        )

    def company(request: Request, store: Store) -> dict[str, Any]:
        return profile.company_profile(
            store,
            _required(request, "id"),
            as_of=_as_of(request),
            min_confidence=_float(request, "min_confidence", DEFAULT_MIN_CONFIDENCE),
        )

    def signals(request: Request, store: Store) -> dict[str, Any]:
        """One theme's coverage series with the burst statistic for every day, and its bursts."""
        key = _required(request, "key")
        as_of = _as_of(request)
        days = _int(request, "days", 730)
        min_surprise = _float(request, "min_surprise", signal.MIN_SURPRISE)
        rows = store.coverage(key, end=as_of, known_at=as_of)
        stats = signal.day_stats(rows)
        first = as_of - timedelta(days=days)
        return {
            "as_of": as_of.isoformat(),
            "key": key,
            "min_surprise": min_surprise,
            "query_hash": rows[-1].query_hash if rows else None,
            "held_days": len(rows),
            "days": [
                {
                    "day": s.day.isoformat(),
                    "matched": s.matched,
                    "share": s.share,
                    "baseline": s.baseline,
                    "expected": round(s.expected, 2),
                    "ratio": round(s.ratio, 2),
                    "surprise": round(s.surprise, 2),
                    "tone": s.tone,
                }
                for s in stats
                if s.day >= first
            ],
            "bursts": [
                {
                    "start": b.start.isoformat(),
                    "end": b.end.isoformat(),
                    "peak_day": b.peak_day.isoformat(),
                    "surprise": round(b.peak_surprise, 2),
                    "ratio": round(b.peak_ratio, 2),
                    "matched": b.matched,
                    "tone_flag": b.tone_flag,
                }
                for b in signal.bursts(key, rows, min_surprise=min_surprise)
                if b.end >= first
            ],
            "notes": [signal.TRENDING_NOTE],
        }

    def calibration(request: Request, store: Store) -> dict[str, Any]:
        as_of = _as_of(request)
        nodes = store.snapshot(as_of).nodes
        series = {
            n: store.coverage(n, end=as_of, known_at=as_of)
            for n, v in nodes.items()
            if v.type == "theme"
        }
        result = signal.calibrate_threshold(series)
        return {
            "as_of": as_of.isoformat(),
            "threshold": result.threshold,
            "rate": round(result.rate, 2),
            "target": result.target,
            "series": result.series,
            "in_code": signal.MIN_SURPRISE,
        }

    def trending(request: Request, store: Store) -> dict[str, Any]:
        return signal.trending_themes(
            store,
            _as_of(request),
            window=_int(request, "window", 90),
            min_surprise=_float(request, "min_surprise", signal.MIN_SURPRISE),
        )

    def events(request: Request, store: Store) -> dict[str, Any]:
        min_surprise = _float(request, "min_surprise", signal.MIN_SURPRISE)
        report = events_module.check_events(store, min_surprise=min_surprise)
        held = {key for _, key, *_ in store.coverage_summary()}
        return {
            "min_surprise": min_surprise,
            "tolerance_days": report.tolerance_days,
            "min_hit_rate": report.min_hit_rate,
            "hit_rate": round(report.hit_rate, 3),
            "exact_hit_rate": round(report.exact_hit_rate, 3),
            "measurable": sum(r.event.theme in held for r in report.results),
            "events": [
                {
                    "id": r.event.id,
                    "label": r.event.label,
                    "day": r.event.day.isoformat(),
                    "theme": r.event.theme,
                    "direction": r.event.direction,
                    "reversal": r.event.reversal,
                    "approximate": r.event.approximate,
                    "has_series": r.event.theme in held,
                    "hit": r.hit,
                    "burst": None
                    if r.burst is None
                    else {"start": r.burst.start.isoformat(), "end": r.burst.end.isoformat()},
                    "nearest_gap": r.nearest_gap,
                    "flagged": r.flagged,
                    "shares_burst_with": r.shares_burst_with,
                }
                for r in report.results
            ],
        }

    def quality(request: Request, store: Store) -> dict[str, Any]:
        as_of = _as_of(request)
        snapshot = store.snapshot(as_of)
        propagating = [
            e
            for e in snapshot.edges
            if demand_flow(e) is not None and e.confidence >= DEFAULT_MIN_CONFIDENCE
        ]
        by_rel: dict[str, Counter[str]] = {}
        for e in propagating:
            by_rel.setdefault(e.rel, Counter())[e.weight_source] += 1
        confidence = Counter(f"{min(int(e.confidence * 10), 9) / 10:.1f}" for e in snapshot.edges)
        status = verify.status(snapshot, verify.load_ledger(ledger))
        held = {key for _, key, *_ in store.coverage_summary()}
        return {
            "as_of": as_of.isoformat(),
            "weight_sources": {rel: dict(c) for rel, c in sorted(by_rel.items())},
            "propagating_edges": len(propagating),
            "stored_edges": len(snapshot.edges),
            "confidence": dict(sorted(confidence.items())),
            "sources": dict(Counter(snapshot.sources.get(e.id, "seed") for e in snapshot.edges)),
            "verification": {
                "total": status.total,
                "ruled": status.ruled,
                "verified": status.verified,
                "failed": status.failed,
                "documents_left": status.documents_left,
                "by_source": {
                    s: {"items": n, "ruled": r} for s, (n, r) in status.by_source.items()
                },
            },
            "coverage": {
                "themes_held": sum(
                    1 for n, v in snapshot.nodes.items() if v.type == "theme" and n in held
                ),
                "themes": sum(1 for v in snapshot.nodes.values() if v.type == "theme"),
                "companies_held": sum(
                    1 for n, v in snapshot.nodes.items() if v.type == "company" and n in held
                ),
                "companies": sum(1 for v in snapshot.nodes.values() if v.type == "company"),
            },
            "shockable": list(SHOCKABLE),
        }

    async def index(request: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    api = [
        ("/api/health", health),
        ("/api/themes", themes),
        ("/api/search", search),
        ("/api/flow", flow),
        ("/api/exposed", exposed),
        ("/api/paths", paths),
        ("/api/edge/{edge_id}", edge),
        ("/api/company", company),
        ("/api/signals", signals),
        ("/api/calibration", calibration),
        ("/api/trending", trending),
        ("/api/events", events),
        ("/api/quality", quality),
    ]
    routes = [Route(path, endpoint(handler)) for path, handler in api]
    routes += [Route("/", index), Mount("/static", StaticFiles(directory=STATIC), name="static")]
    return Starlette(routes=routes)


def _as_of(request: Request) -> date:
    value = request.query_params.get("as_of")
    if not value:
        return today_utc()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BadRequest(f"as_of must be YYYY-MM-DD, not {value!r}") from exc


def _required(request: Request, name: str) -> str:
    value = request.query_params.get(name)
    if not value:
        raise BadRequest(f"missing query parameter {name!r}")
    return value


def _int(request: Request, name: str, default: int) -> int:
    value = request.query_params.get(name)
    try:
        return default if value in (None, "") else int(value)
    except ValueError as exc:
        raise BadRequest(f"{name} must be an integer") from exc


def _float(request: Request, name: str, default: float) -> float:
    value = request.query_params.get(name)
    try:
        return default if value in (None, "") else float(value)
    except ValueError as exc:
        raise BadRequest(f"{name} must be a number") from exc
