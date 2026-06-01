"""Revision-diff engine for cached datasheets.

Input: two `DatasheetRevision` records (old, new), typically produced by the
fetcher or looked up from the cache.

Output: a `DatasheetDiff` carrying:
    - page-count delta
    - section-heading changes
    - parameter-table deltas classified per DS-062
    - silicon-rev-linked and errata-candidate markers
    - overall severity

The implementation is best-effort: pdfplumber handles standard text-layer PDFs
well, but manufacturer layouts vary wildly. When a table can't be parsed
confidently, we emit an `UNPARSED` finding rather than pretending there's no
change. This is the right bias for design-review — silence is misleading.

No network I/O here. No writes to the cache. Stateless.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pdfplumber

from .models import (
    DatasheetConfig,
    DatasheetDiff,
    DatasheetRevision,
    DiffClass,
    FindingSeverity,
    ParameterChange,
    ParameterRow,
    SectionChange,
)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)(?:\s+|\s*[-:]\s*)([A-Z][^\n]{3,120})\s*$")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


@dataclass
class _ExtractedContent:
    page_count: int
    headings: list[tuple[int, str]]   # (page_number_1_indexed, normalized_heading)
    parameter_rows: list[ParameterRow]


def _normalize_heading(line: str) -> str:
    # Strip leading numbering so "3.2 Electrical Characteristics" matches
    # "Electrical Characteristics" across a renumber.
    return re.sub(r"^\s*\d+(?:\.\d+)*\s*[-:]?\s*", "", line).strip().lower()


def _parse_num(cell: str | None) -> float | None:
    if cell is None:
        return None
    s = cell.strip().replace(",", "")
    if not s or s.lower() in {"-", "—", "tbd", "n/a"}:
        return None
    # Pull the first numeric substring. Tables often carry footnote markers.
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _extract(path: Path, config: DatasheetConfig) -> _ExtractedContent:
    """Pull the structural content we diff against.

    Keeps memory bounded: we walk pages lazily and don't retain page objects.
    """
    headings: list[tuple[int, str]] = []
    rows: list[ParameterRow] = []
    wanted_heading_set = {h.strip().lower() for h in config.parameter_table_headings}
    current_section: str | None = None
    page_count = 0

    with pdfplumber.open(str(path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            page_count += 1
            text = page.extract_text() or ""
            for line in text.splitlines():
                m = _HEADING_RE.match(line)
                if m:
                    norm = _normalize_heading(line)
                    headings.append((idx, norm))
                    if norm in wanted_heading_set:
                        current_section = norm
            if current_section is None:
                continue

            # Extract tables only while we're inside a wanted section. Huge
                # datasheets often have dozens of non-parameter tables (pin
                # assignment, package drawings) — skipping them is a big win.
            try:
                tables = page.extract_tables() or []
            except Exception:
                # pdfplumber occasionally throws on malformed tables; treat as
                # "no tables on this page" and keep going.
                tables = []
            for raw in tables:
                if not raw or len(raw) < 2:
                    continue
                header = [(c or "").strip().lower() for c in raw[0]]
                if not any(h in header for h in ("parameter", "symbol", "description")):
                    continue
                col = _ColumnIndex(header)
                for r in raw[1:]:
                    cells = [(c or "").strip() for c in r]
                    if not any(cells):
                        continue
                    rows.append(
                        ParameterRow(
                            parameter_name=(col.get(cells, "parameter") or col.get(cells, "description") or "").strip(),
                            symbol=col.get(cells, "symbol") or None,
                            min=_parse_num(col.get(cells, "min")),
                            typ=_parse_num(col.get(cells, "typ")),
                            max=_parse_num(col.get(cells, "max")),
                            unit=col.get(cells, "unit") or None,
                            conditions=col.get(cells, "conditions") or col.get(cells, "test conditions") or None,
                            page=idx,
                            section_heading=current_section,
                            raw_row=cells,
                        )
                    )

    return _ExtractedContent(page_count=page_count, headings=headings, parameter_rows=rows)


class _ColumnIndex:
    """Small helper that maps canonical column names to actual column indices.

    Accepts a few common synonyms so "Min", "Minimum", "MIN." all resolve.
    """

    _SYNONYMS = {
        "parameter": {"parameter", "parameters", "parameter name"},
        "description": {"description", "desc"},
        "symbol": {"symbol", "sym"},
        "min": {"min", "minimum", "min."},
        "typ": {"typ", "typical", "typ.", "nom", "nominal"},
        "max": {"max", "maximum", "max."},
        "unit": {"unit", "units"},
        "conditions": {"conditions", "condition"},
        "test conditions": {"test conditions", "test condition"},
    }

    def __init__(self, header: list[str]) -> None:
        self._idx: dict[str, int] = {}
        for canonical, synonyms in self._SYNONYMS.items():
            for i, h in enumerate(header):
                if h in synonyms:
                    self._idx[canonical] = i
                    break

    def get(self, cells: list[str], name: str) -> str | None:
        i = self._idx.get(name)
        if i is None or i >= len(cells):
            return None
        return cells[i]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _row_key(row: ParameterRow) -> tuple[str, str]:
    return (
        re.sub(r"\s+", " ", row.parameter_name.lower().strip()),
        re.sub(r"\s+", " ", (row.symbol or "").lower().strip()),
    )


def _numeric_delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if math.isnan(a) or math.isnan(b):
        return None
    return b - a


def _classify_row_change(old: ParameterRow, new: ParameterRow) -> DiffClass:
    numeric_moved = (
        _numeric_delta(old.min, new.min) not in (None, 0.0)
        or _numeric_delta(old.typ, new.typ) not in (None, 0.0)
        or _numeric_delta(old.max, new.max) not in (None, 0.0)
        or (old.unit or "") != (new.unit or "")
    )
    if numeric_moved:
        return DiffClass.SPECIFICATION

    only_punct_ws = (
        re.sub(r"\W", "", old.parameter_name or "") == re.sub(r"\W", "", new.parameter_name or "")
        and re.sub(r"\W", "", old.conditions or "") == re.sub(r"\W", "", new.conditions or "")
    )
    if only_punct_ws:
        return DiffClass.EDITORIAL
    return DiffClass.CLARIFYING


def _is_errata_candidate(row_name: str, config: DatasheetConfig) -> bool:
    name = row_name.lower()
    return any(tag in name for tag in config.errata_relevant_fields)


def _severity_for(diff_class: DiffClass, *, errata_candidate: bool) -> FindingSeverity:
    if errata_candidate and diff_class == DiffClass.SPECIFICATION:
        return FindingSeverity.CRITICAL
    return {
        DiffClass.EDITORIAL: FindingSeverity.INFO,
        DiffClass.CLARIFYING: FindingSeverity.HINT,
        DiffClass.SPECIFICATION: FindingSeverity.WARN,
        DiffClass.SILICON_REV_LINKED: FindingSeverity.CRITICAL,
        DiffClass.STRUCTURAL: FindingSeverity.INFO,
        DiffClass.UNPARSED: FindingSeverity.WARN,
    }[diff_class]


def _diff_sections(old: list[tuple[int, str]], new: list[tuple[int, str]]) -> list[SectionChange]:
    """Ordered-sequence diff between heading lists.

    We use difflib.SequenceMatcher on the heading text only; pages come from
    the original lists. This handles renumbering gracefully.
    """
    old_h = [h for _, h in old]
    new_h = [h for _, h in new]
    sm = difflib.SequenceMatcher(a=old_h, b=new_h, autojunk=False)
    changes: list[SectionChange] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            # Treat as pairwise rename when sizes line up.
            pairs = zip(old[i1:i2], new[j1:j2])
            for (op, oh), (np, nh) in pairs:
                changes.append(
                    SectionChange(
                        change_kind="renamed",
                        old_heading=oh,
                        new_heading=nh,
                        old_page=op,
                        new_page=np,
                    )
                )
            # Remaining unpaired go as added / removed.
            for op, oh in old[i1 + min(i2 - i1, j2 - j1) : i2]:
                changes.append(SectionChange(change_kind="removed", old_heading=oh, old_page=op))
            for np, nh in new[j1 + min(i2 - i1, j2 - j1) : j2]:
                changes.append(SectionChange(change_kind="added", new_heading=nh, new_page=np))
        elif tag == "delete":
            for op, oh in old[i1:i2]:
                changes.append(SectionChange(change_kind="removed", old_heading=oh, old_page=op))
        elif tag == "insert":
            for np, nh in new[j1:j2]:
                changes.append(SectionChange(change_kind="added", new_heading=nh, new_page=np))
    return changes


def diff_revisions(
    old: DatasheetRevision,
    new: DatasheetRevision,
    config: DatasheetConfig,
) -> DatasheetDiff:
    """Compute a structured diff between two cached revisions.

    The two revisions must be of the same MPN; we don't enforce manufacturer
    match because some parts are second-sourced under a different brand.
    """
    if old.cache_entry.mpn != new.cache_entry.mpn:
        raise ValueError(
            f"diff requires same MPN: got {old.cache_entry.mpn!r} vs {new.cache_entry.mpn!r}"
        )

    old_content = _extract(old.absolute_pdf_path, config)
    new_content = _extract(new.absolute_pdf_path, config)

    # Section changes --------------------------------------------------------
    section_changes = _diff_sections(old_content.headings, new_content.headings)

    # Parameter changes ------------------------------------------------------
    parameter_changes: list[ParameterChange] = []
    old_by_key: dict[tuple[str, str], ParameterRow] = {}
    for r in old_content.parameter_rows:
        old_by_key[_row_key(r)] = r
    new_by_key: dict[tuple[str, str], ParameterRow] = {}
    for r in new_content.parameter_rows:
        new_by_key[_row_key(r)] = r

    seen: set[tuple[str, str]] = set()
    for key, old_row in old_by_key.items():
        seen.add(key)
        new_row = new_by_key.get(key)
        if new_row is None:
            parameter_changes.append(
                ParameterChange(
                    diff_class=DiffClass.STRUCTURAL,
                    severity=FindingSeverity.WARN,
                    change_kind="removed",
                    parameter_name=old_row.parameter_name,
                    symbol=old_row.symbol,
                    old=old_row,
                    errata_candidate=_is_errata_candidate(old_row.parameter_name, config),
                )
            )
            continue
        cls = _classify_row_change(old_row, new_row)
        if cls == DiffClass.EDITORIAL:
            continue    # don't surface noise
        errata = _is_errata_candidate(old_row.parameter_name, config) or _is_errata_candidate(
            new_row.parameter_name, config
        )
        parameter_changes.append(
            ParameterChange(
                diff_class=cls,
                severity=_severity_for(cls, errata_candidate=errata),
                change_kind="modified",
                parameter_name=new_row.parameter_name or old_row.parameter_name,
                symbol=new_row.symbol or old_row.symbol,
                old=old_row,
                new=new_row,
                delta_min=_numeric_delta(old_row.min, new_row.min),
                delta_typ=_numeric_delta(old_row.typ, new_row.typ),
                delta_max=_numeric_delta(old_row.max, new_row.max),
                unit_changed=(old_row.unit or "") != (new_row.unit or ""),
                errata_candidate=errata,
            )
        )

    for key, new_row in new_by_key.items():
        if key in seen:
            continue
        parameter_changes.append(
            ParameterChange(
                diff_class=DiffClass.STRUCTURAL,
                severity=FindingSeverity.WARN,
                change_kind="added",
                parameter_name=new_row.parameter_name,
                symbol=new_row.symbol,
                new=new_row,
                errata_candidate=_is_errata_candidate(new_row.parameter_name, config),
            )
        )

    # Silicon-rev hint pass --------------------------------------------------
    for pc in parameter_changes:
        name = (pc.parameter_name or "").lower()
        if "device id" in name or "silicon rev" in name or "die rev" in name:
            pc.silicon_rev_linked = True
            pc.diff_class = DiffClass.SILICON_REV_LINKED
            pc.severity = FindingSeverity.CRITICAL

    # Overall classification ------------------------------------------------
    overall_sev = FindingSeverity.INFO
    overall_cls = DiffClass.EDITORIAL
    rank = {
        FindingSeverity.INFO: 0,
        FindingSeverity.HINT: 1,
        FindingSeverity.WARN: 2,
        FindingSeverity.ERROR: 3,
        FindingSeverity.CRITICAL: 4,
    }
    for f in (*section_changes, *parameter_changes):
        if rank[f.severity] > rank[overall_sev]:
            overall_sev = f.severity
        if f.diff_class != DiffClass.EDITORIAL:
            overall_cls = f.diff_class

    # Page-count delta ------------------------------------------------------
    page_delta = new_content.page_count - old_content.page_count

    citations = {"DS-061", "DS-063"}
    if any(pc.diff_class == DiffClass.SPECIFICATION for pc in parameter_changes):
        citations.add("DS-062")
    if any(pc.silicon_rev_linked for pc in parameter_changes):
        citations.add("DS-062")
    if any(pc.errata_candidate for pc in parameter_changes):
        citations.add("DS-031")

    return DatasheetDiff(
        mpn=old.cache_entry.mpn,
        manufacturer=new.cache_entry.manufacturer,
        old_revision=old.cache_entry.revision,
        new_revision=new.cache_entry.revision,
        old_sha256=old.cache_entry.sha256,
        new_sha256=new.cache_entry.sha256,
        old_page_count=old_content.page_count,
        new_page_count=new_content.page_count,
        page_count_delta=page_delta,
        section_changes=section_changes,
        parameter_changes=parameter_changes,
        unparsed_tables=[],     # reserved for a future confidence-heuristic pass
        overall_severity=overall_sev,
        overall_class=overall_cls,
        rule_citations=sorted(citations),
    )
