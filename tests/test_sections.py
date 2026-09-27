"""Cutting filings into sections (M17). Fixtures are synthetic HTML in each real layout; no
filing text is copied into the repo (CLAUDE.md)."""

from ripple.sections import (
    FALLBACK_CHUNK_CHARS,
    Section,
    find_sections,
    section_text,
    to_text,
)

BODY = "Lorem ipsum dolor sit amet. " * 120  # about 3,400 characters per section


def toc(items: list[str], style: str) -> str:
    rows = "".join(
        f"<tr><td>{style.format(item=i)}</td><td>Title {i}</td><td>{n + 3}</td></tr>"
        for n, i in enumerate(items)
    )
    return f"<table>{rows}</table>"


def tenk(heading: str, toc_style: str = "Item {item}.") -> str:
    items = ["1", "1A", "1B", "2", "7", "7A", "8"]
    body = "".join(f"<p>{heading.format(item=i)}</p><p>{BODY}</p>" for i in items)
    return f"<html><body><p>FORM 10-K</p>{toc(items, toc_style)}{body}</body></html>"


def names(sections: list[Section]) -> list[str]:
    return [s.name for s in sections]


def test_to_text_keeps_block_boundaries_as_lines() -> None:
    text = to_text(
        "<div>Item 1.</div><div>Business</div><table><tr><td>a</td><td>b</td></tr>"
        "</table><p>x&amp;y&nbsp;z</p><script>ignored()</script>"
    )
    assert text.splitlines() == ["Item 1.", "Business", "a b", "x&y z"]


def test_standard_colon_layout_skips_the_table_of_contents() -> None:
    text = to_text(tenk("Item {item}: Heading", toc_style="Item {item}: Title"))
    sections = find_sections(text, "10-K")
    assert names(sections) == ["1", "1A", "7"]
    item1 = sections[0]
    assert section_text(text, item1).startswith("Item 1: Heading")
    assert "Item 1A" not in section_text(text, item1)  # ends at the next item heading
    assert all(not s.fallback for s in sections)


def test_dash_layout() -> None:
    text = to_text(tenk("ITEM {item} — HEADING"))
    assert names(find_sections(text, "10-K")) == ["1", "1A", "7"]


def test_item_7a_does_not_end_up_inside_item_7() -> None:
    text = to_text(tenk("Item {item}. Heading"))
    item7 = next(s for s in find_sections(text, "10-K") if s.name == "7")
    assert "Item 7A" not in section_text(text, item7)


def test_twenty_f_layout_with_risk_factors_inside_item_3() -> None:
    parts = [
        ("ITEM 3. KEY INFORMATION", BODY),
        ("D. Risk Factors", BODY),
        ("ITEM 4. INFORMATION ON THE COMPANY", BODY),
        ("ITEM 4A. UNRESOLVED", "none"),
        ("ITEM 5. OPERATING AND FINANCIAL REVIEWS AND PROSPECTS", BODY),
        ("ITEM 6. DIRECTORS", BODY),
    ]
    html = "<p>ITEM 3.</p><p>ITEM 4.</p><p>ITEM 5.</p>" + "".join(
        f"<p>{h}</p><p>{b}</p>" for h, b in parts
    )
    text = to_text(html)
    sections = find_sections(text, "20-F")
    assert names(sections) == ["3D", "4", "5"]
    assert section_text(text, sections[0]).startswith("D. Risk Factors")
    assert "ITEM 4A" not in section_text(text, sections[1])


def test_index_only_layout_falls_back_to_chunks() -> None:
    # Intel style: body organized by running headers, items only in a cross-reference index.
    body = "".join(f"<p>Our Business</p><p>{BODY}</p>" for _ in range(60))
    index = (
        "<p>Item 1. Business:</p><p>Item 1A. Risk Factors Pages 37 - 51</p>"
        "<p>Item 7. Management's Discussion and Analysis:</p><p>Item 8. Pages 56 - 108</p>"
    )
    text = to_text(f"<html><body>{body}{index}</body></html>")
    sections = find_sections(text, "10-K")
    assert all(s.fallback for s in sections)
    assert names(sections)[0] == "part-1"
    assert all(s.end - s.start <= FALLBACK_CHUNK_CHARS for s in sections)
    assert sections[0].start == 0 and sections[-1].end == len(text)


def test_sections_never_overlap_and_stay_in_bounds() -> None:
    text = to_text(tenk("Item {item}. Heading"))
    sections = find_sections(text, "10-K")
    for a, b in zip(sections, sections[1:], strict=False):
        assert a.end <= b.start
    assert all(0 <= s.start < s.end <= len(text) for s in sections)


def test_short_matches_are_not_sections() -> None:
    # An agenda that happens to say "Item 1 ... Item 6" (ASML) is not a set of sections.
    agenda = "".join(f"<p>Item {i}</p><p>Vote on topic {i}.</p>" for i in range(1, 7))
    text = to_text(f"<html><body><p>{BODY * 12}</p>{agenda}</body></html>")
    assert all(s.fallback for s in find_sections(text, "20-F"))


def test_load_sections_caches_text_and_offsets(tmp_path) -> None:
    import gzip
    import json

    from ripple.edgar import latest_cached
    from ripple.sections import load_sections
    from tests.test_xbrl import cache_fixture

    cache_fixture(tmp_path)
    cached = latest_cached(tmp_path, 1)
    assert cached is not None
    cached.files["primary"].write_bytes(gzip.compress(tenk("Item {item}. Heading").encode()))
    text, sections = load_sections(cached)
    assert [s.name for s in sections] == ["1", "1A", "7"]
    meta = json.loads((cached.directory / "sections.json").read_text())
    assert [s["name"] for s in meta["sections"]] == ["1", "1A", "7"]
    # Second call reads the cache, even if the primary document is gone.
    cached.files["primary"].unlink()
    again_text, again = load_sections(cached)
    assert (again_text, again) == (text, sections)


def test_sections_command_reports_each_filer(tmp_path) -> None:
    import gzip

    from typer.testing import CliRunner

    from ripple.cli import app
    from ripple.edgar import UniverseEntry, latest_cached, save_universe
    from tests.test_xbrl import cache_fixture

    cache_fixture(tmp_path / "raw")
    cached = latest_cached(tmp_path / "raw", 1)
    assert cached is not None
    cached.files["primary"].write_bytes(gzip.compress(tenk("Item {item}. Heading").encode()))
    universe = tmp_path / "universe.yaml"
    save_universe(universe, [UniverseEntry("company/x", "EX", 1, "10-K", True)])
    result = CliRunner().invoke(
        app, ["sections", "--universe", str(universe), "--cache", str(tmp_path / "raw")]
    )
    assert result.exit_code == 0, result.output
    assert "company/x" in result.output and "1A" in result.output
    assert "items found: 1 of 1" in result.output


def test_item_7_incorporated_by_reference_keeps_the_other_items() -> None:
    # Eaton: "Item 7." is followed at once by "Item 7A."; the MD&A lives elsewhere.
    items = [
        ("Item 1. Business.", BODY),
        ("Item 1A. Risk Factors.", BODY),
        ("Item 2. Properties.", BODY),
        ("Item 7. Discussion.", "See Appendix."),
        ("Item 7A. Market Risk.", BODY),
        ("Item 8. Statements.", BODY),
    ]
    text = to_text("".join(f"<p>{h}</p><p>{b}</p>" for h, b in items))
    assert names(find_sections(text, "10-K")) == ["1", "1A"]


def test_twenty_f_plain_risk_factors_heading_inside_item_3() -> None:
    # TSMC: the sub-heading is "Risk Factors" without the "D." prefix.
    parts = [
        ("ITEM 3. KEY INFORMATION", BODY),
        ("Risk Factors", BODY),
        ("ITEM 4. INFORMATION ON THE COMPANY", BODY),
        ("ITEM 5. OPERATING AND FINANCIAL REVIEWS AND PROSPECTS", BODY),
        ("ITEM 6. DIRECTORS", BODY),
        ("Risk Factors", "a later mention"),
    ]
    text = to_text("".join(f"<p>{h}</p><p>{b}</p>" for h, b in parts))
    sections = find_sections(text, "20-F")
    assert names(sections) == ["3D", "4", "5"]
    assert section_text(text, sections[0]).startswith("Risk Factors")


def test_an_index_after_the_body_does_not_steal_the_headings() -> None:
    items = ["1", "1A", "2", "7", "8"]
    body = "".join(f"<p>Item {i}. Heading</p><p>{BODY}</p>" for i in items)
    index = "".join(
        f"<p>Item {i}. Heading Pages {n * 10} - {n * 10 + 5}</p>" for n, i in enumerate(items, 1)
    )
    text = to_text(f"<html><body>{body}{index}<p>{BODY}</p></body></html>")
    sections = find_sections(text, "10-K")
    assert names(sections) == ["1", "1A", "7"]
    assert not any(s.fallback for s in sections)
