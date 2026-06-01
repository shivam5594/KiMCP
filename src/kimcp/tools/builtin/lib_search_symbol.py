"""lib_search_symbol — keyword-search across .kicad_sym libraries (M31).

Companion to M30 ``lib_list_symbols``. Listing is the "browse the
shelf" operation; this tool is "find the part I have in mind across
several shelves at once". An LLM building a schematic typically has
a fuzzy intent ("I need a 3.3V LDO with SOT-23 footprint"); this
tool answers that by AND-matching the query terms against each
library entry's name + description + keywords + footprint filters +
reference designator.

Scope of the first ship
-----------------------

* **AND semantics across terms.** Every whitespace-delimited token in
  ``query`` must appear somewhere in the symbol's searchable text.
  Matches the common "opamp single-supply" intent without drifting
  into half-assed fuzzy search. OR / phrase / regex queries are
  deferred — dodged them on purpose to keep the API narrow.
* **Case-insensitive.** KiCAD library data is inconsistent on case
  ("LM358" / "lm358" / "Dual Op-Amp"); normalizing keeps results
  predictable.
* **Multiple sources.** Accepts a list of file-or-directory paths.
  Directory paths are walked (non-recursive one level deep) for
  ``*.kicad_sym`` files — mirrors how KiCAD's own sym-lib-table works.
* **Best-effort parsing.** One malformed lib doesn't poison the run;
  per-lib parse errors surface in ``parse_errors``, other libs still
  contribute results.
* **Per-match score.** A simple count: how many query terms matched
  the entry's searchable blob. Results are sorted by score DESC,
  then lib path ASC, then entry name ASC — deterministic across runs.

Not a moat feature yet — the moat is the citations / engineering
rigor in how results are shaped. This tool is the plumbing M21+ had
been waiting on.

Status enum:

* **ok**             — search ran; ``results`` holds matches (possibly 0).
* **invalid_input**  — empty query string or empty lib_paths list.
* **no_libs_found**  — none of the paths resolved to a usable lib file.
                        Differs from ``ok`` with empty results: here
                        the inputs themselves didn't point at anything.

READ classification: no filesystem writes, no subprocess, no snapshot.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kimcp._types import Backend, ToolClass
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SList
from kimcp.tools.base import Tool
from kimcp.tools.builtin.lib_list_symbols import LibSymbolEntry, _parse_lib_symbol

log = logging.getLogger(__name__)


# Default cap on returned matches. Library searches can produce
# hundreds of hits on broad queries ("amp", "resistor") — 50 is the
# sweet spot where a human scrolls comfortably and an LLM context
# stays cheap.
_DEFAULT_MAX_RESULTS = 50


# -- envelope sub-models ---------------------------------------------------


class LibSearchMatch(BaseModel):
    """One matched symbol with provenance and rank metadata."""

    model_config = ConfigDict(extra="allow")

    lib_path: str = Field(
        ..., description="Absolute path to the .kicad_sym the match came from."
    )
    score: int = Field(
        ...,
        description=(
            "Number of query terms that matched this entry's searchable "
            "text. Higher is better. Lower-bound is 1 (a result never "
            "has zero matches — those are filtered out)."
        ),
    )
    entry: LibSymbolEntry = Field(
        ..., description="The matched symbol's fields (same shape as lib_list_symbols)."
    )


# -- input / output --------------------------------------------------------


class LibSearchSymbolInput(BaseModel):
    lib_paths: list[Path] = Field(
        ...,
        description=(
            "One or more paths to either .kicad_sym files or directories. "
            "Directories are scanned (non-recursive) for *.kicad_sym. "
            "Must be non-empty."
        ),
    )
    query: str = Field(
        ...,
        description=(
            "Whitespace-delimited search terms. AND semantics: every "
            "term must appear somewhere in the symbol's searchable text "
            "(name + reference + value + description + keywords + "
            "footprint filters). Case-insensitive."
        ),
    )
    max_results: int = Field(
        default=_DEFAULT_MAX_RESULTS,
        gt=0,
        description=(
            "Cap on the number of matches returned. The tool still "
            "reports how many hits existed before truncation in "
            "'total_before_truncate'."
        ),
    )


class LibSearchSymbolOutput(ToolOutput):
    status: Literal[
        "ok",
        "invalid_input",
        "no_libs_found",
    ]
    query: str | None = Field(default=None, description="Echo of the query string.")
    results: list[LibSearchMatch] = Field(
        default_factory=list,
        description=(
            "Matches sorted by score DESC, lib_path ASC, entry.name ASC. "
            "Truncated to max_results."
        ),
    )
    total: int = Field(
        default=0,
        description="Length of results (after truncation). Mirrors len(results).",
    )
    total_before_truncate: int = Field(
        default=0,
        description=(
            "Number of matches before max_results truncation. When this "
            "exceeds 'total', the caller can re-run with a larger cap."
        ),
    )
    libs_scanned: list[str] = Field(
        default_factory=list,
        description="Absolute paths of every .kicad_sym the search successfully parsed.",
    )
    parse_errors: list[str] = Field(
        default_factory=list,
        description=(
            "Per-lib parse/read failures, as human-readable strings. "
            "Empty when every resolved lib parsed cleanly."
        ),
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class LibSearchSymbolTool(Tool[LibSearchSymbolInput, LibSearchSymbolOutput]):
    """Keyword-search across one or more .kicad_sym libraries."""

    name = "lib_search_symbol"
    version = "0.1.0"
    description = (
        "Search for symbols across .kicad_sym libraries by keyword. "
        "Accepts a list of file or directory paths (directories are "
        "scanned for *.kicad_sym). Query terms AND together; matching "
        "is case-insensitive across name, reference, value, "
        "description, keywords, and footprint filters. Results sorted "
        "by match count."
    )
    input_model = LibSearchSymbolInput
    output_model = LibSearchSymbolOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    async def run(self, input: LibSearchSymbolInput) -> LibSearchSymbolOutput:
        # 1. Input sanity. An empty query or empty lib_paths is a bug in
        # the caller, not a zero-result search — surface it explicitly.
        terms = [t for t in input.query.lower().split() if t]
        if not terms:
            return LibSearchSymbolOutput(
                status="invalid_input",
                note="query must contain at least one non-whitespace term.",
            )
        if not input.lib_paths:
            return LibSearchSymbolOutput(
                status="invalid_input",
                note="lib_paths must be a non-empty list.",
            )

        # 2. Resolve lib_paths → concrete file paths. Directories get
        # walked (non-recursive) for *.kicad_sym. Missing paths and
        # non-lib files are recorded as parse_errors rather than
        # aborting the whole search.
        lib_files: list[Path] = []
        parse_errors: list[str] = []
        for raw in input.lib_paths:
            resolved = raw.expanduser().resolve()
            if not resolved.exists():
                parse_errors.append(f"no such path: {resolved}")
                continue
            if resolved.is_file():
                if resolved.suffix.lower() != ".kicad_sym":
                    parse_errors.append(
                        f"not a .kicad_sym file: {resolved} (got suffix "
                        f"{resolved.suffix!r})."
                    )
                    continue
                lib_files.append(resolved)
                continue
            if resolved.is_dir():
                # Non-recursive glob — matches KiCAD's own sym-lib-table
                # semantics (explicit per-lib entries, not a tree walk).
                found = sorted(resolved.glob("*.kicad_sym"))
                if not found:
                    parse_errors.append(f"no .kicad_sym files in: {resolved}")
                lib_files.extend(found)
                continue
            parse_errors.append(f"not a file or directory: {resolved}")

        if not lib_files:
            return LibSearchSymbolOutput(
                status="no_libs_found",
                query=input.query,
                parse_errors=parse_errors,
                note="none of the supplied paths yielded a .kicad_sym file.",
            )

        # 3. Search each lib. Per-lib parse errors get recorded but don't
        # stop the run — a broken fixture in one lib shouldn't hide real
        # matches in another.
        matches: list[LibSearchMatch] = []
        libs_scanned: list[str] = []
        seen_libs: set[str] = set()
        for lib_file in lib_files:
            key = str(lib_file)
            if key in seen_libs:
                # Don't double-scan when the caller passed the same lib
                # twice (e.g. once directly and once via its parent dir).
                continue
            seen_libs.add(key)
            try:
                doc = SexprDocument.from_path(lib_file)
            except SexprParseError as exc:
                parse_errors.append(f"parse failed for {lib_file}: {exc}")
                continue
            if doc.top_head != "kicad_symbol_lib":
                parse_errors.append(
                    f"not a kicad_symbol_lib: {lib_file} "
                    f"(top_head={doc.top_head or '?'!r})"
                )
                continue
            libs_scanned.append(key)
            for child in doc.root.items:
                if not isinstance(child, SList) or child.head != "symbol":
                    continue
                entry = _parse_lib_symbol(child)
                if entry is None:
                    continue
                score = _score_match(entry, terms)
                if score <= 0:
                    continue
                matches.append(
                    LibSearchMatch(
                        lib_path=key,
                        score=score,
                        entry=entry,
                    )
                )

        # 4. Deterministic sort: score DESC, lib_path ASC, name ASC.
        # All three tie-breakers matter — two symbols with the same
        # score from the same lib should still come back in a stable
        # order so downstream caching keys don't churn.
        matches.sort(key=lambda m: (-m.score, m.lib_path, m.entry.name))
        total_before = len(matches)
        truncated = matches[: input.max_results]

        return LibSearchSymbolOutput(
            status="ok",
            query=input.query,
            results=truncated,
            total=len(truncated),
            total_before_truncate=total_before,
            libs_scanned=libs_scanned,
            parse_errors=parse_errors,
        )


# -- scoring helper --------------------------------------------------------


def _score_match(entry: LibSymbolEntry, terms: list[str]) -> int:
    """Return the number of ``terms`` that appear in the entry's text blob.

    Zero means "not a match" and the caller filters it out. AND
    semantics: a result is surfaced only when *every* term hits, but
    the score still counts individual hits — a symbol can have a term
    match multiple fields and still count as one hit per term, which
    keeps the ranking stable across entries that mention a term
    redundantly (e.g. "resistor" in both name and keywords).
    """
    blob = " ".join(
        (
            entry.name,
            entry.reference,
            entry.value,
            entry.description,
            entry.keywords,
            " ".join(entry.footprint_filters),
        )
    ).lower()
    hits = 0
    for term in terms:
        if term in blob:
            hits += 1
    # AND across terms: if any term is missing, drop the match entirely
    # by returning 0. Otherwise the score is the number of distinct
    # terms that hit (equal to len(terms) when every term landed).
    if hits < len(terms):
        return 0
    return hits


__all__ = [
    "LibSearchMatch",
    "LibSearchSymbolInput",
    "LibSearchSymbolOutput",
    "LibSearchSymbolTool",
]
