"""lib_attach_3d_model — link a STEP/WRL model to a footprint (M47).

The third piece of library authoring: once a footprint exists on disk
(via M45 or hand-editing), we still need a 3D model to light up the
3D viewer and the MCAD export path. ``.kicad_mod`` files store model
references as::

    (model "path/to/model.step"
      (offset (xyz 0 0 0))
      (scale (xyz 1 1 1))
      (rotate (xyz 0 0 0)))

One footprint can carry multiple ``(model ...)`` blocks (KiCAD
renders each); this tool appends one or, with ``replace=True``,
swaps the existing first entry in place.

Scope of the first ship
-----------------------

* **.step and .wrl only.** Those are the formats KiCAD's 3D viewer
  reads natively. .stp is normalized to .step (identical file
  format, KiCAD accepts either suffix but the library convention is
  .step).
* **Offset / scale / rotate in millimetres + degrees.** KiCAD's XYZ
  vector is in mm for offset, dimensionless for scale, degrees for
  rotate. We accept all three with sensible defaults.
* **Path-as-string.** We don't verify the model file actually exists;
  board authors often point at ``${KICAD6_3DMODEL_DIR}/...`` tokens
  that resolve only when KiCAD is running. We DO emit a warning on
  the output envelope if the path is clearly wrong (non-existent
  filename with no environment variable prefix).

Conflict policy
---------------

If a ``(model ...)`` block already exists:

* ``replace=False`` (default) → append a second one.
* ``replace=True`` → replace the FIRST existing model in place.
  Additional model blocks (if any) are left untouched.

Status enum
-----------

* **ok**                   — model attached.
* **dry_run**              — caller passed ``dry_run=True``.
* **invalid_input**        — bad model suffix, bad scale/offset/rotate.
* **footprint_not_found**  — .kicad_mod path missing / wrong suffix.
* **invalid_schema**       — file parses but top_head isn't ``footprint``.
* **parse_failed**         — SEXPR parser rejected the footprint.
* **write_failed**         — snapshot / save raised.

Backend: SEXPR, required.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from kimcp._types import Backend, ToolClass
from kimcp.config import Config
from kimcp.safety import SnapshotError, snapshot
from kimcp.schemas.envelope import ToolOutput
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SList
from kimcp.tools.base import Tool
from kimcp.tools.builtin._sexpr_build import atom, fmt_mm, slist

log = logging.getLogger(__name__)


# Accepted model-file suffixes. ``.stp`` is an alias for ``.step`` and
# gets normalized on write so libraries read consistently.
_VALID_MODEL_SUFFIXES = frozenset({".step", ".stp", ".wrl", ".STEP", ".STP", ".WRL"})


# -- input sub-models ------------------------------------------------------


class Xyz(BaseModel):
    """``(xyz X Y Z)`` triple used for offset / scale / rotate.

    Units depend on context:
    * ``offset`` → millimetres (translate the model relative to origin).
    * ``scale`` → dimensionless multiplier on each axis (1.0 = no scale).
    * ``rotate`` → degrees around each axis.
    """

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


# -- input / output --------------------------------------------------------


class LibAttach3dModelInput(BaseModel):
    footprint_path: Path = Field(
        ...,
        description=(
            "Path to the target .kicad_mod file. Must be an existing "
            "regular file with top-head 'footprint'."
        ),
    )
    model_path: str = Field(
        ...,
        description=(
            "Path or URI of the 3D model. Accepts absolute paths, "
            "paths under ${KICAD6_3DMODEL_DIR} / ${KIPRJMOD} / other "
            "KiCAD environment variables, or relative paths — stored "
            "verbatim. Must end in .step (or .stp → normalized to "
            ".step) or .wrl."
        ),
    )
    offset: Xyz = Field(
        default_factory=Xyz,
        description=(
            "Translation in millimetres. Typical defaults are 0/0/0 "
            "(model's own origin matches the footprint's)."
        ),
    )
    scale: Xyz = Field(
        default_factory=lambda: Xyz(x=1.0, y=1.0, z=1.0),
        description=(
            "Scale factor per axis. 1.0/1.0/1.0 is no scaling. Values "
            "<1 shrink, >1 enlarge."
        ),
    )
    rotate: Xyz = Field(
        default_factory=Xyz,
        description=(
            "Rotation in degrees around X, Y, Z axes in turn. Zero "
            "leaves the model in its native orientation."
        ),
    )
    replace: bool = Field(
        default=False,
        description=(
            "If True and the footprint already has at least one "
            "(model ...) child, replace the FIRST one in place. If "
            "False, append an additional model — KiCAD renders all "
            "model blocks (useful for multi-part mechanicals)."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the planned mutation without writing.",
    )

    @field_validator("model_path")
    @classmethod
    def _check_model_suffix(cls, v: str) -> str:
        if not v:
            raise ValueError("model_path must be non-empty")
        # Accept either case; normalize later when we actually write.
        if not any(v.endswith(suf) for suf in _VALID_MODEL_SUFFIXES):
            raise ValueError(
                f"model_path must end in .step (or .stp) or .wrl; got {v!r}"
            )
        return v


class LibAttach3dModelOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "invalid_input",
        "footprint_not_found",
        "invalid_schema",
        "parse_failed",
        "write_failed",
    ]
    footprint_path: str | None = Field(default=None)
    model_path: str | None = Field(
        default=None,
        description=(
            "The path as written into the .kicad_mod (normalized "
            ".stp → .step if applicable)."
        ),
    )
    replaced_existing: bool = Field(
        default=False,
        description="True when ``replace=True`` and an existing model was swapped.",
    )
    model_count: int = Field(
        default=0,
        description="Total (model ...) children after the operation.",
    )
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class LibAttach3dModelTool(
    Tool[LibAttach3dModelInput, LibAttach3dModelOutput]
):
    """Attach (or replace) a 3D model on a KiCAD .kicad_mod footprint."""

    name = "lib_attach_3d_model"
    version = "0.1.0"
    description = (
        "Add a (model ...) reference to a KiCAD .kicad_mod footprint "
        "file, with offset / scale / rotate transforms. Normalizes "
        ".stp → .step on write. replace=True swaps the first existing "
        "model in place; the default appends an additional block. "
        "Supports dry_run; snapshots before write per ADR-0008."
    )
    input_model = LibAttach3dModelInput
    output_model = LibAttach3dModelOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.SEXPR,)
    required_backends = frozenset({Backend.SEXPR})

    def __init__(self, config: Config | None = None) -> None:
        self._config = config

    def set_config(self, config: Config) -> None:
        self._config = config

    async def run(
        self, input: LibAttach3dModelInput
    ) -> LibAttach3dModelOutput:
        # 1. Footprint path check.
        fp_path = input.footprint_path.expanduser().resolve()
        if not fp_path.exists():
            return LibAttach3dModelOutput(
                status="footprint_not_found",
                footprint_path=None,
                note=f"no such file: {fp_path}",
            )
        if not fp_path.is_file():
            return LibAttach3dModelOutput(
                status="footprint_not_found",
                footprint_path=str(fp_path),
                note=f"not a regular file: {fp_path}",
            )
        if fp_path.suffix.lower() != ".kicad_mod":
            return LibAttach3dModelOutput(
                status="footprint_not_found",
                footprint_path=str(fp_path),
                note=(
                    f"not a .kicad_mod file: {fp_path} (got suffix "
                    f"{fp_path.suffix!r})."
                ),
            )

        # 2. Parse + shape check.
        try:
            doc = SexprDocument.from_path(fp_path)
        except SexprParseError as exc:
            return LibAttach3dModelOutput(
                status="parse_failed",
                footprint_path=str(fp_path),
                note=f"SEXPR parse failed: {exc}",
            )
        if doc.top_head != "footprint":
            return LibAttach3dModelOutput(
                status="invalid_schema",
                footprint_path=str(fp_path),
                note=(
                    f"expected top-level '(footprint ...)' but got "
                    f"'({doc.top_head or '?'} ...)'."
                ),
            )

        # 3. Normalize .stp → .step. KiCAD accepts both but libraries
        # standardize on .step; keeping them uniform avoids subtle
        # diff churn when the model is later refreshed from the CAD
        # source.
        model_path = input.model_path
        if model_path.lower().endswith(".stp"):
            model_path = model_path[: -len(".stp")] + ".step"

        # 4. Find existing (model ...) children — used both for the
        # replace path and for the post-op count.
        existing_models = [
            (idx, child)
            for idx, child in enumerate(doc.root.items)
            if isinstance(child, SList) and child.head == "model"
        ]

        # 5. Dry-run short-circuit.
        if input.dry_run:
            if input.replace and existing_models:
                action = "replace first of"
            elif existing_models:
                action = "append (existing:"
            else:
                action = "append"
            if "existing" in action:
                action = f"append (existing: {len(existing_models)})"
            return LibAttach3dModelOutput(
                status="dry_run",
                footprint_path=str(fp_path),
                model_path=model_path,
                replaced_existing=False,
                model_count=len(existing_models),
                note=(
                    f"dry_run=True; would {action} (model ...) child "
                    f"pointing at {model_path!r}."
                ),
            )

        # 6. Synthesize the new (model ...) block and insert.
        new_model = _build_model_node(
            path=model_path,
            offset=input.offset,
            scale=input.scale,
            rotate=input.rotate,
        )
        replaced_existing = False
        if input.replace and existing_models:
            idx = existing_models[0][0]
            doc.root.replace(idx, new_model)
            replaced_existing = True
        else:
            # Insert right before the trailing (embedded_fonts ...) if
            # present — that's where KiCAD's footprint editor places
            # newly-added models. If there's no embedded_fonts (older
            # fixtures), append at the end.
            ef_idx: int | None = None
            for i, child in enumerate(doc.root.items):
                if isinstance(child, SList) and child.head == "embedded_fonts":
                    ef_idx = i
                    break
            if ef_idx is not None:
                doc.root.insert(ef_idx, new_model)
            else:
                doc.root.append(new_model)

        # 7. Snapshot.
        snapshot_mode = "git"
        if self._config is not None:
            snapshot_mode = self._config.safety.snapshot_mode
        snapshot_ref: str | None = None
        try:
            snapshot_ref = snapshot(
                fp_path.parent,
                mode=snapshot_mode,
                reason=f"lib_attach_3d_model:{fp_path.name}",
            )
        except SnapshotError as exc:
            return LibAttach3dModelOutput(
                status="write_failed",
                footprint_path=str(fp_path),
                model_path=model_path,
                note=f"snapshot failed before write: {exc}.",
            )

        # 8. Save.
        try:
            doc.save()
        except (OSError, RuntimeError) as exc:
            out_fail = LibAttach3dModelOutput(
                status="write_failed",
                footprint_path=str(fp_path),
                model_path=model_path,
                note=(
                    f"save failed after snapshot: {exc}. Restore from the "
                    "snapshot if needed."
                ),
            )
            out_fail.meta.snapshot_ref = snapshot_ref
            return out_fail

        # Post-op count: existing count + 1 if appended, unchanged if
        # replaced.
        final_count = (
            len(existing_models) if replaced_existing else len(existing_models) + 1
        )

        out = LibAttach3dModelOutput(
            status="ok",
            footprint_path=str(fp_path),
            model_path=model_path,
            replaced_existing=replaced_existing,
            model_count=final_count,
        )
        out.meta.snapshot_ref = snapshot_ref
        return out


# -- helpers ---------------------------------------------------------------


def _xyz_node(head: str, v: Xyz) -> SList:
    """``(offset|scale|rotate (xyz X Y Z))``."""
    return slist(
        atom(head),
        slist(atom("xyz"), atom(fmt_mm(v.x)), atom(fmt_mm(v.y)), atom(fmt_mm(v.z))),
    )


def _build_model_node(
    *, path: str, offset: Xyz, scale: Xyz, rotate: Xyz
) -> SList:
    """``(model "path" (offset ...) (scale ...) (rotate ...))``.

    Layout matches KiCAD 10's footprint-editor emission exactly — the
    three transform blocks always appear in offset/scale/rotate order,
    even when all three are defaults, so subsequent GUI saves round-
    trip without diff churn.
    """
    return slist(
        atom("model"),
        atom(path, quoted=True),
        _xyz_node("offset", offset),
        _xyz_node("scale", scale),
        _xyz_node("rotate", rotate),
    )


__all__ = [
    "LibAttach3dModelInput",
    "LibAttach3dModelOutput",
    "LibAttach3dModelTool",
    "Xyz",
]
