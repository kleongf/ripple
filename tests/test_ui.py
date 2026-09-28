"""The read-only UI's JSON routes, against temp stores (off-roadmap tool, docs/ui.md)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ripple.store import CoverageRow, Store
from ripple.ui.app import create_app
from tests.conftest import SeedWriter, base_edges, base_nodes

MINI = Path(__file__).parent / "fixtures" / "mini"
T1 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
AS_OF = "2026-09-26"


@pytest.fixture
def mini(tmp_path: Path) -> TestClient:
    db = tmp_path / "mini.duckdb"
    with Store(db) as store:
        store.load(MINI, now=T1)
    return TestClient(create_app(db, ledger=tmp_path / "verified.yaml"))


def get(client: TestClient, path: str, **params: object) -> dict:
    response = client.get(path, params={"as_of": AS_OF, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_page_and_its_assets_are_served(mini: TestClient) -> None:
    page = mini.get("/")
    assert page.status_code == 200 and "<title>Ripple</title>" in page.text
    for asset in ("app.js", "style.css"):
        assert mini.get(f"/static/{asset}").status_code == 200


def test_health_lists_what_has_no_coverage(mini: TestClient) -> None:
    data = get(mini, "/api/health")
    assert data["as_of"] == AS_OF
    assert data["themes"] == {"total": 2, "missing": ["theme/ai-compute", "theme/cooling-shift"]}
    assert data["companies"]["missing"] == data["companies"]["total"] == 8


def test_themes_carry_kind_and_series_length(mini: TestClient) -> None:
    themes = {t["id"]: t for t in get(mini, "/api/themes")["themes"]}
    assert themes["theme/cooling-shift"]["kind"] == "share_shift"
    assert themes["theme/ai-compute"]["series_days"] == 0


def test_flow_lays_out_paths_by_depth_with_exposures(mini: TestClient) -> None:
    data = get(mini, "/api/flow", node="theme/ai-compute")
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["theme/ai-compute"]["depth"] == 0
    assert nodes["product/accelerators"]["depth"] == 1
    assert nodes["company/asml"]["depth"] == 4
    assert nodes["company/asml"]["exposure"] == 0.16
    # Unknown attention is null, never 0 (D105).
    assert nodes["company/asml"]["novelty"] is None
    assert data["as_of"] == AS_OF and data["notes"]


def test_flow_ghosts_the_below_threshold_edge(mini: TestClient) -> None:
    """Rumor's 0.3-confidence edge is shown as evidence that does not propagate, so a missing
    company is explained rather than hidden."""
    data = get(mini, "/api/flow", node="theme/ai-compute")
    [ghost] = [e for e in data["edges"] if not e["propagates"]]
    assert ghost["src"] == "company/rumor" and ghost["flow"] == 0
    rumor = next(n for n in data["nodes"] if n["id"] == "company/rumor")
    assert "exposure" not in rumor


def test_flow_marks_bucket_weights(write_seed: SeedWriter, tmp_path: Path) -> None:
    db = tmp_path / "base.duckdb"
    with Store(db) as store:
        store.load_sources({"seed": write_seed(base_nodes(), base_edges())}, now=T1)
    data = get(TestClient(create_app(db)), "/api/flow", node="theme/vol")
    assert data["edges"] and {e["weight_source"] for e in data["edges"]} == {"bucket"}


def test_ranking_paths_edge_and_company(mini: TestClient) -> None:
    ranking = get(mini, "/api/exposed", node="theme/ai-compute")
    assert ranking["results"][0]["company"] == "company/nvidia"
    paths = get(mini, "/api/paths", **{"from": "theme/ai-compute", "to": "company/asml"})
    edge_id = paths["paths"][0]["edges"][0]["edge"]
    edge = get(mini, f"/api/edge/{edge_id}")
    assert edge["edge"] == edge_id and edge["evidence"][0]["verified"] is False
    profile = get(mini, "/api/company", id="company/vertiv")
    assert [t["theme"] for t in profile["themes"]] == ["theme/ai-compute", "theme/cooling-shift"]


def test_bad_input_is_a_400_with_a_message(mini: TestClient) -> None:
    for path, params in [
        ("/api/flow", {}),
        ("/api/flow", {"node": "company/asml"}),
        ("/api/company", {"id": "product/euv"}),
        ("/api/exposed", {"node": "theme/ai-compute", "as_of": "yesterday"}),
        ("/api/edge/e-0000000000", {}),
    ]:
        response = mini.get(path, params=params)
        assert response.status_code == 400, path
        assert response.json()["error"]


def test_a_missing_store_is_a_400_not_a_crash(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "missing.duckdb"))
    response = client.get("/api/health")
    assert response.status_code == 400 and "ripple load" in response.json()["error"]


def spike(key: str) -> list[CoverageRow]:
    start = date(2025, 12, 1)
    return [
        CoverageRow(
            "theme",
            key,
            start + timedelta(days=i),
            600 if 140 <= i < 144 else 60,
            500_000,
            -1.0,
            "q1",
        )
        for i in range(160)
    ]


def test_signals_trending_calibration_events_and_quality(tmp_path: Path) -> None:
    db = tmp_path / "mini.duckdb"
    with Store(db) as store:
        store.load(MINI, now=T1)
        store.add_coverage(spike("theme/cooling-shift"), now=T1)
    client = TestClient(create_app(db, ledger=tmp_path / "verified.yaml"))
    as_of = "2026-05-09"
    signals = get(client, "/api/signals", key="theme/cooling-shift", as_of=as_of, days=60)
    assert signals["query_hash"] == "q1"
    assert [b["start"] for b in signals["bursts"]] == ["2026-04-20"]
    assert all(set(d) >= {"share", "baseline", "ratio", "surprise"} for d in signals["days"])
    empty = get(client, "/api/signals", key="theme/ai-compute", as_of=as_of)
    assert empty["days"] == [] and empty["held_days"] == 0

    trending = get(client, "/api/trending", as_of=as_of, window=30)
    assert [b["theme"] for b in trending["bursts"]] == ["theme/cooling-shift"]
    assert get(client, "/api/calibration", as_of=as_of)["series"] == 1
    events = get(client, "/api/events")
    assert events["events"] and "hit_rate" in events

    quality = get(client, "/api/quality")
    assert quality["verification"]["total"] == quality["verification"]["documents_left"] * 16
    assert quality["coverage"] == {
        "themes_held": 1,
        "themes": 2,
        "companies_held": 0,
        "companies": 8,
    }
    # Eight PRODUCES edges, but Rumor's sits at confidence 0.3 and does not propagate.
    assert quality["weight_sources"]["PRODUCES"] == {"manual": 7}


def test_flow_signs_the_losers_edges_on_a_share_shift(mini: TestClient) -> None:
    """Aircool's PRODUCES edge has polarity +1 but carries the loss from the negative DRIVES
    edge above it: 0.3 x 0.7 = 0.21 lost per unit shock."""
    data = get(mini, "/api/flow", node="theme/cooling-shift")
    edge = next(e for e in data["edges"] if e["src"] == "company/aircool")
    assert edge["polarity"] == 1 and edge["signed_flow"] == pytest.approx(-0.21)
    down = get(mini, "/api/flow", node="theme/cooling-shift", direction="down")
    edge = next(e for e in down["edges"] if e["src"] == "company/aircool")
    assert edge["signed_flow"] == pytest.approx(0.21)
