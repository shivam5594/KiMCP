"""pcb_export_pos — render a .kicad_pcb to a pick-and-place centroid file (M23).

The assembly-house companion to :mod:`pcb_export_gerbers` /
:mod:`pcb_export_drill`. A pick-and-place (also called "centroid",
"component placement", or ".pos") file tells the assembly machine
where each component lands, at what angle, and on which side — without
this the pick-and-place head doesn't know where to drop the part.
Every turnkey assembly order ships three artifacts: gerbers + drill
(the fabrication set) and this pos file (the assembly set).

Status enum:

* **ok**              — kicad-cli exited cleanly and the output file
                        exists with nonzero size.
* **dry_run**         — caller passed ``dry_run=True``; we report the
                        planned argv + resolved output_path.
* **pcb_not_found**   — input path is missing or not a .kicad_pcb.
* **cli_failed**      — kicad-cli didn't run cleanly.
* **output_missing**  — exit 0 but nothing on disk (or zero bytes).

Format scope: we support ``ascii`` and ``csv`` here. The ``gerber``
format kicad-cli also accepts emits a *directory* of two gerber files
(one per side) — that's close enough to the gerber-export pattern that
it belongs in its own tool if we need it; for now the 98%-case single-
file CSV output for the assembly house is what callers want.

The assembly house default is **CSV** (parseable by every P&P CAM
program on the market) but kicad-cli's own default is ``ascii``.
Callers fall through to kicad-cli's default unless they set ``format``.
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

# Pick-and-place export is IO-bound — it iterates the footprint list
# and writes one line per component. Even a 1000-component dense SMT
# board finishes in well under 10 s. 60 s gives slack for very large
# panels + slow filesystems without camping on a stuck CLI.
_POS_TIMEOUT_SEC = 60.0


# -- input / output --------------------------------------------------------


class PcbExportPosInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    output_path: Path | None = Field(
        default=None,
        description=(
            "Destination path for the pos file. Defaults to the PCB's "
            "stem with a '-pos.<ext>' suffix next to the input (e.g. "
            "board.kicad_pcb → board-pos.csv for format='csv'). Parent "
            "directory is created if missing."
        ),
    )
    format: Literal["ascii", "csv"] = Field(
        default="csv",
        description=(
            "Output format. 'csv' is what every assembly-house P&P CAM "
            "program expects and is the recommended default. 'ascii' is "
            "the legacy whitespace-aligned dump kicad-cli historically "
            "defaulted to — pick it only if you need to paste into an "
            "internal tool that parses the ascii layout."
        ),
    )
    side: Literal["front", "back", "both"] = Field(
        default="both",
        description=(
            "Which side(s) to emit. 'both' writes a combined file — the "
            "normal assembly-house deliverable. 'front' / 'back' split "
            "the file per side; pick these when your fab requested one "
            "file per side or for internal documentation."
        ),
    )
    units: Literal["in", "mm"] = Field(
        default="mm",
        description=(
            "Coordinate units. 'mm' is what every modern assembly house "
            "expects and is the SI default. 'in' exists for legacy "
            "CAM programs that only read inch-unit pos files."
        ),
    )
    use_drill_file_origin: bool = Field(
        default=False,
        description=(
            "Use the PCB's drill/auxiliary origin for coordinates. "
            "Default False uses the page origin — what most assembly "
            "houses expect. Only set True if your fab has specifically "
            "asked for drill-origin-relative coordinates."
        ),
    )
    bottom_negate_x: bool = Field(
        default=False,
        description=(
            "Negate the X coordinate for bottom-side components. Some "
            "pick-and-place machines read the bottom view as mirrored; "
            "enabling this flips the X axis so the coordinates match "
            "what the machine expects. Most modern P&P lines don't need "
            "it — verify with the fab before enabling."
        ),
    )
    smd_only: bool = Field(
        default=False,
        description=(
            "Emit only SMD-attribute components. Useful when your "
            "assembly line only does reflow/SMT and hand-assembles or "
            "wave-solders the through-hole parts. Off by default — the "
            "normal deliverable includes every placeable component."
        ),
    )
    exclude_fp_th: bool = Field(
        default=False,
        description=(
            "Exclude through-hole footprints (those with TH/NPTH pads). "
            "Orthogonal to smd_only: this uses the footprint's pad "
            "classification rather than the SMD attribute flag. Pick "
            "whichever matches how your library classifies parts."
        ),
    )
    exclude_dnp: bool = Field(
        default=False,
        description=(
            "Exclude components marked Do Not Populate. Most assembly "
            "houses want DNPs removed from the pos file so the machine "
            "doesn't try to place them. Off by default for backwards "
            "compatibility — kicad-cli's own default keeps them in."
        ),
    )
    exclude_footprints_with_th: bool = Field(
        default=False,
        description=(
            "Alias for exclude_fp_th kept for forward-compat with "
            "kicad-cli's evolving flag names. If both are True, the "
            "flag fires once — harmless duplication."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If True, validate inputs and report the planned argv + "
            "resolved output_path without invoking kicad-cli."
        ),
    )


class PcbExportPosOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "pcb_not_found",
        "cli_failed",
        "output_missing",
    ]
    pcb_path: str | None = Field(default=None)
    output_path: str | None = Field(default=None)
    size_bytes: int | None = Field(default=None)
    cli_argv: list[str] | None = Field(default=None)
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class PcbExportPosTool(Tool[PcbExportPosInput, PcbExportPosOutput]):
    """Render a .kicad_pcb to a pick-and-place centroid file via `kicad-cli pcb export pos`."""

    name = "pcb_export_pos"
    version = "0.1.0"
    description = (
        "Render a .kicad_pcb to a pick-and-place / centroid file via "
        "`kicad-cli pcb export pos`. Returns the output path and size. "
        "Defaults (format=csv, side=both, units=mm) match what most "
        "assembly houses expect. Supports dry_run for safe preview."
    )
    input_model = PcbExportPosInput
    output_model = PcbExportPosOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: PcbExportPosInput) -> PcbExportPosOutput:
        # 1. Resolve PCB path.
        pcb_path = input.pcb_path.expanduser().resolve()
        if not pcb_path.exists():
            return PcbExportPosOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbExportPosOutput(
                status="pcb_not_found",
                pcb_path=str(pcb_path),
                note=(
                    f"not a .kicad_pcb file: {pcb_path} (got suffix "
                    f"{pcb_path.suffix!r})."
                ),
            )

        # 2. Resolve output path. Default: sibling '<stem>-pos.<ext>'.
        # The '-pos' suffix plus format extension matches the convention
        # every KiCAD user recognises.
        if input.output_path is None:
            ext = "csv" if input.format == "csv" else "pos"
            output_path = pcb_path.with_name(f"{pcb_path.stem}-pos.{ext}")
        else:
            output_path = input.output_path.expanduser().resolve()

        # 3. Build argv.
        argv: list[str] = [
            "pcb",
            "export",
            "pos",
            "--output",
            str(output_path),
            "--format",
            input.format,
            "--side",
            input.side,
            "--units",
            input.units,
        ]
        if input.use_drill_file_origin:
            argv.append("--use-drill-file-origin")
        if input.bottom_negate_x:
            argv.append("--bottom-negate-x")
        if input.smd_only:
            argv.append("--smd-only")
        if input.exclude_fp_th or input.exclude_footprints_with_th:
            argv.append("--exclude-fp-th")
        if input.exclude_dnp:
            argv.append("--exclude-dnp")
        argv.append(str(pcb_path))

        # 4. Dry-run short-circuit.
        if input.dry_run:
            return PcbExportPosOutput(
                status="dry_run",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    "dry_run=True; no files were written. Re-run with "
                    "dry_run=False to actually invoke kicad-cli."
                ),
            )

        # 5. Resolve CLI path.
        backend = self._cli_backend
        if backend is None:
            backend = CliBackend()
        await backend.probe()
        cli_path = backend.cli_path
        if cli_path is None:
            return PcbExportPosOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        # 6. Ensure parent exists.
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return PcbExportPosOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"could not create output directory: {exc}",
            )

        # 7. Invoke.
        try:
            result = await run_cli(
                tuple(argv),
                cli_path=Path(cli_path),
                timeout=_POS_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return PcbExportPosOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli timed out after {exc.timeout:.0f}s",
            )
        except CliError as exc:
            return PcbExportPosOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return PcbExportPosOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    f"kicad-cli exited {result.exit_code}: "
                    f"{stderr_excerpt or '<no stderr output>'}"
                ),
            )

        # 8. Verify output exists and is nonempty.
        if not output_path.exists():
            return PcbExportPosOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    "kicad-cli exited 0 but the expected output file is "
                    "not on disk. Does the board have any placeable "
                    "components on the selected side?"
                ),
            )
        try:
            size = output_path.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            return PcbExportPosOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    "output file exists but is zero bytes — filters may "
                    "have excluded every component (smd_only + "
                    "exclude_dnp + exclude_fp_th on a through-hole board)."
                ),
            )

        return PcbExportPosOutput(
            status="ok",
            pcb_path=str(pcb_path),
            output_path=str(output_path),
            size_bytes=size,
            cli_argv=argv,
        )


__all__ = [
    "PcbExportPosInput",
    "PcbExportPosOutput",
    "PcbExportPosTool",
]
