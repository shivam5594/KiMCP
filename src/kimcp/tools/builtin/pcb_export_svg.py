"""pcb_export_svg — render a .kicad_pcb to an SVG plot.

SVG is the embed-friendly sibling of ``pcb_export_pdf``: web docs,
issue trackers, README screenshots, wiki pages. Unlike the
multi-page PDF, the single-SVG shape is a one-shot composite —
every selected layer painted onto one canvas — which suits
embedding directly in markdown or rendering inline in a browser.

Separate / per-layer SVG output (``--mode-separate``) is *deferred*
for the same reason we deferred the PDF's ``separate`` mode: it
emits a directory of files, which is a different envelope shape.
A future ``pcb_export_svg_split`` tool can adopt the directory
pattern of :mod:`pcb_export_drill` when the need arises.

What's different from ``pcb_export_pdf`` besides the file format:

* **No mode flag** — SVG ships as single-file composite only.
* **``page_size`` knob** — kicad-cli exposes three canvas sizes:
  ``full`` (page frame + title block), ``current`` (page size,
  no frame), ``board_only`` (tight crop to the board outline).
  The page-tight mode is especially useful for embedding in docs
  where the title block is noise. Selecting ``board_only`` also
  implies the drawing sheet is suppressed.

Status enum mirrors pcb_export_pdf:

* **ok**              — kicad-cli ran cleanly; SVG exists with nonzero size.
* **dry_run**         — caller passed ``dry_run=True``.
* **pcb_not_found**   — missing or wrong-suffix input.
* **cli_failed**      — kicad-cli errored.
* **output_missing**  — exit 0 but no SVG landed (or zero bytes).

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

_SVG_TIMEOUT_SEC = 180.0

# Same sensible default as pcb_export_pdf — the layers any review
# viewer expects on a plot. Callers that want internal copper or fab
# layers pass an explicit list.
_DEFAULT_LAYERS: tuple[str, ...] = (
    "F.Cu",
    "B.Cu",
    "F.Mask",
    "B.Mask",
    "F.SilkS",
    "B.SilkS",
    "Edge.Cuts",
)


# -- input / output --------------------------------------------------------


class PcbExportSvgInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    output_path: Path | None = Field(
        default=None,
        description=(
            "Destination SVG path. Defaults to the PCB's stem with a "
            "'.svg' suffix next to the input (e.g. board.kicad_pcb → "
            "board.svg). Parent directory is created if missing."
        ),
    )
    layers: list[str] | None = Field(
        default=None,
        description=(
            "Layer list (KiCAD names, e.g. 'F.Cu', 'Edge.Cuts'). Null "
            "falls through to the same 7-layer default as pcb_export_pdf "
            "(F.Cu, B.Cu, F.Mask, B.Mask, F.SilkS, B.SilkS, Edge.Cuts). "
            "Internal copper layers must be added explicitly."
        ),
    )
    mirror: bool = Field(
        default=False,
        description=(
            "Mirror the plot around the vertical axis — matches the "
            "bottom-side-as-viewed-from-bottom convention."
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
    page_size: Literal["full", "current", "board_only"] = Field(
        default="full",
        description=(
            "Canvas size. ``'full'`` draws the page frame + title block "
            "(kicad-cli --page-size-mode 0, the default). ``'current'`` "
            "uses the current page size without a frame (mode 1). "
            "``'board_only'`` crops tightly to the board outline — "
            "ideal for README embeds and docs where the title block is "
            "noise (mode 2)."
        ),
    )
    black_and_white: bool = Field(
        default=False,
        description=(
            "Render in black and white. Useful for print-friendly docs "
            "or where layer colors fight the page theme."
        ),
    )
    negative: bool = Field(
        default=False,
        description="Emit a negative (inverted) plot. Niche — etching / film workflows.",
    )
    drill_shape: Literal["none", "small", "real"] = Field(
        default="real",
        description=(
            "How drill holes appear in the plot. ``'real'`` draws them "
            "at actual size (default); ``'small'`` uses a fixed marker "
            "for low-zoom overviews; ``'none'`` suppresses drill marks."
        ),
    )
    theme: str | None = Field(
        default=None,
        description=(
            "KiCAD color theme to apply. Null falls through to the "
            "host's default theme. Useful when a team standardises on "
            "a specific theme for documentation plots."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the planned argv without invoking kicad-cli.",
    )


class PcbExportSvgOutput(ToolOutput):
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


class PcbExportSvgTool(Tool[PcbExportSvgInput, PcbExportSvgOutput]):
    """Render a .kicad_pcb to an SVG plot via `kicad-cli pcb export svg`."""

    name = "pcb_export_svg"
    version = "0.1.0"
    description = (
        "Render a .kicad_pcb to a single composite SVG via "
        "`kicad-cli pcb export svg`. Defaults: 7-layer fab-style plot, "
        "full-page canvas, real-size drill marks. Page-size modes cover "
        "embed use cases (board-only crop for README images)."
    )
    input_model = PcbExportSvgInput
    output_model = PcbExportSvgOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: PcbExportSvgInput) -> PcbExportSvgOutput:
        pcb_path = input.pcb_path.expanduser().resolve()
        if not pcb_path.exists():
            return PcbExportSvgOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbExportSvgOutput(
                status="pcb_not_found",
                pcb_path=str(pcb_path),
                note=(
                    f"not a .kicad_pcb file: {pcb_path} (got suffix "
                    f"{pcb_path.suffix!r})."
                ),
            )

        if input.output_path is None:
            output_path = pcb_path.with_suffix(".svg")
        else:
            output_path = input.output_path.expanduser().resolve()

        layers = input.layers if input.layers else list(_DEFAULT_LAYERS)

        argv: list[str] = [
            "pcb",
            "export",
            "svg",
            "--output",
            str(output_path),
            "--layers",
            ",".join(layers),
            # SVG has multiple mode flags (--mode-single / --mode-multi /
            # --mode-separate). We're single-file-only for now; emit the
            # flag explicitly so upstream default drift doesn't break us.
            "--mode-single",
        ]
        # kicad-cli's --page-size-mode takes 0/1/2.
        page_size_map = {"full": "0", "current": "1", "board_only": "2"}
        argv.extend(["--page-size-mode", page_size_map[input.page_size]])
        if input.mirror:
            argv.append("--mirror")
        if input.exclude_refdes:
            argv.append("--exclude-refdes")
        if input.exclude_value:
            argv.append("--exclude-value")
        if input.black_and_white:
            argv.append("--black-and-white")
        if input.negative:
            argv.append("--negative")
        drill_map = {"none": "0", "small": "1", "real": "2"}
        argv.extend(["--drill-shape-opt", drill_map[input.drill_shape]])
        if input.theme is not None:
            argv.extend(["--theme", input.theme])
        argv.append(str(pcb_path))

        if input.dry_run:
            return PcbExportSvgOutput(
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
            return PcbExportSvgOutput(
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
            return PcbExportSvgOutput(
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
                timeout=_SVG_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return PcbExportSvgOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli timed out after {exc.timeout:.0f}s",
            )
        except CliError as exc:
            return PcbExportSvgOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return PcbExportSvgOutput(
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
            return PcbExportSvgOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note="kicad-cli exited 0 but the SVG is not on disk.",
            )
        try:
            size = output_path.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            return PcbExportSvgOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note="SVG exists but is zero bytes.",
            )

        return PcbExportSvgOutput(
            status="ok",
            pcb_path=str(pcb_path),
            output_path=str(output_path),
            size_bytes=size,
            cli_argv=argv,
        )


__all__ = [
    "PcbExportSvgInput",
    "PcbExportSvgOutput",
    "PcbExportSvgTool",
]
