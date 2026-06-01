"""pcb_drc — run DRC (Design Rule Check) on a KiCAD PCB.

The first manufacturing-output tool in KiMCP, and the first tool whose
answer a real KiCAD user actually cares about. Shells out to
``kicad-cli pcb drc --format json`` and parses the structured output
into a typed envelope.

Three lists come back, matching kicad-cli's schema:

* ``violations`` — per-rule design-rule findings (clearance, track
  width, silkscreen-over-copper, …).
* ``unconnected_items`` — nets/pins that ratsnest says should connect
  but physically don't.
* ``schematic_parity_issues`` — schematic-vs-PCB mismatches (present
  only when the caller opts in via ``schematic_parity=True``).

Each entry is a :class:`DrcViolation` with ``rule_id`` / ``severity`` /
``description`` / ``items`` — plus any future fields kicad-cli adds,
via Pydantic's ``extra="allow"`` so schema drift upstream doesn't
silently drop data.

Status enum distinguishes verdicts so callers can branch without
string-matching:

* **ok**               — DRC completed, no findings ≥ severity_floor.
* **violations**       — DRC completed, one or more findings matched.
* **pcb_not_found**    — the input path is missing or not a .kicad_pcb.
* **cli_failed**       — kicad-cli didn't run cleanly (timeout,
                         non-zero exit, or the CLI extra is unavailable
                         at call time). ``note`` carries the reason.
* **parse_failed**     — kicad-cli ran but the JSON on disk was
                         unparseable. Likely a KiCAD version skew — the
                         min_version gate in CliBackend should prevent
                         this in practice; if it fires, treat it as a
                         bug against the min_version setting.

Severity filtering is applied **after** parsing: kicad-cli always emits
every severity it found; this tool filters to
``severity_floor`` so dashboards default to "warnings and errors" and
CI gates can opt in to "errors only" without re-running the CLI.
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

# Default CLI timeout for DRC. A dense mixed-signal board hits the
# 30-60 s mark; 120 s gives headroom without letting a pathologically
# slow run hang the server indefinitely. Callers that know their board
# is huge can't override today (keeping the input surface minimal for
# M6); a `timeout_sec` knob arrives when real-world boards motivate it.
_DRC_TIMEOUT_SEC = 120.0

# kicad-cli severity rank, error-worst → ignore-best. Values outside this
# map rank at `_UNKNOWN_SEVERITY_RANK` (strictly more severe than 'error')
# so unknown severities never get filtered out — if KiCAD adds a new tier
# upstream we'd rather surface it than silently drop it.
_SEVERITY_ORDER: dict[str, int] = {
    "error": 0,
    "warning": 1,
    "info": 2,
    "exclusion": 3,
    "ignore": 4,
}
# Sentinel rank for severities we haven't modelled. `_parse_violations`
# filters on `rank > floor_rank`, so a rank of -1 always passes.
_UNKNOWN_SEVERITY_RANK = -1


# -- envelope sub-models ---------------------------------------------------


class DrcItem(BaseModel):
    """One object involved in a DRC violation (a track, pad, footprint, …).

    Pass-through shape — field list mirrors kicad-cli's ``items[]`` entries
    but ``extra="allow"`` preserves any keys we haven't modeled (position,
    uuid, layer, …) so the envelope stays faithful as kicad-cli evolves.
    """

    model_config = ConfigDict(extra="allow")

    description: str = ""
    uuid: str = ""


class DrcViolation(BaseModel):
    """A single DRC finding from kicad-cli.

    Field names come from kicad-cli's JSON output, with one rename:
    kicad-cli calls the field ``type`` (the rule name) and we surface it
    as ``rule_id`` so it doesn't shadow Python's built-in and so the
    envelope reads naturally. The mapping lives in ``_parse_violations``.
    """

    model_config = ConfigDict(extra="allow")

    rule_id: str
    severity: str
    description: str = ""
    items: list[DrcItem] = Field(default_factory=list)


# -- input / output --------------------------------------------------------


class PcbDrcInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    severity_floor: Literal["error", "warning"] = Field(
        default="warning",
        description=(
            "Minimum severity to include in the result. 'warning' keeps "
            "errors+warnings; 'error' keeps errors only. kicad-cli always "
            "emits every severity it found; this filter runs post-parse."
        ),
    )
    schematic_parity: bool = Field(
        default=False,
        description=(
            "Also check schematic-vs-PCB parity. Requires the sibling "
            ".kicad_sch file to exist next to the .kicad_pcb — kicad-cli "
            "will surface an error in `schematic_parity_issues` if it doesn't."
        ),
    )
    units: Literal["mm", "in"] = Field(
        default="mm",
        description="Coordinate units for positional fields in violation items.",
    )


class PcbDrcOutput(ToolOutput):
    status: Literal[
        "ok",
        "violations",
        "pcb_not_found",
        "cli_failed",
        "parse_failed",
    ]
    pcb_path: str | None = Field(
        default=None,
        description=(
            "Resolved absolute path to the .kicad_pcb. Null only when the "
            "file couldn't be located at all."
        ),
    )
    violations: list[DrcViolation] = Field(
        default_factory=list,
        description="Design-rule findings (clearance, width, silkscreen, …).",
    )
    unconnected_items: list[DrcViolation] = Field(
        default_factory=list,
        description="Nets/pins ratsnest says should connect but don't.",
    )
    schematic_parity_issues: list[DrcViolation] = Field(
        default_factory=list,
        description=(
            "Schematic-vs-PCB mismatches. Empty unless schematic_parity=True "
            "was requested AND a sibling .kicad_sch was present."
        ),
    )
    total_count: int = Field(
        default=0,
        description="Sum of the three lists AFTER severity_floor filtering.",
    )
    coordinate_units: str = Field(
        default="",
        description="Units as reported by kicad-cli (usually mirrors input.units).",
    )
    kicad_version: str = Field(
        default="",
        description="KiCAD version from the DRC JSON — audit trail for triage.",
    )
    note: str | None = Field(
        default=None,
        description="Diagnostic string for non-ok statuses (reason + actionable hint).",
    )


# -- tool ------------------------------------------------------------------


class PcbDrcTool(Tool[PcbDrcInput, PcbDrcOutput]):
    """Run DRC (Design Rule Check) on a .kicad_pcb via `kicad-cli pcb drc`."""

    name = "pcb_drc"
    version = "0.1.0"
    description = (
        "Run DRC on a .kicad_pcb file via `kicad-cli pcb drc --format json` "
        "and return the structured violations, unconnected items, and "
        "optional schematic-parity findings."
    )
    input_model = PcbDrcInput
    output_model = PcbDrcOutput
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

    async def run(self, input: PcbDrcInput) -> PcbDrcOutput:
        # 1. Resolve the PCB path + do pre-flight checks before we pay for
        # a subprocess. `expanduser` catches `~/…`, `resolve` normalizes
        # both relative and absolute paths.
        pcb_path = input.pcb_path.expanduser().resolve()

        if not pcb_path.exists():
            return PcbDrcOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbDrcOutput(
                status="pcb_not_found",
                pcb_path=str(pcb_path),
                note=(
                    f"not a .kicad_pcb file: {pcb_path} (got suffix "
                    f"{pcb_path.suffix!r}). pcb_drc runs on a board file, "
                    "not a project or schematic."
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
            return PcbDrcOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        # 3. Invoke kicad-cli. Always use `--format json -o <tempfile>`:
        # writing to file is the canonical path across kicad-cli versions;
        # some versions mix non-JSON chatter into stdout which would
        # poison a stdout-parsed result. We clean the tempdir on exit.
        with tempfile.TemporaryDirectory(prefix="kimcp-drc-") as tdir:
            report_path = Path(tdir) / "drc.json"
            argv: list[str] = [
                "pcb",
                "drc",
                "--format",
                "json",
                "--units",
                input.units,
                "-o",
                str(report_path),
            ]
            if input.schematic_parity:
                argv.append("--schematic-parity")
            argv.append(str(pcb_path))

            try:
                result = await run_cli(
                    tuple(argv),
                    cli_path=Path(cli_path),
                    timeout=_DRC_TIMEOUT_SEC,
                    check=False,
                )
            except CliTimeoutError as exc:
                return PcbDrcOutput(
                    status="cli_failed",
                    pcb_path=str(pcb_path),
                    note=(
                        f"kicad-cli timed out after {exc.timeout:.0f}s — "
                        "the board may be unusually large; re-run with the "
                        "host KiCAD interactively to confirm."
                    ),
                )
            except CliError as exc:
                return PcbDrcOutput(
                    status="cli_failed",
                    pcb_path=str(pcb_path),
                    note=f"kicad-cli failed: {exc}",
                )

            if result.exit_code != 0:
                stderr_excerpt = (result.stderr or "").strip()[:500]
                return PcbDrcOutput(
                    status="cli_failed",
                    pcb_path=str(pcb_path),
                    note=(
                        f"kicad-cli exited {result.exit_code}: "
                        f"{stderr_excerpt or '<no stderr output>'}"
                    ),
                )

            try:
                raw_text = report_path.read_text(encoding="utf-8")
            except OSError as exc:
                return PcbDrcOutput(
                    status="parse_failed",
                    pcb_path=str(pcb_path),
                    note=f"could not read DRC report file: {exc}",
                )

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return PcbDrcOutput(
                status="parse_failed",
                pcb_path=str(pcb_path),
                note=f"DRC JSON was not parseable: {exc}",
            )
        if not isinstance(raw, dict):
            return PcbDrcOutput(
                status="parse_failed",
                pcb_path=str(pcb_path),
                note=f"DRC JSON top-level was a {type(raw).__name__}, expected object",
            )

        return _build_output(
            raw,
            pcb_path=pcb_path,
            severity_floor=input.severity_floor,
        )


# -- parsing helpers (module-level for testability) ------------------------


def _build_output(
    raw: dict[str, Any],
    *,
    pcb_path: Path,
    severity_floor: Literal["error", "warning"],
) -> PcbDrcOutput:
    """Translate kicad-cli DRC JSON into the envelope.

    Surfaces schema-drift signals via ``meta.warnings`` instead of failing
    parse. The top-level scalars (``kicad_version`` / ``coordinate_units``)
    and the ``type`` → ``rule_id`` mapping are the fields most likely to
    change across KiCAD major releases; empty values there mean either
    kicad-cli dropped the key or renamed it. Downstream dashboards can
    alert on warnings without the tool itself becoming brittle.
    """
    violations = _parse_violations(raw.get("violations", []), severity_floor)
    unconnected = _parse_violations(raw.get("unconnected_items", []), severity_floor)
    parity = _parse_violations(raw.get("schematic_parity", []), severity_floor)
    total = len(violations) + len(unconnected) + len(parity)
    status: Literal["ok", "violations"] = "ok" if total == 0 else "violations"

    coordinate_units = str(raw.get("coordinate_units", ""))
    kicad_version = str(raw.get("kicad_version", ""))

    warnings: list[str] = []
    if not kicad_version:
        warnings.append(
            "DRC JSON did not include `kicad_version` — possible kicad-cli "
            "schema drift; the audit-trail field will be empty."
        )
    if not coordinate_units:
        warnings.append(
            "DRC JSON did not include `coordinate_units` — possible "
            "kicad-cli schema drift; positional fields on violation items "
            "should still be in the units requested via `units`."
        )
    empty_rule_ids = sum(
        1
        for v in (*violations, *unconnected, *parity)
        if v.rule_id == ""
    )
    if empty_rule_ids:
        warnings.append(
            f"{empty_rule_ids} DRC finding(s) had an empty rule_id — the "
            "kicad-cli `type` field may have been renamed. Re-check with "
            "`kimcp-cli` against the host KiCAD version."
        )

    out = PcbDrcOutput(
        status=status,
        pcb_path=str(pcb_path),
        violations=violations,
        unconnected_items=unconnected,
        schematic_parity_issues=parity,
        total_count=total,
        coordinate_units=coordinate_units,
        kicad_version=kicad_version,
    )
    out.meta.warnings.extend(warnings)
    return out


def _parse_violations(
    raw_list: Any,
    severity_floor: Literal["error", "warning"],
) -> list[DrcViolation]:
    """Parse a kicad-cli violations array and apply the severity floor.

    Unknown severities (anything outside ``_SEVERITY_ORDER``) are KEPT —
    they rank at ``_UNKNOWN_SEVERITY_RANK`` (-1), which is strictly less
    than any valid floor rank, so the ``rank > floor_rank`` filter always
    passes them. If kicad-cli adds a new severity tier upstream we surface
    it rather than silently dropping it.
    """
    if not isinstance(raw_list, list):
        return []
    out: list[DrcViolation] = []
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
        items: list[DrcItem] = []
        if isinstance(raw_items, list):
            for i in raw_items:
                if isinstance(i, dict):
                    items.append(DrcItem.model_validate(i))
        out.append(
            DrcViolation(
                rule_id=str(entry.get("type", "")),
                severity=severity,
                description=str(entry.get("description", "")),
                items=items,
            )
        )
    return out


__all__ = [
    "DrcItem",
    "DrcViolation",
    "PcbDrcInput",
    "PcbDrcOutput",
    "PcbDrcTool",
]
