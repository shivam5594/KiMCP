"""sch_delete — remove a schematic element by UUID (M20).

The inverse of every M14-M18 placement tool. Given a UUID, find the
matching top-level child in the schematic root and remove it.

Deletable node types (all live at the root level of a ``kicad_sch``):

* ``symbol``  — component instances placed by sch_add_symbol / sch_add_power.
* ``wire``  — segments placed by sch_add_wire.
* ``junction``  — junctions placed by sch_add_junction.
* ``label`` / ``global_label`` / ``hierarchical_label``  — net labels
  placed by sch_add_label.
* ``no_connect``  — no-connect flags placed by sch_add_no_connect.

The tool targets the immediate **root-level** children of the schematic.
Nested UUIDs (property UUIDs, pin UUIDs inside a symbol instance) are
not addressable here — you'd delete the owning parent instead.

Scope of the first ship
-----------------------

* **Single UUID per call.** Bulk delete is a wrapper concern.
* **No cascade.** Deleting a symbol does NOT auto-remove wires that
  touched its pins. The caller is responsible for cleaning up dangling
  connectivity. KiCAD tolerates orphan wires — they just produce ERC
  warnings.
* **No re-annotation.** If you delete ``R1``, there's no automatic
  renumber. Annotation is the caller's or KiCAD-GUI's job.

Status enum
-----------

* **ok**              — node removed and written.
* **dry_run**         — caller passed ``dry_run=True``.
* **sch_not_found**   — path missing / not a file / wrong suffix.
* **invalid_schema**  — top_head isn't ``kicad_sch``.
* **parse_failed**    — the SEXPR parser rejected the file bytes.
* **uuid_not_found**  — no root-level child has that UUID.
* **write_failed**    — snapshot or atomic save raised.

Backend: SEXPR, required. Same rationale as M14-M19.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from kimcp._types import Backend, ToolClass
from kimcp.config import Config
from kimcp.safety import SnapshotError, SnapshotPolicy, snapshot, take_snapshot
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.cache import ParseCache
from kimcp.tools.builtin._sexpr_build import load_sexpr_doc
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.base import Tool

log = logging.getLogger(__name__)

# Root-level heads that are valid deletion targets.
_DELETABLE_HEADS: frozenset[str] = frozenset(
    {
        "symbol",
        "wire",
        "junction",
        "label",
        "global_label",
        "hierarchical_label",
        "no_connect",
    }
)


# -- input / output --------------------------------------------------------


class SchDeleteInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    uuid: str = Field(
        ...,
        description=(
            "UUID of the element to delete. Must match the (uuid \"...\") "
            "payload of a root-level child (symbol, wire, junction, label, "
            "no_connect). Nested UUIDs (pins, properties) are not "
            "addressable — delete the owning parent instead."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the element that would be deleted without writing.",
    )


class SchDeleteOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "invalid_schema",
        "parse_failed",
        "uuid_not_found",
        "write_failed",
    ]
    sch_path: str | None = Field(
        default=None, description="Resolved absolute path to the .kicad_sch."
    )
    deleted_head: str | None = Field(
        default=None,
        description=(
            "Head keyword of the deleted node (e.g. 'symbol', 'wire', "
            "'junction', 'label'). Populated on status=ok and dry_run."
        ),
    )
    deleted_uuid: str | None = Field(
        default=None, description="Echo of the deleted UUID."
    )
    note: str | None = Field(
        default=None, description="Diagnostic string for non-ok statuses."
    )


# -- tool ------------------------------------------------------------------


class SchDeleteTool(Tool[SchDeleteInput, SchDeleteOutput]):
    """Remove a schematic element by UUID from a .kicad_sch."""

    name = "sch_delete"
    version = "0.1.0"
    description = (
        "Delete a root-level schematic element (symbol, wire, junction, "
        "label, no_connect) identified by its UUID. No cascading — dangling "
        "wires or orphan junctions remain for the caller to clean up. "
        "Supports dry_run; snapshots before write per ADR-0008."
    )
    input_model = SchDeleteInput
    output_model = SchDeleteOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    def __init__(self, config: Config | None = None) -> None:
        self._config = config

    _parse_cache: ParseCache | None = None

    def set_config(self, config: Config) -> None:
        self._config = config

    _snapshot_policy: SnapshotPolicy | None = None

    def set_parse_cache(self, parse_cache: ParseCache) -> None:
        self._parse_cache = parse_cache

    def set_snapshot_policy(self, policy: SnapshotPolicy) -> None:
        self._snapshot_policy = policy

    async def run(self, input: SchDeleteInput) -> SchDeleteOutput:
        # 1. Preflight path.
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists():
            return SchDeleteOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if not sch_path.is_file():
            return SchDeleteOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=f"not a regular file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchDeleteOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        # 2. Parse.
        try:
            doc = load_sexpr_doc(self._parse_cache, sch_path)
        except SexprParseError as exc:
            return SchDeleteOutput(
                status="parse_failed",
                sch_path=str(sch_path),
                note=f"SEXPR parse failed: {exc}",
            )

        if doc.top_head != "kicad_sch":
            return SchDeleteOutput(
                status="invalid_schema",
                sch_path=str(sch_path),
                note=(
                    f"expected top-level '(kicad_sch ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # 3. Find the target by UUID among root-level children.
        match = _find_by_uuid(doc.root, input.uuid)
        if match is None:
            return SchDeleteOutput(
                status="uuid_not_found",
                sch_path=str(sch_path),
                deleted_uuid=input.uuid,
                note=(
                    f"no root-level element with UUID {input.uuid!r} found. "
                    "Ensure the UUID belongs to a top-level node (symbol, "
                    "wire, junction, label, no_connect) — nested UUIDs "
                    "(pins, properties) are not addressable by sch_delete."
                ),
            )
        target_idx, target_node = match

        # 4. Dry-run.
        if input.dry_run:
            return SchDeleteOutput(
                status="dry_run",
                sch_path=str(sch_path),
                deleted_head=target_node.head,
                deleted_uuid=input.uuid,
                note=(
                    f"dry_run=True; would delete {target_node.head!r} "
                    f"(UUID {input.uuid!r}). Re-run with dry_run=False "
                    "to apply."
                ),
            )

        # 5. Remove.
        deleted_head = target_node.head
        doc.root.remove_at(target_idx)

        # 6. Snapshot before write.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode

        snapshot_ref: str | None = None
        try:
            snapshot_ref = take_snapshot(self._snapshot_policy, sch_path.parent,
                mode=snapshot_mode,
                reason=f"sch_delete:{sch_path.name}:{input.uuid}",
            )
        except SnapshotError as exc:
            return SchDeleteOutput(
                status="write_failed",
                sch_path=str(sch_path),
                deleted_uuid=input.uuid,
                note=f"snapshot failed before write: {exc}.",
            )

        # 7. Save.
        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = SchDeleteOutput(
                status="write_failed",
                sch_path=str(sch_path),
                deleted_uuid=input.uuid,
                note=f"save failed after snapshot: {exc}.",
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        out = SchDeleteOutput(
            status="ok",
            sch_path=str(sch_path),
            deleted_head=deleted_head,
            deleted_uuid=input.uuid,
        )
        out.meta.snapshot_ref = snapshot_ref
        return out


# -- helpers ---------------------------------------------------------------


def _find_by_uuid(
    root: SList, target_uuid: str
) -> tuple[int, SList] | None:
    """Return ``(index, node)`` for the first root-level child matching.

    Only searches children whose head is in ``_DELETABLE_HEADS`` —
    structural nodes like ``(lib_symbols ...)`` and ``(version ...)``
    are not deletable via this tool.
    """
    for idx, child in enumerate(root.items):
        if not isinstance(child, SList):
            continue
        if child.head not in _DELETABLE_HEADS:
            continue
        uuid_node = child.find("uuid")
        if uuid_node is None or len(uuid_node.items) < 2:
            continue
        payload = uuid_node.items[1]
        if isinstance(payload, SAtom) and payload.text == target_uuid:
            return (idx, child)
    return None


__all__ = [
    "SchDeleteInput",
    "SchDeleteOutput",
    "SchDeleteTool",
    "_find_by_uuid",
]
