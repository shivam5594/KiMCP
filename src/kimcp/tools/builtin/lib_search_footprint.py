"""lib_search_footprint — keyword-search across .pretty footprint libs.

Companion to ``lib_list_footprints`` (M+N), mirroring
``lib_search_symbol`` on the footprint side. Listing browses one
shelf; this tool finds a footprint matching a fuzzy intent across
many.

The plumbing differs from ``lib_search_symbol`` in one place:
footprint libs are *directories* of per-footprint ``.kicad_mod``
files. Inputs are therefore directory paths (or single ``.kicad_mod``
files for ad-hoc searches). Directories are scanned non-recursively
for ``*.kicad_mod``, matching how KiCAD's fp-lib-table reads
``.pretty`` libraries.

Scope of the first ship (identical to ``lib_search_symbol``):

* **AND semantics across terms** — every whitespace-delimited token
  in ``query`` must appear in the footprint's searchable blob
  (name + file + description + tags + attributes).
* **Case-insensitive**.
* **Best-effort parsing** — one broken ``.kicad_mod`` file doesn't
  poison the whole search; failures land in ``parse_errors``.
* **Per-match score** = how many query terms hit the blob. Sorted
  by score DESC, lib_dir ASC, name ASC for determinism.

Status enum:

* **ok**             — search ran; ``results`` may be empty.
* **invalid_input**  — empty query or empty lib_paths.
* **no_libs_found**  — none of the paths resolved to a usable lib.

READ classification.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kimcp._types import Backend, ToolClass
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool
from kimcp.tools.builtin.lib_list_footprints import (
    LibFootprintEntry,
    _parse_footprint_file,
)

log = logging.getLogger(__name__)


_DEFAULT_MAX_RESULTS = 50


# -- envelope sub-models ---------------------------------------------------


class LibFootprintSearchMatch(BaseModel):
    """One matched footprint with provenance and rank metadata."""

    model_config = ConfigDict(extra="allow")

    lib_dir: str = Field(
        ...,
        description=(
            "Absolute path to the .pretty directory (or containing folder) "
            "the match came from."
        ),
    )
    score: int = Field(
        ...,
        description=(
            "Number of query terms that matched this footprint's searchable "
            "text. Higher is better. Results with score 0 are filtered out."
        ),
    )
    entry: LibFootprintEntry = Field(
        ...,
        description="The matched footprint (same shape as lib_list_footprints).",
    )


# -- input / output --------------------------------------------------------


class LibSearchFootprintInput(BaseModel):
    lib_paths: list[Path] = Field(
        ...,
        description=(
            "One or more paths. A directory is treated as a .pretty library "
            "and scanned (non-recursive) for *.kicad_mod. A single "
            ".kicad_mod file is searched on its own. Must be non-empty."
        ),
    )
    query: str = Field(
        ...,
        description=(
            "Whitespace-delimited search terms. AND semantics: every term "
            "must appear somewhere in the footprint's searchable text "
            "(name + file + description + tags + attributes). "
            "Case-insensitive."
        ),
    )
    max_results: int = Field(
        default=_DEFAULT_MAX_RESULTS,
        gt=0,
        description=(
            "Cap on returned matches. ``total_before_truncate`` reports how "
            "many hits existed before truncation."
        ),
    )


class LibSearchFootprintOutput(ToolOutput):
    status: Literal[
        "ok",
        "invalid_input",
        "no_libs_found",
    ]
    query: str | None = Field(default=None)
    results: list[LibFootprintSearchMatch] = Field(
        default_factory=list,
        description=(
            "Matches sorted by score DESC, lib_dir ASC, entry.name ASC. "
            "Truncated to max_results."
        ),
    )
    total: int = Field(
        default=0,
        description="Length of ``results`` after truncation.",
    )
    total_before_truncate: int = Field(
        default=0,
        description="Match count before truncation — re-run with a larger cap to see all.",
    )
    libs_scanned: list[str] = Field(
        default_factory=list,
        description="Absolute paths of every .pretty dir or .kicad_mod file the search processed.",
    )
    parse_errors: list[str] = Field(
        default_factory=list,
        description=(
            "Per-file read / parse / schema errors. Empty when every "
            "input path resolved and every encountered .kicad_mod parsed."
        ),
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class LibSearchFootprintTool(Tool[LibSearchFootprintInput, LibSearchFootprintOutput]):
    """Keyword-search across one or more footprint libraries."""

    name = "lib_search_footprint"
    version = "0.1.0"
    description = (
        "Search for footprints across .pretty directories (or individual "
        ".kicad_mod files) by keyword. Query terms AND together; matching is "
        "case-insensitive across name, file stem, description, tags, and "
        "attributes. Results sorted by match count."
    )
    input_model = LibSearchFootprintInput
    output_model = LibSearchFootprintOutput
    classification = ToolClass.READ
    mutates = False
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    async def run(
        self, input: LibSearchFootprintInput
    ) -> LibSearchFootprintOutput:
        # 1. Input sanity.
        terms = [t for t in input.query.lower().split() if t]
        if not terms:
            return LibSearchFootprintOutput(
                status="invalid_input",
                note="query must contain at least one non-whitespace term.",
            )
        if not input.lib_paths:
            return LibSearchFootprintOutput(
                status="invalid_input",
                note="lib_paths must be a non-empty list.",
            )

        # 2. Resolve paths → (lib_dir, file) pairs. Directories are
        # walked non-recursively; single .kicad_mod files act as their
        # own "lib" with the file's parent as the dir.
        pairs: list[tuple[Path, Path]] = []
        parse_errors: list[str] = []
        libs_scanned: list[str] = []
        seen_dirs: set[str] = set()

        for raw in input.lib_paths:
            resolved = raw.expanduser().resolve()
            if not resolved.exists():
                parse_errors.append(f"no such path: {resolved}")
                continue
            if resolved.is_file():
                if resolved.suffix.lower() != ".kicad_mod":
                    parse_errors.append(
                        f"not a .kicad_mod file: {resolved} (got suffix "
                        f"{resolved.suffix!r})."
                    )
                    continue
                pairs.append((resolved.parent, resolved))
                key = str(resolved)
                if key not in seen_dirs:
                    libs_scanned.append(key)
                    seen_dirs.add(key)
                continue
            if resolved.is_dir():
                found = sorted(resolved.glob("*.kicad_mod"))
                if not found:
                    parse_errors.append(
                        f"no .kicad_mod files in: {resolved}"
                    )
                for f in found:
                    pairs.append((resolved, f))
                dir_key = str(resolved)
                if found and dir_key not in seen_dirs:
                    libs_scanned.append(dir_key)
                    seen_dirs.add(dir_key)
                continue
            parse_errors.append(f"not a file or directory: {resolved}")

        if not pairs:
            return LibSearchFootprintOutput(
                status="no_libs_found",
                query=input.query,
                parse_errors=parse_errors,
                libs_scanned=libs_scanned,
                note=(
                    "none of the supplied paths yielded a .kicad_mod file."
                ),
            )

        # 3. De-duplicate exact (dir, file) pairs so passing the same
        # lib multiple ways (path + parent dir) doesn't double-score.
        seen_files: set[str] = set()
        unique_pairs: list[tuple[Path, Path]] = []
        for lib_dir, file in pairs:
            file_key = str(file)
            if file_key in seen_files:
                continue
            seen_files.add(file_key)
            unique_pairs.append((lib_dir, file))

        # 4. Parse + score. Per-file failures land in parse_errors.
        matches: list[LibFootprintSearchMatch] = []
        for lib_dir, file in unique_pairs:
            parsed = _parse_footprint_file(file)
            if not isinstance(parsed, LibFootprintEntry):
                # Skip entries land in parse_errors with file provenance
                # so a caller can pinpoint the broken file.
                parse_errors.append(f"{file}: {parsed.reason}")
                continue
            score = _score_match(parsed, terms)
            if score <= 0:
                continue
            matches.append(
                LibFootprintSearchMatch(
                    lib_dir=str(lib_dir),
                    score=score,
                    entry=parsed,
                )
            )

        # 5. Deterministic sort: score DESC, lib_dir ASC, name ASC.
        matches.sort(key=lambda m: (-m.score, m.lib_dir, m.entry.name))
        total_before = len(matches)
        truncated = matches[: input.max_results]

        return LibSearchFootprintOutput(
            status="ok",
            query=input.query,
            results=truncated,
            total=len(truncated),
            total_before_truncate=total_before,
            libs_scanned=libs_scanned,
            parse_errors=parse_errors,
        )


# -- scoring helper --------------------------------------------------------


def _score_match(entry: LibFootprintEntry, terms: list[str]) -> int:
    """Return the number of ``terms`` matched across the entry's blob.

    Searchable blob: name + file + description + tags + attributes.
    AND semantics — missing any term drops the match to 0.
    """
    blob = " ".join(
        (
            entry.name,
            entry.file,
            entry.description,
            entry.tags,
            " ".join(entry.attributes),
        )
    ).lower()
    hits = 0
    for term in terms:
        if term in blob:
            hits += 1
    if hits < len(terms):
        return 0
    return hits


__all__ = [
    "LibFootprintSearchMatch",
    "LibSearchFootprintInput",
    "LibSearchFootprintOutput",
    "LibSearchFootprintTool",
]
