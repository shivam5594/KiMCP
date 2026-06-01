"""sch_compose — batched primitive that runs many schematic mutations in
one parse/snapshot/save cycle (Perf P1a).

Background
----------

The user's MK-II Controller Board build session logged 178 mutation tool
calls in ~2 hours. Each call pays a fixed cost of ~225 ms (4 git
subprocess invocations for the snapshot + a fresh sexpr parse of the
schematic), independent of what the call actually does. For a workload
that's mostly "place 30 junctions" or "route 66 wires", that overhead
sums to ~40 s of wasted fixed cost over the session — plus the LLM
round-trip on every step.

`sch_compose` collapses N primitive mutations into ONE tool call: parse
once, mutate N times in memory, snapshot once, save once. For a typical
"wire up a regulator block" batch (10-20 wires + 5-10 junctions) the
end-to-end latency drops from ~3-5 s to ~250 ms.

Scope of v1
-----------

Supported step ops:

* `add_wire`        — same primitive as sch_add_wire
* `add_junction`    — same primitive as sch_add_junction
* `add_no_connect`  — same primitive as sch_add_no_connect
* `add_label`       — same primitive as sch_add_label (with kind + shape)
* `add_symbol`      — same primitive as sch_add_symbol

Deferred (file-side-effect-heavy or fallback-synthesis cases): `add_power`
(reads system `power.kicad_sym`, has fallback synthesis path),
`add_sheet` (creates child .kicad_sch on disk). Call those one at a time
through their dedicated tools for now.

Per-step semantics
------------------

* Grid snapping (`safety.grid_snap_mm`) applies to every coordinate. A
  per-step warning is emitted in the step result when a snap actually
  moved the coordinate.
* `continue_on_error=False` (the default) aborts on the first step
  failure WITHOUT saving — the schematic on disk is unchanged regardless
  of how many steps succeeded in memory before the failure.
* `continue_on_error=True` keeps going, applies the surviving steps, and
  reports per-step status in the result. Use when you want best-effort
  application (e.g., "wire up everything possible, tell me what didn't
  fit").
* `dry_run=True` runs validation + grid snap + step dispatch but never
  appends to the doc or saves. Returns the same per-step result shape so
  callers can preview what would be written.

The compose tool intentionally does NOT re-implement the per-tool
proximity / crowding warnings (label-proximity from sch_add_label,
symbol-crowding from sch_add_symbol). Those checks rely on the doc's
state at the moment of the call; batching N steps would make the checks
either inconsistent (state at step 1 vs step N) or expensive (re-walk on
each step). Callers wanting those warnings should use the individual
tools for the readability-sensitive operations.

Status enum
-----------

* `ok`              — all steps succeeded; doc saved.
* `dry_run`         — caller passed dry_run=True; no write.
* `partial`         — `continue_on_error=True` and at least one step
                      failed; the surviving steps were saved.
* `aborted`         — `continue_on_error=False` and a step failed
                      mid-batch; nothing was saved.
* `sch_not_found`   — schematic path invalid before any step ran.
* `invalid_schema`  — parseable but top_head isn't `kicad_sch`.
* `parse_failed`    — sexpr parser rejected the file.
* `empty_batch`     — steps list was empty.
* `write_failed`    — snapshot or atomic save raised.

Backend: SEXPR, required.
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from kimcp._types import Backend, ToolClass
from kimcp.config import Config
from kimcp.safety import SnapshotError, SnapshotPolicy, snapshot, take_snapshot
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.cache import ParseCache
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SList
from kimcp.tools.base import Tool
from kimcp.tools.builtin._sexpr_build import (
    apply_grid_snap,
    find_scalar_string,
    load_sexpr_doc,
)
from kimcp.tools.builtin.sch_add_junction import _build_junction_node
from kimcp.tools.builtin.sch_add_label import _build_label_node
from kimcp.tools.builtin.sch_add_no_connect import _build_no_connect_node
from kimcp.tools.builtin.sch_add_symbol import (
    _build_symbol_instance,
    _derive_project_name,
    _extract_pin_numbers,
    _find_lib_symbol,
)
from kimcp.tools.builtin.sch_add_wire import _build_wire_node

log = logging.getLogger(__name__)


# -- step schemas ----------------------------------------------------------


class _BaseStep(BaseModel):
    """Common base — only the discriminator is shared at this level."""


class AddWireStep(_BaseStep):
    op: Literal["add_wire"]
    start_x: float = Field(..., description="Wire start X in mm.")
    start_y: float = Field(..., description="Wire start Y in mm.")
    end_x: float = Field(..., description="Wire end X in mm.")
    end_y: float = Field(..., description="Wire end Y in mm.")


class AddJunctionStep(_BaseStep):
    op: Literal["add_junction"]
    at_x: float = Field(..., description="Junction X in mm.")
    at_y: float = Field(..., description="Junction Y in mm.")


class AddNoConnectStep(_BaseStep):
    op: Literal["add_no_connect"]
    at_x: float = Field(..., description="No-connect marker X in mm.")
    at_y: float = Field(..., description="No-connect marker Y in mm.")


class AddLabelStep(_BaseStep):
    op: Literal["add_label"]
    text: str = Field(..., description="Net name; must be non-empty.")
    at_x: float = Field(..., description="Anchor X in mm.")
    at_y: float = Field(..., description="Anchor Y in mm.")
    angle: float = Field(default=0.0, description="Rotation angle in degrees.")
    kind: Literal["local", "global", "hierarchical"] = Field(
        default="local",
        description=(
            "Label variety. Prefer local for same-sheet refs; use global "
            "or hierarchical for cross-sheet connectivity. See KICAD-311."
        ),
    )
    shape: Literal["input", "output", "bidirectional", "tri_state", "passive"] = Field(
        default="input",
        description="Shape hint for global/hierarchical labels; ignored for local.",
    )


class AddSymbolStep(_BaseStep):
    op: Literal["add_symbol"]
    lib_id: str = Field(
        ...,
        description=(
            "Library-qualified symbol name (e.g. 'Device:R_Small'). The "
            "lib_symbol MUST already be embedded in the schematic before "
            "the compose call — use sch_embed_lib_symbol first (once per "
            "unique lib_id; the compose call won't auto-embed)."
        ),
    )
    reference: str = Field(..., description="Reference designator (e.g. 'R1').")
    value: str = Field(..., description="Component value (e.g. '10k').")
    at_x: float = Field(..., description="Anchor X in mm.")
    at_y: float = Field(..., description="Anchor Y in mm.")
    angle: float = Field(default=0.0)
    footprint: str = Field(default="")
    unit: int = Field(default=1, ge=1)


ComposeStep = Annotated[
    Union[  # noqa: UP007 — Annotated wraps the union; PEP-604 syntax fights the discriminator
        AddWireStep,
        AddJunctionStep,
        AddNoConnectStep,
        AddLabelStep,
        AddSymbolStep,
    ],
    Field(discriminator="op"),
]


# -- input / output --------------------------------------------------------


class SchComposeInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    steps: list[ComposeStep] = Field(
        ...,
        description=(
            "Ordered list of primitive mutations to apply in a single "
            "parse/snapshot/save cycle. Each step's op discriminator "
            "selects one of: add_wire, add_junction, add_no_connect, "
            "add_label, add_symbol. Steps see each other's effects in "
            "order (e.g., add_wire then add_junction at the wire's "
            "endpoint works as expected)."
        ),
    )
    continue_on_error: bool = Field(
        default=False,
        description=(
            "When True, a failed step is recorded and the batch continues. "
            "When False (the default), the first failure aborts and "
            "NOTHING is written — the schematic on disk is unchanged."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Validate + snap + dispatch every step without saving. Per "
            "ADR-0008."
        ),
    )


class ComposeStepResult(BaseModel):
    index: int = Field(..., description="0-based position in the input steps list.")
    op: str = Field(..., description="Op of the step at this index.")
    status: Literal["ok", "dry_run", "failed"] = Field(...)
    uuid: str | None = Field(
        default=None,
        description="UUID of the newly synthesized node (populated on ok).",
    )
    note: str | None = Field(
        default=None,
        description=(
            "Per-step diagnostic. On failure: the reason. On success: "
            "snap-warning text if the coords moved, else null."
        ),
    )


class SchComposeOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "partial",
        "aborted",
        "sch_not_found",
        "invalid_schema",
        "parse_failed",
        "empty_batch",
        "write_failed",
    ]
    sch_path: str | None = Field(default=None)
    results: list[ComposeStepResult] = Field(
        default_factory=list,
        description=(
            "Per-step outcomes in input order. Length equals the number "
            "of steps actually attempted (= len(steps) on ok/dry_run/"
            "partial; ≤ len(steps) on aborted)."
        ),
    )
    applied: int = Field(
        default=0,
        description="Number of steps that succeeded (excludes failures).",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class SchComposeTool(Tool[SchComposeInput, SchComposeOutput]):
    """Run many sch_add_* primitives in a single parse/snapshot/save cycle."""

    name = "sch_compose"
    version = "0.1.0"
    description = (
        "Batched mutation primitive — run many sch_add_* operations in a "
        "single parse + snapshot + save cycle. "
        "IMPORTANT: When creating a new circuit or subcircuit (e.g. a "
        "DC-DC converter, amplifier stage, filter), ALWAYS present the "
        "full design proposal to the user FIRST — show topology, component "
        "table, connectivity, and design parameters — and wait for explicit "
        "approval before calling this tool. Use the 'circuit-proposal' "
        "prompt for the presentation format. Individual tweaks (moving a "
        "wire, adding a junction) do not need a proposal. "
        "Prefer this over chained individual sch_add_wire/sch_add_junction/"
        "sch_add_label/sch_add_symbol calls whenever you have ≥3 mutations "
        "queued. Supports five step ops via a discriminated union on 'op': "
        "add_wire, add_junction, add_no_connect, add_label, add_symbol. "
        "Steps run in order; later steps see earlier steps' mutations. "
        "For add_symbol, the lib_id must already be embedded via "
        "sch_embed_lib_symbol. Supports dry_run; snapshots before write "
        "per ADR-0008."
    )
    input_model = SchComposeInput
    output_model = SchComposeOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    _parse_cache: ParseCache | None = None

    def __init__(self, config: Config | None = None) -> None:
        self._config = config

    def set_config(self, config: Config) -> None:
        self._config = config

    _snapshot_policy: SnapshotPolicy | None = None

    def set_parse_cache(self, parse_cache: ParseCache) -> None:
        self._parse_cache = parse_cache

    def set_snapshot_policy(self, policy: SnapshotPolicy) -> None:
        self._snapshot_policy = policy

    async def run(self, input: SchComposeInput) -> SchComposeOutput:
        # 1. Empty batch is its own status — cheaper than a no-op snapshot
        # round-trip, and surfaces the smell ("why did you call compose
        # with no steps?") clearly to the caller.
        if not input.steps:
            return SchComposeOutput(
                status="empty_batch",
                sch_path=None,
                note="steps list was empty; compose requires at least one step.",
            )

        # 2. Preflight path.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchComposeOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchComposeOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchComposeOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        # 3. Parse + shape check.
        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchComposeOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchComposeOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # 4. Resolve the top-level UUID once — needed by add_symbol for the
        # instances block. Compute even when no symbol step is present;
        # the cost is a single linear scan and saves a per-step probe.
        top_uuid = find_scalar_string(doc.root, "uuid")
        project_name = _derive_project_name(sch_path)

        grid_snap_mm = (
            self._config.safety.grid_snap_mm if self._config is not None else 2.54
        )

        # 5. Run each step. We append to `doc.root` as we go so later
        # steps observe earlier mutations. UUIDs are allocated only on
        # the write path (not dry_run) so dry_run is idempotent.
        results: list[ComposeStepResult] = []
        applied = 0
        aborted = False

        for index, step in enumerate(input.steps):
            try:
                result, uuid_for_step, node = _dispatch_step(
                    step=step,
                    index=index,
                    doc_root=doc.root,
                    top_uuid=top_uuid,
                    project_name=project_name,
                    grid_snap_mm=grid_snap_mm,
                    dry_run=input.dry_run,
                )
            except _StepError as exc:
                result = ComposeStepResult(
                    index=index,
                    op=step.op,
                    status="failed",
                    note=str(exc),
                )
                results.append(result)
                if not input.continue_on_error:
                    aborted = True
                    break
                continue

            if not input.dry_run and node is not None:
                doc.root.append(node)
                applied += 1

            results.append(result)

        # 6. Outcome dispatch.
        if aborted:
            return SchComposeOutput(
                status="aborted",
                sch_path=str(sch_path),
                results=results,
                applied=0,
                note=(
                    f"aborted at step {results[-1].index} "
                    f"({results[-1].op}): {results[-1].note}. No changes "
                    "were saved. Set continue_on_error=true for "
                    "best-effort batching."
                ),
            )

        if input.dry_run:
            out_dry = SchComposeOutput(
                status="dry_run",
                sch_path=str(sch_path),
                results=results,
                applied=0,
                note=(
                    f"dry_run=True; would apply "
                    f"{sum(1 for r in results if r.status == 'dry_run')} "
                    f"of {len(input.steps)} step(s). Re-run with "
                    "dry_run=False to write."
                ),
            )
            return out_dry

        # 7. Snapshot (single) + save (single).
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_compose:{sch_path.name}:{applied}_steps",
            )
        except SnapshotError as exc:
            return SchComposeOutput(
                status="write_failed",
                sch_path=str(sch_path),
                results=results,
                applied=0,
                note=f"snapshot failed before write: {exc}. No mutation was applied.",
            )

        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchComposeOutput(
                status="write_failed",
                sch_path=str(sch_path),
                results=results,
                applied=0,
                note=f"save failed after snapshot: {exc}.",
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        had_failures = any(r.status == "failed" for r in results)
        final_status: Literal["ok", "partial"] = "partial" if had_failures else "ok"
        out = SchComposeOutput(
            status=final_status,
            sch_path=str(sch_path),
            results=results,
            applied=applied,
            note=(
                f"{applied} of {len(input.steps)} step(s) applied"
                + (
                    f"; {sum(1 for r in results if r.status == 'failed')} "
                    "failed (continue_on_error=true)."
                    if had_failures
                    else "."
                )
            ),
        )
        out.meta.snapshot_ref = snapshot_ref
        return out


# -- per-step dispatcher ---------------------------------------------------


class _StepError(RuntimeError):
    """Raised by a step handler to abort the step with a diagnostic."""


def _dispatch_step(
    *,
    step: ComposeStep,
    index: int,
    doc_root: SList,
    top_uuid: str | None,
    project_name: str,
    grid_snap_mm: float | None,
    dry_run: bool,
) -> tuple[ComposeStepResult, str | None, SList | None]:
    """Apply ``step`` to ``doc_root`` (in memory) and return its result.

    Returns ``(result, uuid_or_none, node_or_none)``. The caller appends
    ``node`` to ``doc_root`` when not in dry-run mode — this layer just
    synthesizes and validates.
    """
    op = step.op

    if isinstance(step, AddWireStep):
        snapped, snap_warning = apply_grid_snap(
            {
                "start_x": step.start_x,
                "start_y": step.start_y,
                "end_x": step.end_x,
                "end_y": step.end_y,
            },
            grid_snap_mm,
        )
        if (
            snapped["start_x"] == snapped["end_x"]
            and snapped["start_y"] == snapped["end_y"]
        ):
            raise _StepError(
                f"zero-length wire after grid snap at "
                f"({snapped['start_x']}, {snapped['start_y']})"
            )
        new_uuid = str(uuid_mod.uuid4()) if not dry_run else None
        node = (
            _build_wire_node(
                start_x=snapped["start_x"],
                start_y=snapped["start_y"],
                end_x=snapped["end_x"],
                end_y=snapped["end_y"],
                wire_uuid=new_uuid or "00000000-0000-0000-0000-000000000000",
            )
            if not dry_run
            else None
        )
        return (
            ComposeStepResult(
                index=index, op=op,
                status="dry_run" if dry_run else "ok",
                uuid=new_uuid,
                note=snap_warning,
            ),
            new_uuid,
            node,
        )

    if isinstance(step, AddJunctionStep):
        snapped, snap_warning = apply_grid_snap(
            {"at_x": step.at_x, "at_y": step.at_y}, grid_snap_mm
        )
        new_uuid = str(uuid_mod.uuid4()) if not dry_run else None
        node = (
            _build_junction_node(
                at_x=snapped["at_x"],
                at_y=snapped["at_y"],
                junction_uuid=new_uuid or "00000000-0000-0000-0000-000000000000",
            )
            if not dry_run
            else None
        )
        return (
            ComposeStepResult(
                index=index, op=op,
                status="dry_run" if dry_run else "ok",
                uuid=new_uuid,
                note=snap_warning,
            ),
            new_uuid,
            node,
        )

    if isinstance(step, AddNoConnectStep):
        snapped, snap_warning = apply_grid_snap(
            {"at_x": step.at_x, "at_y": step.at_y}, grid_snap_mm
        )
        new_uuid = str(uuid_mod.uuid4()) if not dry_run else None
        node = (
            _build_no_connect_node(
                at_x=snapped["at_x"],
                at_y=snapped["at_y"],
                nc_uuid=new_uuid or "00000000-0000-0000-0000-000000000000",
            )
            if not dry_run
            else None
        )
        return (
            ComposeStepResult(
                index=index, op=op,
                status="dry_run" if dry_run else "ok",
                uuid=new_uuid,
                note=snap_warning,
            ),
            new_uuid,
            node,
        )

    if isinstance(step, AddLabelStep):
        if not step.text:
            raise _StepError("label text must be non-empty")
        snapped, snap_warning = apply_grid_snap(
            {"at_x": step.at_x, "at_y": step.at_y}, grid_snap_mm
        )
        new_uuid = str(uuid_mod.uuid4()) if not dry_run else None
        node = (
            _build_label_node(
                kind=step.kind,
                text=step.text,
                at_x=snapped["at_x"],
                at_y=snapped["at_y"],
                angle=step.angle,
                shape=step.shape,
                label_uuid=new_uuid or "00000000-0000-0000-0000-000000000000",
            )
            if not dry_run
            else None
        )
        return (
            ComposeStepResult(
                index=index, op=op,
                status="dry_run" if dry_run else "ok",
                uuid=new_uuid,
                note=snap_warning,
            ),
            new_uuid,
            node,
        )

    if isinstance(step, AddSymbolStep):
        if top_uuid is None:
            raise _StepError(
                "schematic has no (uuid \"...\") at root; can't build "
                "the instances block. Open the file in KiCAD once."
            )
        lib_symbols = doc_root.find("lib_symbols")
        lib_symbol = (
            _find_lib_symbol(lib_symbols, step.lib_id) if lib_symbols else None
        )
        if lib_symbol is None:
            raise _StepError(
                f"lib_id {step.lib_id!r} is not embedded in lib_symbols. "
                "Run sch_embed_lib_symbol first."
            )
        pin_numbers = _extract_pin_numbers(lib_symbol)
        snapped, snap_warning = apply_grid_snap(
            {"at_x": step.at_x, "at_y": step.at_y}, grid_snap_mm
        )
        if dry_run:
            return (
                ComposeStepResult(
                    index=index, op=op,
                    status="dry_run",
                    uuid=None,
                    note=snap_warning,
                ),
                None,
                None,
            )
        instance_uuid = str(uuid_mod.uuid4())
        pin_uuids = {num: str(uuid_mod.uuid4()) for num in pin_numbers}
        node = _build_symbol_instance(
            lib_id=step.lib_id,
            reference=step.reference,
            value=step.value,
            at_x=snapped["at_x"],
            at_y=snapped["at_y"],
            angle=step.angle,
            footprint=step.footprint,
            unit=step.unit,
            instance_uuid=instance_uuid,
            pin_uuids=pin_uuids,
            project_name=project_name,
            top_uuid=top_uuid,
        )
        return (
            ComposeStepResult(
                index=index, op=op,
                status="ok",
                uuid=instance_uuid,
                note=snap_warning,
            ),
            instance_uuid,
            node,
        )

    # Should be unreachable thanks to the discriminator, but stay defensive.
    raise _StepError(f"unsupported step op: {op!r}")


__all__ = [
    "AddJunctionStep",
    "AddLabelStep",
    "AddNoConnectStep",
    "AddSymbolStep",
    "AddWireStep",
    "ComposeStep",
    "ComposeStepResult",
    "SchComposeInput",
    "SchComposeOutput",
    "SchComposeTool",
]
