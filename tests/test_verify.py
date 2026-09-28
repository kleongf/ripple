"""Evidence verification: the ledger, rulings applied at load, and xbrl recompute (M33, D113)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ripple import verify
from ripple.cli import app
from ripple.edgar import UniverseEntry, save_universe
from ripple.load import load_seed
from ripple.model import Edge
from ripple.store import Store
from tests.conftest import SeedWriter, base_edges, base_nodes, edge
from tests.test_sources import write_dir

runner = CliRunner()
T1 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
URL = "https://example.com/source"


def entry(e: Edge, ruling: str = "verified", note: str = "test", **extra) -> verify.Entry:
    return verify.Entry(
        edge=e.id,
        url=URL,
        note_hash=verify.note_hash(note),
        ruling=ruling,  # type: ignore[arg-type]
        date=date(2026, 9, 28),
        **extra,
    )


def first_edge(seed_dir: Path) -> Edge:
    return load_seed(seed_dir).edges[0].value


def test_ledger_round_trip_and_later_rulings_win(write_seed: SeedWriter, tmp_path: Path) -> None:
    target = first_edge(write_seed(base_nodes(), base_edges()))
    ledger = tmp_path / "verified.yaml"
    verify.append_rulings([entry(target, "wrong-note", reason="says 40%, page says 30%")], ledger)
    verify.append_rulings([entry(target, "verified")], ledger)
    assert ledger.read_text().startswith("# Evidence rulings")
    loaded = verify.load_ledger(ledger)
    assert len(loaded) == 1
    assert next(iter(loaded.values())).ruling == "verified"


def test_unknown_ruling_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "verified.yaml"
    ledger.write_text(
        yaml.safe_dump(
            [
                {
                    "edge": "e-1",
                    "url": URL,
                    "note_hash": "x",
                    "ruling": "probably",
                    "date": date(2026, 1, 1),
                }
            ]
        )
    )
    with pytest.raises(ValueError, match="probably"):
        verify.load_ledger(ledger)


def test_only_a_verified_ruling_on_the_same_note_sets_verified(write_seed: SeedWriter) -> None:
    seed = load_seed(write_seed(base_nodes(), base_edges()))
    a, b, c = (located.value for located in seed.edges[:3])
    ledger = {
        e.key: e
        for e in (
            entry(a, "verified"),
            entry(b, "dead-link", reason="404"),
            entry(c, "verified", note="an older note"),  # the note changed since: lapses
        )
    }
    applied = verify.apply_rulings(seed, ledger)
    flags = {located.value.id: located.value.evidence[0].verified for located in applied.edges}
    assert flags[a.id] is True
    assert flags[b.id] is False
    assert flags[c.id] is False
    # The input is not mutated.
    assert not any(item.verified for located in seed.edges for item in located.value.evidence)


def test_load_stores_the_ruling_and_reports_a_change_of_winner(
    write_seed: SeedWriter, tmp_path: Path
) -> None:
    """A verified bucket row outranks an unverified filing row (D63), and `ripple load` says so."""
    filing = edge(
        "company/x",
        "PRODUCES",
        "product/a",
        0.55,
        weight_source="filing",
        evidence=[{"url": "https://sec.gov/x", "note": "10-K", "accessed": date(2026, 1, 1)}],
    )
    seed = write_seed(base_nodes(), base_edges())
    xbrl = write_dir(tmp_path / "xbrl", [], [filing])
    sources = tmp_path / "sources.yaml"
    sources.write_text(yaml.safe_dump({"seed": str(seed), "xbrl": str(xbrl)}))
    db, ledger = tmp_path / "r.duckdb", tmp_path / "verified.yaml"
    args = ["load", "--sources", str(sources), "--db", str(db), "--ledger", str(ledger)]
    assert runner.invoke(app, args).exit_code == 0
    seed_row = next(e for e in (x.value for x in load_seed(seed).edges) if e.key == filing_key())

    verify.append_rulings([entry(seed_row)], ledger)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert f"winner changed: {seed_row.id}" in result.output
    assert "now from seed (was xbrl)" in result.output
    with Store(db, read_only=True) as store:
        snap = store.snapshot(date.today())
    [won] = [e for e in snap.edges if e.key == filing_key()]
    assert won.evidence[0].verified and won.weight_source == "bucket"


def filing_key() -> tuple[str, str, str]:
    return ("company/x", "PRODUCES", "product/a")


def test_w11_appears_only_once_verification_has_started(
    write_seed: SeedWriter, tmp_path: Path
) -> None:
    seed = write_seed(base_nodes(), base_edges())
    ledger = tmp_path / "verified.yaml"
    before = runner.invoke(app, ["validate", str(seed), "--ledger", str(ledger)])
    assert "W11" not in before.output and "0 errors, 0 warnings" in before.output

    verify.append_rulings([entry(first_edge(seed))], ledger)
    after = runner.invoke(app, ["validate", str(seed), "--ledger", str(ledger)])
    assert after.exit_code == 0
    assert "W11" in after.output
    assert "6 of 7 propagating edges have no verified evidence" in after.output
    assert "1 of 7 evidence items verified" in after.output


@pytest.fixture
def loaded(write_seed: SeedWriter, tmp_path: Path) -> Path:
    db = tmp_path / "r.duckdb"
    with Store(db) as store:
        store.load_sources({"seed": write_seed(base_nodes(), base_edges())}, now=T1)
    return db


def test_status_and_rule_by_document(loaded: Path, tmp_path: Path) -> None:
    ledger = tmp_path / "verified.yaml"
    common = ["--db", str(loaded), "--ledger", str(ledger)]
    status = runner.invoke(app, ["verify", "status", *common])
    assert "0 of 7 ruled" in status.output and "across 1 documents" in status.output

    refused = runner.invoke(app, ["verify", "rule", "dead-link", "--document", URL, *common])
    assert refused.exit_code == 1 and "--reason" in refused.output

    ruled = runner.invoke(app, ["verify", "rule", "verified", "--document", URL, *common])
    assert ruled.exit_code == 0, ruled.output
    assert "7 items ruled verified" in ruled.output
    status = runner.invoke(app, ["verify", "status", *common])
    assert "7 of 7 ruled: 7 verified, 0 failed" in status.output
    assert "run `ripple load`" in status.output

    again = runner.invoke(app, ["verify", "rule", "verified", "--document", URL, *common])
    assert again.exit_code == 1 and "no unruled item" in again.output


def test_next_shows_the_document_and_its_items(loaded: Path, tmp_path: Path) -> None:
    args = ["verify", "next", "--db", str(loaded), "--ledger", str(tmp_path / "v.yaml")]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert URL in result.output
    assert "note: test" in result.output
    assert "1 documents left" in result.output


def test_walk_saves_rulings_after_each_document(loaded: Path, tmp_path: Path) -> None:
    ledger = tmp_path / "verified.yaml"
    args = ["verify", "walk", "--db", str(loaded), "--ledger", str(ledger)]
    answers = "e\n" + "v\n" + "w\nnote overstates the share\n" + "s\n" * 5
    result = runner.invoke(app, args, input=answers)
    assert result.exit_code == 0, result.output
    rulings = sorted(e.ruling for e in verify.load_ledger(ledger).values())
    assert rulings == ["verified", "wrong-note"]


def test_losing_rows_are_listed_filing_layer_first(write_seed: SeedWriter, tmp_path: Path) -> None:
    filing = edge(
        "company/x",
        "PRODUCES",
        "product/a",
        0.55,
        weight_source="filing",
        evidence=[{"url": URL, "note": "10-K", "accessed": date(2026, 1, 1)}],
    )
    with Store(tmp_path / "r.duckdb") as store:
        store.load_sources(
            {
                "seed": write_seed(base_nodes(), base_edges()),
                "xbrl": write_dir(tmp_path / "xbrl", [], [filing]),
            },
            now=T1,
        )
        snap = store.snapshot(date(2026, 6, 1))
    [group] = verify.unruled_by_document(snap, {}).values()
    assert group[0].source == "xbrl"
    losers = [i for i in group if not i.winner]
    assert [(i.source, i.edge.key) for i in losers] == [("seed", filing_key())]


def test_recompute_matches_regenerated_xbrl_edges_and_catches_a_tampered_one(
    write_seed: SeedWriter, tmp_path: Path
) -> None:
    from tests.test_xbrl import cache_fixture

    cache_fixture(tmp_path / "raw")
    universe = tmp_path / "universe.yaml"
    entries = [UniverseEntry("company/x", "EX", 1, "10-K", True)]
    save_universe(universe, entries)
    seed = write_seed(base_nodes(), base_edges())
    sources = tmp_path / "sources.yaml"
    sources.write_text(yaml.safe_dump({"seed": str(seed)}))
    out = tmp_path / "xbrl"
    result = runner.invoke(
        app,
        [
            "xbrl-edges",
            "--universe",
            str(universe),
            "--cache",
            str(tmp_path / "raw"),
            "--sources",
            str(sources),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    regions = out / "edges" / "x-regions.yaml"
    records = yaml.safe_load(regions.read_text())
    records[0]["weight"] = round(records[0]["weight"] + 0.05, 4)  # tamper with one edge
    regions.write_text(yaml.safe_dump(records, sort_keys=False))

    with Store(tmp_path / "r.duckdb") as store:
        store.load_sources({"seed": seed, "xbrl": out}, now=T1)
        snap = store.snapshot(date.today())
    group = [i for i in verify.items(snap) if i.source == "xbrl"]
    found = verify.recompute(group, entries, tmp_path / "raw", tmp_path / "queue")
    assert len(found) == 3
    assert sum(r.match for r in found) == 2
    [bad] = [r for r in found if not r.match]
    assert bad.item.edge.dst == records[0]["dst"] and "recomputed" in bad.detail


def test_link_check_records_status_and_redirects_without_ruling() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/old":
            return httpx.Response(301, headers={"Location": "https://a.example/new"})
        if path == "/gone":
            return httpx.Response(404)
        if request.url.host == "down.example":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text="page")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    waits: list[float] = []
    urls = [
        "https://a.example/ok",
        "https://a.example/old",
        "https://a.example/gone",
        "https://down.example/x",
        "https://a.example/ok",
    ]
    links = {link.url: link for link in verify.check_links(urls, client, sleep=waits.append)}
    assert len(links) == 4  # each distinct URL once
    assert links["https://a.example/ok"].ok and not links["https://a.example/ok"].moved
    assert links["https://a.example/old"].moved
    assert links["https://a.example/old"].final_url == "https://a.example/new"
    assert links["https://a.example/gone"].status == 404 and not links["https://a.example/gone"].ok
    assert links["https://down.example/x"].error.startswith("ConnectError")
    # Paced per host: three a.example URLs after the first wait; the other host does not.
    assert waits == [verify.LINK_INTERVAL] * 2
