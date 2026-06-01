"""sch_erc — run ERC (Electrical Rule Check) on a KiCAD schematic (M9).

The first schematic tool in KiMCP and the sibling of :mod:`pcb_drc`:
where that tool verifies the PCB against design rules, this one runs
ERC against the `.kicad_sch` to catch electrical mistakes before they
propagate to the board — unconnected pins, conflicting drivers,
hierarchical label mismatches, simulation-model issues, and so on.

Shells out to ``kicad-cli sch erc --format json`` and parses the
structured output into a typed envelope. KiCAD's ERC JSON can come in
two shapes depending on version — a flat ``violations`` array at the
top level, or a hierarchical ``sheets[].violations`` nesting (schematic
hierarchies are first-class in KiCAD, and ERC-per-sheet is the natural
grouping). We accept both, flattening into one list with ``sheet_path``
preserved on each entry so callers see a single violations feed without
losing provenance.

Status enum mirrors :mod:`pcb_drc`:

* **ok**              — ERC completed, no findings ≥ severity_floor.
* **violations**      — ERC completed, one or more findings matched.
* **sch_not_found**   — the input path is missing or not a .kicad_sch.
* **cli_failed**      — kicad-cli didn't run cleanly (timeout, non-zero
                        exit, or the CLI extra is unavailable at call
                        time). ``note`` carries the reason.
* **parse_failed**    — kicad-cli ran but the JSON on disk was
                        unparseable. Likely a KiCAD version skew — the
                        min_version gate in CliBackend should prevent
                        this in practice; if it fires, treat it as a
                        bug against the min_version setting.

Severity filtering is applied **after** parsing: kicad-cli always emits
every severity it found; this tool filters to ``severity_floor`` so
dashboards default to "warnings and errors" and CI gates can opt in to
"errors only" without re-running the CLI.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kimcp._types import Backend, ToolClass
from kimcp.backends.cli import CliBackend
from kimcp.cli.errors import CliError, CliTimeoutError
from kimcp.cli.runner import run_cli
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool

log = logging.getLogger(__name__)

# Default CLI timeout for ERC. ERC is faster than DRC on a comparable
# design (no geometry checks), but hierarchical schematics with dozens
# of sheets can still push toward the 30-60 s mark on an HDL-style
# project. 120 s matches the pcb_drc headroom; a ``timeout_sec`` knob
# can arrive when real schematics motivate it.
_ERC_TIMEOUT_SEC = 120.0

# kicad-cli severity rank — shared semantics with pcb_drc. Kept duplicated
# rather than hoisted to a shared module because the two tools' severity
# sets can diverge independently across KiCAD versions (ERC has added
# categories that DRC never emits), and coupling them through a shared
# constant would hide drift between the two.
_SEVERITY_ORDER: dict[str, int] = {
    "error": 0,
    "warning": 1,
    "info": 2,
    "exclusion": 3,
    "ignore": 4,
}
_UNKNOWN_SEVERITY_RANK = -1


# -- envelope sub-models ---------------------------------------------------


class ErcItem(BaseModel):
    """One object involved in an ERC violation (a pin, label, wire, …).

    Pass-through shape — field list mirrors kicad-cli's ``items[]`` entries
    but ``extra="allow"`` preserves any keys we haven't modeled (pos, uuid,
    sheet path, …) so the envelope stays faithful as kicad-cli evolves.
    """

    model_config = ConfigDict(extra="allow")

    description: str = ""
    uuid: str = ""


class ErcViolation(BaseModel):
    """A single ERC finding from kicad-cli.

    Field names come from kicad-cli's JSON output with one rename:
    kicad-cli calls the rule name ``type`` and we surface it as
    ``rule_id`` so it doesn't shadow Python's built-in. ``sheet_path``
    is populated when the source JSON groups findings by sheet (the
    hierarchical form); empty on the flat form. The mapping lives in
    ``_parse_all_violations``.
    """

    model_config = ConfigDict(extra="allow")

    rule_id: str
    severity: str
    description: str = ""
    items: list[ErcItem] = Field(default_factory=list)
    sheet_path: str = Field(
        default="",
        description=(
            "Schematic sheet path this finding originated from (e.g. '/', "
            "'/power/'). Populated only when the ERC JSON grouped findings "
            "by sheet; empty when the top-level 'violations' array was used."
        ),
    )


# -- input / output --------------------------------------------------------


class SchErcInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    severity_floor: Literal["error", "warning"] = Field(
        default="warning",
        description=(
            "Minimum severity to include in the result. 'warning' keeps "
            "errors+warnings; 'error' keeps errors only. kicad-cli always "
            "emits every severity it found; this filter runs post-parse."
        ),
    )
    units: Literal["mm", "in", "mils"] = Field(
        default="mm",
        description=(
            "Coordinate units for positional fields in violation items. "
            "ERC accepts 'mils' in addition to mm/in (DRC does not) because "
            "schematic coordinates historically used mils."
        ),
    )


class SchErcOutput(ToolOutput):
    status: Literal[
        "ok",
        "violations",
        "sch_not_found",
        "cli_failed",
        "parse_failed",
    ]
    sch_path: str | None = Field(
        default=None,
        description=(
            "Resolved absolute path to the .kicad_sch. Null only when the "
            "file couldn't be located at all."
        ),
    )
    violations: list[ErcViolation] = Field(
        default_factory=list,
        description=(
            "ERC findings flattened across all sheets of the schematic. "
            "Each entry preserves its originating sheet_path when the "
            "source JSON was hierarchical."
        ),
    )
    total_count: int = Field(
        default=0,
        description="Length of violations AFTER severity_floor filtering.",
    )
    coordinate_units: str = Field(
        default="",
        description="Units as reported by kicad-cli (usually mirrors input.units).",
    )
    kicad_version: str = Field(
        default="",
        description="KiCAD version from the ERC JSON — audit trail for triage.",
    )
    note: str | None = Field(
        default=None,
        description="Diagnostic string for non-ok statuses (reason + actionable hint).",
    )


# -- tool ------------------------------------------------------------------


class SchErcTool(Tool[SchErcInput, SchErcOutput]):
    """Run ERC (Electrical Rule Check) on a .kicad_sch via `kicad-cli sch erc`."""

    name = "sch_erc"
    version = "0.1.0"
    description = (
        "Run ERC on a .kicad_sch file via `kicad-cli sch erc --format json` "
        "and return the structured violations list — unconnected pins, "
        "conflicting drivers, hierarchical label mismatches, and other "
        "electrical rule findings. Hierarchical schematics are flattened "
        "into a single violations list with sheet_path preserved."
    )
    input_model = SchErcInput
    output_model = SchErcOutput
    classification = ToolClass.READ
    # CLI is the only way to service this. The dispatcher gate rejects the
    # call with BACKEND_UNAVAILABLE when `kicad-cli` isn't installed — the
    # correct signal, since the remedy is "install KiCAD", not "set a flag".
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: SchErcInput) -> SchErcOutput:
        # 1. Resolve the schematic path + do pre-flight checks before we
        # pay for a subprocess. `expanduser` catches `~/…`, `resolve`
        # normalizes both relative and absolute paths.
        sch_path = input.sch_path.expanduser().resolve()

        if not sch_path.exists():
            return SchErcOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchErcOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r}). sch_erc runs on a schematic "
                    "file, not a project or board."
                ),
            )

        # 2. Resolve the CLI. The dispatcher already gated on CLI
        # availability (preferred=(CLI,)), but we still need the resolved
        # path — and a safety belt covers the rare race where CLI was up
        # at probe time and gone by the call.
        backend = self._cli_backend
        if backend is None:
            backend = CliBackend()
        await backend.probe()
        cli_path = backend.cli_path
        if cli_path is None:
            return SchErcOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        # 3. Invoke kicad-cli. Always use `--format json -o <tempfile>`:
        # writing to file is the canonical path across kicad-cli versions;
        # some versions mix non-JSON chatter into stdout which would poison
        # a stdout-parsed result. We clean the tempdir on exit.
        with tempfile.TemporaryDirectory(prefix="kimcp-erc-") as tdir:
            report_path = Path(tdir) / "erc.json"
            argv: list[str] = [
                "sch",
                "erc",
                "--format",
                "json",
                "--units",
                input.units,
                "-o",
                str(report_path),
                str(sch_path),
            ]

            try:
                result = await run_cli(
                    tuple(argv),
                    cli_path=Path(cli_path),
                    timeout=_ERC_TIMEOUT_SEC,
                    check=False,
                )
            except CliTimeoutError as exc:
                return SchErcOutput(
                    status="cli_failed",
                    sch_path=str(sch_path),
                    note=(
                        f"kicad-cli timed out after {exc.timeout:.0f}s — "
                        "the schematic may be unusually deep; re-run with "
                        "the host KiCAD interactively to confirm."
                    ),
                )
            except CliError as exc:
                return SchErcOutput(
                    status="cli_failed",
                    sch_path=str(sch_path),
                    note=f"kicad-cli failed: {exc}",
                )

            if result.exit_code != 0:
                stderr_excerpt = (result.stderr or "").strip()[:500]
                return SchErcOutput(
                    status="cli_failed",
                    sch_path=str(sch_path),
                    note=(
                        f"kicad-cli exited {result.exit_code}: "
                        f"{stderr_excerpt or '<no stderr output>'}"
                    ),
                )

            try:
                raw_text = report_path.read_text(encoding="utf-8")
            except OSError as exc:
                return SchErcOutput(
                    status="parse_failed",
                    sch_path=str(sch_path),
                    note=f"could not read ERC report file: {exc}",
                )

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return SchErcOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"ERC JSON was not parseable: {exc}",
            )
        if not isinstance(raw, dict):
            return SchErcOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"ERC JSON top-level was a {type(raw).__name__}, expected object",
            )

        return _build_output(
            raw,
            sch_path=sch_path,
            severity_floor=input.severity_floor,
        )


# -- parsing helpers (module-level for testability) ------------------------


def _build_output(
    raw: dict[str, Any],
    *,
    sch_path: Path,
    severity_floor: Literal["error", "warning"],
) -> SchErcOutput:
    """Translate kicad-cli ERC JSON into the envelope.

    Surfaces schema-drift signals via ``meta.warnings`` instead of failing
    parse. The top-level scalars (``kicad_version`` / ``coordinate_units``)
    and the ``type`` → ``rule_id`` mapping are the fields most likely to
    change across KiCAD major releases; empty values there mean either
    kicad-cli dropped the key or renamed it. Downstream dashboards can
    alert on warnings without the tool itself becoming brittle.
    """
    violations = _parse_all_violations(raw, severity_floor)
    total = len(violations)
    status: Literal["ok", "violations"] = "ok" if total == 0 else "violations"

    coordinate_units = str(raw.get("coordinate_units", ""))
    kicad_version = str(raw.get("kicad_version", ""))

    warnings: list[str] = []
    if not kicad_version:
        warnings.append(
            "ERC JSON did not include `kicad_version` — possible kicad-cli "
            "schema drift; the audit-trail field will be empty."
        )
    if not coordinate_units:
        warnings.append(
            "ERC JSON did not include `coordinate_units` — possible "
            "kicad-cli schema drift; positional fields on violation items "
            "should still be in the units requested via `units`."
        )
    empty_rule_ids = sum(1 for v in violations if v.rule_id == "")
    if empty_rule_ids:
        warnings.append(
            f"{empty_rule_ids} ERC finding(s) had an empty rule_id — the "
            "kicad-cli `type` field may have been renamed. Re-check with "
            "`kimcp-cli` against the host KiCAD version."
        )

    out = SchErcOutput(
        status=status,
        sch_path=str(sch_path),
        violations=violations,
        total_count=total,
        coordinate_units=coordinate_units,
        kicad_version=kicad_version,
    )
    out.meta.warnings.extend(warnings)
    return out


def _parse_all_violations(
    raw: dict[str, Any],
    severity_floor: Literal["error", "warning"],
) -> list[ErcViolation]:
    """Parse the ERC JSON, handling both flat and hierarchical shapes.

    KiCAD's ERC JSON has taken two shapes historically:

    * Flat: ``{"violations": [...]}`` at the top level.
    * Hierarchical: ``{"sheets": [{"path": "/", "violations": [...]}, ...]}``,
      reflecting the schematic sheet tree.

    We accept both. Flat findings get ``sheet_path=""``; hierarchical
    findings get the containing sheet's path. The two shapes are checked
    in order — if the flat key is present and a list, we treat this as
    the flat form even if ``sheets`` is also present.
    """
    flat = raw.get("violations")
    if isinstance(flat, list):
        return _parse_violations_array(flat, severity_floor, sheet_path="")

    sheets = raw.get("sheets", [])
    if not isinstance(sheets, list):
        return []

    out: list[ErcViolation] = []
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        sheet_path = str(sheet.get("path", ""))
        sheet_violations = sheet.get("violations", [])
        if isinstance(sheet_violations, list):
            out.extend(
                _parse_violations_array(
                    sheet_violations, severity_floor, sheet_path=sheet_path
                )
            )
    return out


def _parse_violations_array(
    raw_list: list[Any],
    severity_floor: Literal["error", "warning"],
    *,
    sheet_path: str,
) -> list[ErcViolation]:
    """Parse one violations array and apply the severity floor.

    Unknown severities (anything outside ``_SEVERITY_ORDER``) are KEPT —
    they rank at ``_UNKNOWN_SEVERITY_RANK`` (-1), which is strictly less
    than any valid floor rank, so the ``rank > floor_rank`` filter always
    passes them. If kicad-cli adds a new severity tier upstream we surface
    it rather than silently dropping it.
    """
    out: list[ErcViolation] = []
    # Floor is Pydantic-constrained to "error"/"warning" — both in the map;
    # the fallback is defensive only.
    floor_rank = _SEVERITY_ORDER.get(severity_floor, _UNKNOWN_SEVERITY_RANK)
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        severity = str(entry.get("severity", ""))
        if _SEVERITY_ORDER.get(severity, _UNKNOWN_SEVERITY_RANK) > floor_rank:
            continue
        raw_items = entry.get("items", [])
        items: list[ErcItem] = []
        if isinstance(raw_items, list):
            for i in raw_items:
                if isinstance(i, dict):
                    items.append(ErcItem.model_validate(i))
        out.append(
            ErcViolation(
                rule_id=str(entry.get("type", "")),
                severity=severity,
                description=str(entry.get("description", "")),
                items=items,
                sheet_path=sheet_path,
            )
        )
    return out


__all__ = [
    "ErcItem",
    "ErcViolation",
    "SchErcInput",
    "SchErcOutput",
    "SchErcTool",
]
