"""pcb_export_step — render a .kicad_pcb to a 3D STEP model (M22).

Where :mod:`pcb_export_gerbers` and :mod:`pcb_export_drill` emit the 2D
layer stack a fabricator needs, this tool shells out to
``kicad-cli pcb export step`` to produce the 3D STEP model mechanical
CAD tooling (Fusion 360, Onshape, SolidWorks, FreeCAD) consumes when
designing an enclosure or checking clearances. STEP is the lingua
franca of mechanical CAD — any MCAD program that doesn't read it isn't
one you'd trust with an enclosure.

Unlike the gerber/drill exports (which emit a *directory* of files),
STEP export writes a single output file. The envelope therefore
reports ``output_path`` + ``size_bytes`` rather than a ``generated_files``
list — one artifact, one report.

Status enum:

* **ok**              — kicad-cli ran cleanly and the STEP file exists
                        on disk with nonzero size.
* **dry_run**         — caller passed ``dry_run=True``; we report the
                        planned argv + resolved output_path without
                        invoking the CLI. Per ADR-0008, every mutating
                        tool supports dry-run.
* **pcb_not_found**   — input path is missing or not a .kicad_pcb.
* **cli_failed**      — kicad-cli didn't run cleanly (timeout, non-zero
                        exit, binary disappeared between probe and call,
                        or origin flags conflicted). ``note`` carries
                        the reason.
* **output_missing**  — kicad-cli exited 0 but the expected output file
                        isn't on disk (or is zero-bytes). Defensive —
                        usually means the board had no 3D content to
                        export, or a racing rm-rf between exit and stat.

Origin flag discipline: KiCAD's CLI accepts ``--grid-origin``,
``--drill-origin``, and ``--user-origin X,Y`` but rejects combinations
at the kicad-cli layer. We pre-validate the mutually-exclusive trio and
return ``cli_failed`` with an explanatory note BEFORE invoking the CLI
— the caller sees "you asked for two conflicting origins" rather than
a generic non-zero exit.

Why MUTATE and not READ: writes to the filesystem. See the
``pcb_export_drill`` / ``pcb_export_gerbers`` modules for the full
rationale — same contract applies here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from kimcp._types import Backend, ToolClass
from kimcp.backends.cli import CliBackend
from kimcp.cli.errors import CliError, CliTimeoutError
from kimcp.cli.runner import run_cli
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool

log = logging.getLogger(__name__)

# STEP export is the slowest of the kicad-cli export family — it
# tessellates every 3D model on the board, substitutes VRML-only models
# if asked, and optimizes the resulting STEP mesh. A dense board with
# 200+ components carrying detailed 3D models finishes in ~30-60 s on
# commodity hardware; 600 s gives headroom for genuinely large panels
# and a slow filesystem. Same ``timeout_sec`` knob rationale as the
# sibling exports — add one when a real-world board forces the issue.
_STEP_TIMEOUT_SEC = 600.0


# -- input / output --------------------------------------------------------


class PcbExportStepInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    output_path: Path | None = Field(
        default=None,
        description=(
            "Destination path for the STEP file. Defaults to the PCB's "
            "stem with a .step suffix, next to the input (e.g. "
            "board.kicad_pcb → board.step). Parent directory is created "
            "if missing."
        ),
    )
    grid_origin: bool = Field(
        default=False,
        description=(
            "Use the page grid origin as the STEP model origin. Mutually "
            "exclusive with drill_origin and user_origin — picking two "
            "returns cli_failed with an explanatory note."
        ),
    )
    drill_origin: bool = Field(
        default=False,
        description=(
            "Use the PCB's auxiliary/drill origin as the STEP model "
            "origin. Mutually exclusive with grid_origin and user_origin."
        ),
    )
    user_origin: str | None = Field(
        default=None,
        description=(
            "User-defined origin as 'X,Y' in millimetres (e.g. '50,50'). "
            "Mutually exclusive with grid_origin and drill_origin. Pass "
            "None to use the board origin, which is kicad-cli's default."
        ),
    )
    no_unspecified: bool = Field(
        default=False,
        description=(
            "Skip 3D models for footprints marked with 'Unspecified' "
            "attribute. Typical MCAD pre-flight: exclude prototypes, "
            "jumpers, mounting-hole-only footprints. Off by default — "
            "kicad-cli's default behavior is to include everything."
        ),
    )
    no_dnp: bool = Field(
        default=False,
        description=(
            "Skip 3D models for 'Do Not Populate' components. Matches "
            "what the fab skips, so your MCAD assembly matches the "
            "as-built PCB. Off by default."
        ),
    )
    subst_models: bool = Field(
        default=False,
        description=(
            "When a footprint has no STEP model, substitute a VRML model "
            "converted on the fly. Useful for legacy libraries that only "
            "ship VRML; off by default since the conversion can bloat the "
            "output file and some MCAD tools choke on the result."
        ),
    )
    board_only: bool = Field(
        default=False,
        description=(
            "Emit only the PCB substrate — no component 3D models. "
            "Useful for enclosure design where you only need the board "
            "outline + keepouts, not the full assembly."
        ),
    )
    include_tracks: bool = Field(
        default=False,
        description=(
            "Include copper tracks as extruded geometry. Off by default "
            "since tracks add geometry weight without helping mechanical "
            "clearance checks; on for visual renders that want copper."
        ),
    )
    include_zones: bool = Field(
        default=False,
        description=(
            "Include copper zones (fills) as extruded geometry. Same "
            "trade-off as include_tracks — off by default for speed, "
            "on for visual fidelity."
        ),
    )
    min_distance_mm: float = Field(
        default=0.01,
        gt=0.0,
        description=(
            "Minimum distance in mm between points in the STEP mesh. "
            "Smaller values → finer tessellation and bigger files; "
            "larger → coarser mesh. kicad-cli's default is 0.01 mm."
        ),
    )
    no_optimize_step: bool = Field(
        default=False,
        description=(
            "Skip the STEP post-optimization pass. Speeds up export at "
            "the cost of a larger output file. Off by default — the "
            "optimization is usually worth the time."
        ),
    )
    force: bool = Field(
        default=True,
        description=(
            "Overwrite the output file if it already exists. Defaults to "
            "True — we own the output_path, and the whole point of a "
            "rebuild is to replace the previous STEP. Set False to "
            "preserve an existing file (kicad-cli will then fail; the "
            "tool surfaces that as cli_failed)."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If True, validate inputs and report the planned argv + "
            "resolved output_path without invoking kicad-cli. Per "
            "ADR-0008, every mutating tool supports dry-run."
        ),
    )


class PcbExportStepOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "pcb_not_found",
        "cli_failed",
        "output_missing",
    ]
    pcb_path: str | None = Field(
        default=None,
        description="Resolved absolute path to the .kicad_pcb.",
    )
    output_path: str | None = Field(
        default=None,
        description="Resolved absolute path to the STEP file (whether created or planned).",
    )
    size_bytes: int | None = Field(
        default=None,
        description=(
            "On-disk size of the produced STEP file. Null for dry_run "
            "and any failure status. Useful for sanity-checking that "
            "the file wasn't silently empty."
        ),
    )
    cli_argv: list[str] | None = Field(
        default=None,
        description=(
            "Argv kicad-cli was (or would be) invoked with, EXCLUDING the "
            "binary path. Populated for both ok and dry_run."
        ),
    )
    note: str | None = Field(
        default=None,
        description="Diagnostic string for non-ok statuses.",
    )


# -- tool ------------------------------------------------------------------


class PcbExportStepTool(Tool[PcbExportStepInput, PcbExportStepOutput]):
    """Render a .kicad_pcb to a 3D STEP model via `kicad-cli pcb export step`."""

    name = "pcb_export_step"
    version = "0.1.0"
    description = (
        "Render a .kicad_pcb to a 3D STEP model using "
        "`kicad-cli pcb export step`. Returns the output path and "
        "on-disk size. Supports origin selection (grid/drill/user), "
        "DNP + unspecified filtering, VRML model substitution, tracks "
        "and zones inclusion, and dry_run for safe preview."
    )
    input_model = PcbExportStepInput
    output_model = PcbExportStepOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: PcbExportStepInput) -> PcbExportStepOutput:
        # 1. Resolve the PCB path + pre-flight. Same shape as
        # pcb_export_drill — missing file / wrong suffix short-circuits
        # before we pay for a subprocess.
        pcb_path = input.pcb_path.expanduser().resolve()
        if not pcb_path.exists():
            return PcbExportStepOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbExportStepOutput(
                status="pcb_not_found",
                pcb_path=str(pcb_path),
                note=(
                    f"not a .kicad_pcb file: {pcb_path} (got suffix "
                    f"{pcb_path.suffix!r}). pcb_export_step runs on a "
                    "board file, not a project or schematic."
                ),
            )

        # 2. Resolve the output path. Default lands next to the PCB with
        # the same stem + .step suffix — the convention every KiCAD-using
        # MCAD shop expects.
        if input.output_path is None:
            output_path = pcb_path.with_suffix(".step")
        else:
            output_path = input.output_path.expanduser().resolve()

        # 3. Origin flags are mutually exclusive. kicad-cli itself would
        # catch this, but the generic non-zero exit is hard to act on.
        # Fail loudly here with a specific note so the caller knows which
        # flag combination to fix.
        origin_flags = [
            ("grid_origin", input.grid_origin),
            ("drill_origin", input.drill_origin),
            ("user_origin", input.user_origin is not None),
        ]
        chosen = [name for name, on in origin_flags if on]
        if len(chosen) > 1:
            return PcbExportStepOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                note=(
                    f"origin flags are mutually exclusive; you picked: "
                    f"{', '.join(chosen)}. Choose at most one of "
                    "grid_origin, drill_origin, user_origin."
                ),
            )

        # 4. Build argv before probing so dry_run can return it even
        # when kicad-cli isn't installed on the host.
        argv: list[str] = [
            "pcb",
            "export",
            "step",
            "--output",
            str(output_path),
            "--min-distance",
            f"{input.min_distance_mm}mm",
        ]
        if input.grid_origin:
            argv.append("--grid-origin")
        if input.drill_origin:
            argv.append("--drill-origin")
        if input.user_origin is not None:
            argv.extend(["--user-origin", input.user_origin])
        if input.no_unspecified:
            argv.append("--no-unspecified")
        if input.no_dnp:
            argv.append("--no-dnp")
        if input.subst_models:
            argv.append("--subst-models")
        if input.board_only:
            argv.append("--board-only")
        if input.include_tracks:
            argv.append("--include-tracks")
        if input.include_zones:
            argv.append("--include-zones")
        if input.no_optimize_step:
            argv.append("--no-optimize-step")
        if input.force:
            argv.append("--force")
        argv.append(str(pcb_path))

        # 5. Dry-run short-circuit — no mkdir, no subprocess.
        if input.dry_run:
            return PcbExportStepOutput(
                status="dry_run",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    "dry_run=True; no files were written. Re-run with "
                    "dry_run=False to actually invoke kicad-cli."
                ),
            )

        # 6. Resolve CLI. Same safety belt as pcb_export_drill — the
        # dispatcher already gated, but we need the resolved path and
        # cover the rare race between probe and call.
        backend = self._cli_backend
        if backend is None:
            backend = CliBackend()
        await backend.probe()
        cli_path = backend.cli_path
        if cli_path is None:
            return PcbExportStepOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        # 7. Ensure the output's parent directory exists. kicad-cli won't
        # create nested output dirs; doing it here matches the drill/gerber
        # exports and keeps the error surface consistent.
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return PcbExportStepOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"could not create output directory: {exc}",
            )

        # 8. Invoke kicad-cli.
        try:
            result = await run_cli(
                tuple(argv),
                cli_path=Path(cli_path),
                timeout=_STEP_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return PcbExportStepOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    f"kicad-cli timed out after {exc.timeout:.0f}s — "
                    "the board may have unusually heavy 3D models; try "
                    "board_only=True or no_optimize_step=True to trim."
                ),
            )
        except CliError as exc:
            return PcbExportStepOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return PcbExportStepOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    f"kicad-cli exited {result.exit_code}: "
                    f"{stderr_excerpt or '<no stderr output>'}"
                ),
            )

        # 9. Verify the file actually landed. kicad-cli occasionally
        # exits 0 with no output (rare, but documented in the bug
        # tracker — typically when --board-only meets an outline-only
        # board). Catch that defensively.
        if not output_path.exists():
            return PcbExportStepOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    "kicad-cli exited 0 but the expected output file is "
                    "not on disk. The board may have had no 3D content "
                    "to export (check that footprints have 3D models "
                    "assigned and aren't all DNP/Unspecified)."
                ),
            )
        try:
            size = output_path.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            return PcbExportStepOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    "output file exists but is zero bytes — "
                    "kicad-cli reported success but produced no data. "
                    "Inspect the board for 3D model coverage."
                ),
            )

        return PcbExportStepOutput(
            status="ok",
            pcb_path=str(pcb_path),
            output_path=str(output_path),
            size_bytes=size,
            cli_argv=argv,
        )


__all__ = [
    "PcbExportStepInput",
    "PcbExportStepOutput",
    "PcbExportStepTool",
]
