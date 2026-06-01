"""pcb_export_dxf — render a .kicad_pcb to a 2D DXF plot.

DXF is the hand-off format for mechanical CAD: Fusion 360,
SolidWorks, AutoCAD, FreeCAD. Typical consumers:

* Mechanical designers who need the board outline + mounting holes
  to model the enclosure.
* Panelizer / CAM workflows that trace copper or silk as vector
  geometry.
* Legacy fab houses that still accept DXF for board outlines.

The surface is narrower than the PDF/SVG plotters because DXF is
a bare-bones 2D vector format — no theme, no negative, no page
size, no black-and-white (DXF has no concept of a "color theme";
layers map to DXF layers directly). Parameters specific to DXF:

* ``output_units`` — ``mm`` or ``in``. **This is load-bearing** for
  anything downstream that reads DXF: import with the wrong
  assumed unit scales the geometry by 25.4 and disasters follow.
* ``polygon_mode`` — render thick lines as filled polygons. Better
  fidelity at the cost of file size; important for silk artwork or
  copper floods round-tripping into a CAM tool.
* ``use_drill_origin`` — place the DXF origin at the drill/place
  origin defined in the PCB. Makes the exported geometry align
  with the fab's coordinate system when the mechanical designer
  pulls it in.

Status enum mirrors pcb_export_pdf/pcb_export_svg:

* **ok**              — kicad-cli ran cleanly; DXF exists with nonzero size.
* **dry_run**         — caller passed ``dry_run=True``.
* **pcb_not_found**   — missing or wrong-suffix input.
* **cli_failed**      — kicad-cli errored.
* **output_missing**  — exit 0 but no DXF landed (or zero bytes).

MUTATE classification (filesystem write).
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

_DXF_TIMEOUT_SEC = 180.0

# Mechanical hand-off is usually about the outline + fab/silk layers,
# not copper. Edge.Cuts is the critical one (the board outline); F.Fab
# / F.SilkS help the mechanical designer understand placement. Callers
# who actually want copper geometry pass the layers explicitly.
_DEFAULT_LAYERS: tuple[str, ...] = (
    "Edge.Cuts",
    "F.Fab",
    "B.Fab",
    "F.SilkS",
    "B.SilkS",
)


# -- input / output --------------------------------------------------------


class PcbExportDxfInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    output_path: Path | None = Field(
        default=None,
        description=(
            "Destination DXF path. Defaults to the PCB's stem with a "
            "'.dxf' suffix next to the input (e.g. board.kicad_pcb → "
            "board.dxf). Parent directory is created if missing."
        ),
    )
    layers: list[str] | None = Field(
        default=None,
        description=(
            "Layer list (KiCAD names). Null falls through to the "
            "mechanical-handoff default (Edge.Cuts + F.Fab/B.Fab + "
            "F.SilkS/B.SilkS). Copper layers (F.Cu, B.Cu) must be "
            "added explicitly — DXF is rarely used for copper hand-off."
        ),
    )
    mirror: bool = Field(
        default=False,
        description=(
            "Mirror the plot around the vertical axis. Occasionally "
            "needed when the mechanical consumer expects bottom-view "
            "geometry."
        ),
    )
    output_units: Literal["mm", "in"] = Field(
        default="mm",
        description=(
            "DXF geometric units. **Load-bearing** — a mechanical CAD "
            "tool that imports the DXF assuming the wrong unit will "
            "scale the geometry by 25.4. Default is ``'mm'`` to match "
            "KiCAD's native units; switch to ``'in'`` for legacy "
            "inch-native workflows."
        ),
    )
    polygon_mode: bool = Field(
        default=False,
        description=(
            "Render thick lines and fills as filled polygons instead "
            "of open line strings. Higher fidelity for silk / copper "
            "artwork at the cost of file size. Off by default because "
            "the common mechanical-outline use case doesn't need it."
        ),
    )
    use_drill_origin: bool = Field(
        default=False,
        description=(
            "Place the DXF origin at the PCB's drill/place origin. "
            "Aligns the exported coordinate system with the fab's "
            "pick-and-place / NC files. Off by default because most "
            "mechanical workflows keep the page origin."
        ),
    )
    exclude_refdes: bool = Field(
        default=False,
        description="Suppress reference designator text on the plot.",
    )
    exclude_value: bool = Field(
        default=False,
        description="Suppress component value text on the plot.",
    )
    drill_shape: Literal["none", "small", "real"] = Field(
        default="real",
        description=(
            "How drill holes appear. ``'real'`` draws them at actual "
            "size (default). ``'small'`` uses a fixed small marker. "
            "``'none'`` suppresses drill marks entirely — common when "
            "the DXF will be re-processed by a CAM tool that gets "
            "drills from the Excellon file instead."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the planned argv without invoking kicad-cli.",
    )


class PcbExportDxfOutput(ToolOutput):
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


class PcbExportDxfTool(Tool[PcbExportDxfInput, PcbExportDxfOutput]):
    """Render a .kicad_pcb to a 2D DXF plot via `kicad-cli pcb export dxf`."""

    name = "pcb_export_dxf"
    version = "0.1.0"
    description = (
        "Render a .kicad_pcb to a 2D DXF via `kicad-cli pcb export dxf`. "
        "Default layer set targets the mechanical-handoff use case "
        "(Edge.Cuts + F.Fab/B.Fab + silks). Exposes output_units "
        "(mm|in), polygon_mode, and use_drill_origin for CAM alignment."
    )
    input_model = PcbExportDxfInput
    output_model = PcbExportDxfOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: PcbExportDxfInput) -> PcbExportDxfOutput:
        pcb_path = input.pcb_path.expanduser().resolve()
        if not pcb_path.exists():
            return PcbExportDxfOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbExportDxfOutput(
                status="pcb_not_found",
                pcb_path=str(pcb_path),
                note=(
                    f"not a .kicad_pcb file: {pcb_path} (got suffix "
                    f"{pcb_path.suffix!r})."
                ),
            )

        if input.output_path is None:
            output_path = pcb_path.with_suffix(".dxf")
        else:
            output_path = input.output_path.expanduser().resolve()

        layers = input.layers if input.layers else list(_DEFAULT_LAYERS)

        argv: list[str] = [
            "pcb",
            "export",
            "dxf",
            "--output",
            str(output_path),
            "--layers",
            ",".join(layers),
            "--output-units",
            input.output_units,
        ]
        if input.mirror:
            argv.append("--mirror")
        if input.polygon_mode:
            argv.append("--polygon-mode")
        if input.use_drill_origin:
            argv.append("--use-drill-origin")
        if input.exclude_refdes:
            argv.append("--exclude-refdes")
        if input.exclude_value:
            argv.append("--exclude-value")
        drill_map = {"none": "0", "small": "1", "real": "2"}
        argv.extend(["--drill-shape-opt", drill_map[input.drill_shape]])
        argv.append(str(pcb_path))

        if input.dry_run:
            return PcbExportDxfOutput(
                status="dry_run",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note="dry_run=True; no files were written.",
            )

        backend = self._cli_backend
        if backend is None:
            backend = CliBackend()
        await backend.probe()
        cli_path = backend.cli_path
        if cli_path is None:
            return PcbExportDxfOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return PcbExportDxfOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"could not create output directory: {exc}",
            )

        try:
            result = await run_cli(
                tuple(argv),
                cli_path=Path(cli_path),
                timeout=_DXF_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return PcbExportDxfOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli timed out after {exc.timeout:.0f}s",
            )
        except CliError as exc:
            return PcbExportDxfOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return PcbExportDxfOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    f"kicad-cli exited {result.exit_code}: "
                    f"{stderr_excerpt or '<no stderr output>'}"
                ),
            )

        if not output_path.exists():
            return PcbExportDxfOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note="kicad-cli exited 0 but the DXF is not on disk.",
            )
        try:
            size = output_path.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            return PcbExportDxfOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note="DXF exists but is zero bytes.",
            )

        return PcbExportDxfOutput(
            status="ok",
            pcb_path=str(pcb_path),
            output_path=str(output_path),
            size_bytes=size,
            cli_argv=argv,
        )


__all__ = [
    "PcbExportDxfInput",
    "PcbExportDxfOutput",
    "PcbExportDxfTool",
]
