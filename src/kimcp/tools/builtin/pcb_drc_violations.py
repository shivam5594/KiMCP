"""pcb_drc_violations — structured filter + aggregate view over DRC output.

``pcb_drc`` runs the CLI and returns three flat buckets of findings.
That shape is faithful to kicad-cli but painful for the natural LLM
triage questions:

* "Just show me the clearance violations, not every warning."
* "How many findings per rule_id, highest-severity first?"
* "Show me parity issues only — I fixed routing, now check the ECO."

Rather than teach every caller how to filter + count the three
buckets, this tool wraps ``pcb_drc`` and exposes a richer query
surface. It runs the CLI *once* internally via ``PcbDrcTool.run()``
and shapes the result.

What's different from ``pcb_drc`` (besides the filters):

* **Flat output list** — each finding carries a ``bucket`` tag
  (``'violations'`` / ``'unconnected'`` / ``'parity'``) so you can
  iterate without switching lists. Aggregation is one linear pass.
* **Severity extension** — accepts ``severity_floor='info'`` too,
  whereas ``pcb_drc`` only goes down to ``'warning'``. The floor is
  applied after ``pcb_drc`` ran with the most permissive filter we
  can request, so ``info`` findings survive into this view.
* **Aggregates** — counts by rule_id / severity / bucket, returned
  alongside the rows. Lets a dashboard render a histogram without
  re-scanning the list client-side.
* **``include_items=False``** — drop per-item arrays when the caller
  only needs counts. Shrinks payloads on boards with hundreds of
  findings.

Status enum pass-through: we surface whatever ``pcb_drc`` returned
(``ok`` / ``violations`` / ``pcb_not_found`` / ``cli_failed`` /
``parse_failed``). No new failure modes — this tool is pure shaping.

READ classification: delegates to ``pcb_drc`` which is READ.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kimcp._types import Backend, ToolClass
from kimcp.backends.cli import CliBackend
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool
from kimcp.tools.builtin.pcb_drc import (
    DrcItem,
    DrcViolation,
    PcbDrcInput,
    PcbDrcOutput,
    PcbDrcTool,
)

log = logging.getLogger(__name__)


# Severity ranks extended one tier below pcb_drc's floor so we can
# surface info-level findings. Unknown severities rank at -1 and
# pass every floor (same policy as pcb_drc).
_SEVERITY_ORDER: dict[str, int] = {
    "error": 0,
    "warning": 1,
    "info": 2,
    "exclusion": 3,
    "ignore": 4,
}
_UNKNOWN_SEVERITY_RANK = -1


Bucket = Literal["violations", "unconnected", "parity"]


# -- envelope sub-models ---------------------------------------------------


class DrcViolationRow(BaseModel):
    """One DRC finding, flattened with a bucket discriminator.

    Same fields as ``DrcViolation`` from ``pcb_drc`` plus a ``bucket``
    tag. Items can be omitted via ``include_items=False`` in the input
    — in that case the list is empty.
    """

    model_config = ConfigDict(extra="allow")

    bucket: Bucket = Field(
        ...,
        description=(
            "Which of kicad-cli's three lists this row came from: "
            "``'violations'`` (design-rule), ``'unconnected'`` "
            "(ratsnest), or ``'parity'`` (schematic-vs-PCB)."
        ),
    )
    rule_id: str = Field(
        ...,
        description=(
            "Rule identifier (KiCAD's ``type`` field) — ``'clearance'``, "
            "``'silk_over_copper'``, ``'unconnected_items'``, etc."
        ),
    )
    severity: str = Field(..., description="``'error'`` / ``'warning'`` / ``'info'`` / …")
    description: str = Field(default="")
    items: list[DrcItem] = Field(
        default_factory=list,
        description=(
            "Per-item detail (tracks, pads, footprints involved). Empty "
            "when the caller passed ``include_items=False``."
        ),
    )


# -- input / output --------------------------------------------------------


class PcbDrcViolationsInput(BaseModel):
    pcb_path: Path = Field(
        ..., description="Path to the .kicad_pcb file."
    )
    rule_ids: list[str] | None = Field(
        default=None,
        description=(
            "Filter to findings whose ``rule_id`` matches one of these "
            "strings (exact match). Null returns every rule."
        ),
    )
    severity_floor: Literal["error", "warning", "info"] = Field(
        default="warning",
        description=(
            "Minimum severity to include. ``'error'`` keeps errors only; "
            "``'warning'`` keeps errors + warnings (default); ``'info'`` "
            "keeps everything."
        ),
    )
    description_contains: str | None = Field(
        default=None,
        description=(
            "Substring-match filter on the ``description`` field "
            "(case-sensitive). Null disables the filter."
        ),
    )
    buckets: list[Bucket] | None = Field(
        default=None,
        description=(
            "Filter to these source buckets: ``'violations'``, "
            "``'unconnected'``, or ``'parity'``. Null returns all three."
        ),
    )
    schematic_parity: bool = Field(
        default=False,
        description=(
            "Forwarded to ``pcb_drc``: also run the schematic-vs-PCB check. "
            "Required if you want parity findings in the result."
        ),
    )
    units: Literal["mm", "in"] = Field(
        default="mm",
        description="Coordinate units, forwarded to ``pcb_drc``.",
    )
    include_items: bool = Field(
        default=True,
        description=(
            "Include per-finding ``items[]`` arrays. Set to False for a "
            "counts-only summary — cheaper payload on boards with many "
            "findings."
        ),
    )


class PcbDrcViolationsOutput(ToolOutput):
    status: Literal[
        "ok",
        "violations",
        "pcb_not_found",
        "cli_failed",
        "parse_failed",
    ]
    pcb_path: str | None = Field(default=None)
    rows: list[DrcViolationRow] = Field(
        default_factory=list,
        description=(
            "Flat filtered findings. Sorted by (severity rank, rule_id) "
            "so errors sort before warnings and each severity group is "
            "alphabetized by rule."
        ),
    )
    total_count: int = Field(
        default=0,
        description="Count of rows after all filters.",
    )
    count_by_rule_id: dict[str, int] = Field(
        default_factory=dict,
        description="Counts of filtered rows grouped by ``rule_id``.",
    )
    count_by_severity: dict[str, int] = Field(
        default_factory=dict,
        description="Counts of filtered rows grouped by ``severity``.",
    )
    count_by_bucket: dict[str, int] = Field(
        default_factory=dict,
        description="Counts of filtered rows grouped by source bucket.",
    )
    kicad_version: str = Field(default="")
    coordinate_units: str = Field(default="")
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class PcbDrcViolationsTool(Tool[PcbDrcViolationsInput, PcbDrcViolationsOutput]):
    """Structured filter + aggregate view over ``pcb_drc`` output."""

    name = "pcb_drc_violations"
    version = "0.1.0"
    description = (
        "Query the DRC result for a .kicad_pcb with richer filters than "
        "pcb_drc: rule_id match, severity floor down to 'info', description "
        "substring, bucket selection, and counts by rule / severity / bucket. "
        "Runs kicad-cli once internally."
    )
    input_model = PcbDrcViolationsInput
    output_model = PcbDrcViolationsOutput
    classification = ToolClass.READ
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(
        self,
        drc_tool: PcbDrcTool | None = None,
        cli_backend: CliBackend | None = None,
    ) -> None:
        # Allow injection for testing. In production, construct a
        # PcbDrcTool lazily so the dispatcher's CLI gate fires on the
        # underlying tool — same remediation path ("install KiCAD") as
        # every other CLI-backed tool.
        self._drc_tool = drc_tool
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(
        self, input: PcbDrcViolationsInput
    ) -> PcbDrcViolationsOutput:
        drc_tool = self._drc_tool
        if drc_tool is None:
            drc_tool = PcbDrcTool()
            if self._cli_backend is not None:
                drc_tool.set_cli_backend(self._cli_backend)

        # Delegate the CLI run to pcb_drc. We always ask for the
        # broadest severity floor it accepts ('warning') so that when
        # the caller requested 'info' we still have those findings to
        # filter; errors/warnings are already inside 'warning'.
        #
        # Note: pcb_drc's severity_floor literal excludes 'info' by
        # design (pcb_drc is meant for CI gates, not dashboards). We
        # apply the 'info' floor ourselves after the call.
        drc_floor: Literal["error", "warning"] = (
            "error" if input.severity_floor == "error" else "warning"
        )
        drc_out = await drc_tool.run(
            PcbDrcInput(
                pcb_path=input.pcb_path,
                severity_floor=drc_floor,
                schematic_parity=input.schematic_parity,
                units=input.units,
            )
        )

        # Non-success from pcb_drc: propagate status + note, leave
        # aggregates empty. Client sees the same error taxonomy.
        if drc_out.status in {"pcb_not_found", "cli_failed", "parse_failed"}:
            return PcbDrcViolationsOutput(
                status=drc_out.status,
                pcb_path=drc_out.pcb_path,
                note=drc_out.note,
                kicad_version=drc_out.kicad_version,
                coordinate_units=drc_out.coordinate_units,
            )

        # Flatten the three buckets with their tags.
        rows = list(_iter_rows(drc_out))

        # Apply our richer filters.
        bucket_filter: frozenset[str] | None = (
            frozenset(input.buckets) if input.buckets is not None else None
        )
        rule_filter: frozenset[str] | None = (
            frozenset(input.rule_ids) if input.rule_ids is not None else None
        )
        floor_rank = _SEVERITY_ORDER.get(
            input.severity_floor, _UNKNOWN_SEVERITY_RANK
        )

        filtered: list[DrcViolationRow] = []
        for row in rows:
            if bucket_filter is not None and row.bucket not in bucket_filter:
                continue
            if rule_filter is not None and row.rule_id not in rule_filter:
                continue
            sev_rank = _SEVERITY_ORDER.get(row.severity, _UNKNOWN_SEVERITY_RANK)
            if sev_rank > floor_rank:
                continue
            if (
                input.description_contains is not None
                and input.description_contains not in row.description
            ):
                continue
            if not input.include_items:
                # Drop items defensively — model_copy with update to
                # avoid mutating the row constructed upstream.
                row = row.model_copy(update={"items": []})
            filtered.append(row)

        # Sort: severity rank ascending (error first), then rule_id
        # alpha. Unknown severities (rank -1) sort before error —
        # intentional: something we didn't model is louder than
        # something we did.
        filtered.sort(
            key=lambda r: (
                _SEVERITY_ORDER.get(r.severity, _UNKNOWN_SEVERITY_RANK),
                r.rule_id,
            )
        )

        # Aggregates — single pass.
        by_rule: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_bucket: dict[str, int] = {}
        for row in filtered:
            by_rule[row.rule_id] = by_rule.get(row.rule_id, 0) + 1
            by_severity[row.severity] = by_severity.get(row.severity, 0) + 1
            by_bucket[row.bucket] = by_bucket.get(row.bucket, 0) + 1

        # Status echoes pcb_drc's contract: 'ok' iff no rows remained
        # after filtering; 'violations' otherwise. We can't stay 'ok'
        # just because filtering emptied a list that actually had
        # findings — callers would misread that as "clean board". The
        # counts tell the honest story either way.
        status: Literal["ok", "violations"] = (
            "violations" if filtered else "ok"
        )

        return PcbDrcViolationsOutput(
            status=status,
            pcb_path=drc_out.pcb_path,
            rows=filtered,
            total_count=len(filtered),
            count_by_rule_id=by_rule,
            count_by_severity=by_severity,
            count_by_bucket=by_bucket,
            kicad_version=drc_out.kicad_version,
            coordinate_units=drc_out.coordinate_units,
        )


# -- helpers ---------------------------------------------------------------


def _iter_rows(drc_out: PcbDrcOutput) -> list[DrcViolationRow]:
    """Flatten pcb_drc's three buckets into tagged rows.

    The bucket field is the only new information — everything else
    passes through from ``DrcViolation``. We build a fresh list
    rather than wrapping so the result can be freely filtered /
    sorted / mutated without touching the upstream output.
    """
    out: list[DrcViolationRow] = []
    _append_bucket(out, drc_out.violations, "violations")
    _append_bucket(out, drc_out.unconnected_items, "unconnected")
    _append_bucket(out, drc_out.schematic_parity_issues, "parity")
    return out


def _append_bucket(
    out: list[DrcViolationRow],
    source: list[DrcViolation],
    bucket: Bucket,
) -> None:
    for v in source:
        out.append(
            DrcViolationRow(
                bucket=bucket,
                rule_id=v.rule_id,
                severity=v.severity,
                description=v.description,
                items=list(v.items),
            )
        )


__all__ = [
    "Bucket",
    "DrcViolationRow",
    "PcbDrcViolationsInput",
    "PcbDrcViolationsOutput",
    "PcbDrcViolationsTool",
]
