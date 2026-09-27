"""Cut cached filings into sections with character offsets (Phase 1, M17, D72).

Offsets point into the plain text that `to_text` makes from the primary document. That text
lives only in the gitignored cache; the repo stores offsets, never filing text.

10-K targets are Items 1, 1A and 7; 20-F targets are Item 3.D (risk factors), 4 and 5. A body
heading is the last short "Item N" line that is not a table-of-contents row (those end in page
numbers). A target shorter than MIN_SECTION_CHARS is not a real section (a cross-reference
index, or an agenda that happens to say "Item 1"). When a required item is missing, the
document is cut into fallback chunks instead.
"""

import gzip
import html
import json
import re
from dataclasses import asdict, dataclass

from ripple.edgar import CachedFiling, read_cached

SECTIONS_VERSION = 2  # bump when the text or heading rules change; invalidates cached sections
MIN_SECTION_CHARS = 2_000
FALLBACK_CHUNK_CHARS = 128_000  # about 32k tokens
FALLBACK_MAX_CHUNKS = 5
MAX_HEADING_CHARS = 250

TARGETS = {"10-K": ("1", "1A", "7"), "20-F": ("3D", "4", "5"), "40-F": ("3D", "4", "5")}
# Item 7 (MD&A) can be incorporated by reference (Eaton), so only the business item is required.
REQUIRED = {"10-K": ("1",), "20-F": ("4",), "40-F": ("4",)}

ITEM_HEADING = re.compile(r"^item\s*(\d{1,2}[a-d]?)\b\s*(?:[.:—–-]\s*)?(.*)$", re.I)
RISK_FACTORS_3D = re.compile(r"^d\.\s*risk\s+factors\s*$", re.I)
RISK_FACTORS = re.compile(r"^risk\s+factors\s*$", re.I)
TOC_ROW = re.compile(r"(\bpages?\b[\s\d,\-–]*|\s\d{1,3})$", re.I)
BLOCK_END = re.compile(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|section|ul|ol)\s*>")


@dataclass(frozen=True)
class Section:
    name: str
    start: int
    end: int
    fallback: bool = False


def to_text(raw_html: str) -> str:
    """Plain text with one line per block element. Offsets refer to this exact output."""
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", " ", raw_html)
    text = BLOCK_END.sub("\n", text)
    text = re.sub(r"(?i)</t[dh]\s*>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def find_sections(text: str, form: str) -> list[Section]:
    form = form if form in TARGETS else "10-K"
    headings = _headings(text, form)
    ordered = sorted(headings.items(), key=lambda item: item[1])
    sections = []
    for name in TARGETS[form]:
        if name not in headings:
            continue
        start = headings[name]
        end = min((pos for _, pos in ordered if pos > start), default=len(text))
        if end - start >= MIN_SECTION_CHARS:
            sections.append(Section(name, start, end))
    if not all(any(s.name == name for s in sections) for name in REQUIRED[form]):
        return _fallback(text)
    return sorted(sections, key=lambda s: s.start)


def _headings(text: str, form: str) -> dict[str, int]:
    """Position of the last body heading for each item (plus 3D in 20-Fs)."""
    found: dict[str, int] = {}
    plain_risk_factors: list[int] = []
    offset = 0
    for line in text.split("\n"):
        if len(line) <= MAX_HEADING_CHARS:
            match = ITEM_HEADING.match(line)
            if match and not TOC_ROW.search(line):
                found[match.group(1).upper()] = offset
            elif form != "10-K" and RISK_FACTORS_3D.match(line):
                found["3D"] = offset
            elif form != "10-K" and RISK_FACTORS.match(line):
                plain_risk_factors.append(offset)
        offset += len(line) + 1
    if form != "10-K" and "3D" not in found and "3" in found and "4" in found:
        # TSMC: a plain "Risk Factors" sub-heading inside Item 3.
        inside = [pos for pos in plain_risk_factors if found["3"] < pos < found["4"]]
        if inside:
            found["3D"] = inside[-1]
    return found


def _fallback(text: str) -> list[Section]:
    sections: list[Section] = []
    start = 0
    while start < len(text) and len(sections) < FALLBACK_MAX_CHUNKS:
        end = min(start + FALLBACK_CHUNK_CHARS, len(text))
        if end < len(text):
            cut = text.rfind("\n", start, end)
            end = cut if cut > start else end
        sections.append(Section(f"part-{len(sections) + 1}", start, end, fallback=True))
        start = end + 1 if end < len(text) and text[end] == "\n" else end
    return sections


def section_text(text: str, section: Section) -> str:
    return text[section.start : section.end]


def load_sections(cached: CachedFiling) -> tuple[str, list[Section]]:
    """The filing's plain text and its sections, built once and cached next to the filing."""
    text_path = cached.directory / "text.txt.gz"
    meta_path = cached.directory / "sections.json"
    if text_path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if meta.get("version") == SECTIONS_VERSION:
            text = gzip.decompress(text_path.read_bytes()).decode("utf-8")
            return text, [Section(**s) for s in meta["sections"]]
    raw = read_cached(cached.files["primary"]).decode("utf-8", "ignore")
    text = to_text(raw)
    sections = find_sections(text, cached.filing.form)
    text_path.write_bytes(gzip.compress(text.encode("utf-8")))
    meta_path.write_text(
        json.dumps({"version": SECTIONS_VERSION, "sections": [asdict(s) for s in sections]})
    )
    return text, sections
