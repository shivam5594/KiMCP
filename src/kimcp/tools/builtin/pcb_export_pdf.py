"""pcb_export_pdf — render a .kicad_pcb to a PDF plot (M24).

Human-facing companion to the fab-oriented gerber/drill/step exports.
PDF plots are the documentation deliverable: design reviews, assembly
overlays, turnkey fab's "here's what the copper looks like" sanity
check. The output is a single PDF file; callers pick which layers to
include and whether each layer gets its own page (``mode='multipage'``)
or is composited on a single page (``mode='single'``).

Status enum:

* **ok**              — kicad-cli ran cleanly; PDF exists with nonzero size.
* **dry_run**         — caller passed ``dry_run=True``.
* **pcb_not_found**   — missing or wrong-suffix input.
* **cli_failed**      — kicad-cli errored.
* **output_missing**  — exit 0 but no PDF landed (or zero bytes).

Scope: we support ``mode='single'`` (one-page composite) and
``mode='multipage'`` (one layer per page in one PDF). ``mode='separate'``
emits a *directory* of per-layer PDFs — that's a different envelope
shape (list of files, not one path), and we defer it until a concrete
use case lands. A ``pcb_export_pdf_split`` tool can adopt the
directory pattern of :mod:`pcb_export_drill` when the need arises.
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

_PDF_TIMEOUT_SEC = 180.0

# Canonical "what most fab/review PDFs include". Callers that want a
# custom set pass ``layers=`` explicitly. Mirrors the default in
# :mod:`pcb_export_gerbers` (minus Paste, which is rarely useful on a
# review PDF and adds visual clutter).
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


class PcbExportPdfInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    output_path: Path | None = Field(
        default=None,
        description=(
            "Destination PDF path. Defaults to the PCB's stem with a "
            "'.pdf' suffix next to the input (e.g. board.kicad_pcb → "
            "board.pdf). Parent directory is created if missing."
        ),
    )
    layers: list[str] | None = Field(
        default=None,
        description=(
            "Layer list (KiCAD names, e.g. 'F.Cu', 'Edge.Cuts'). Null "
            "falls through to a fab-standard 7-layer default "
            "(F.Cu, B.Cu, F.Mask, B.Mask, F.SilkS, B.SilkS, Edge.Cuts). "
            "Internal copper layers (In1.Cu, In2.Cu, …) must be added "
            "explicitly for multi-layer boards."
        ),
    )
    common_layers: list[str] | None = Field(
        default=None,
        description=(
            "Layers drawn on *every* page in multipage mode (e.g. "
            "Edge.Cuts on every copper-layer page). Ignored in single "
            "mode. Typical: ['Edge.Cuts'] so each per-layer page shows "
            "the board outline for orientation."
        ),
    )
    mode: Literal["single", "multipage"] = Field(
        default="multipage",
        description=(
            "Layer composition. 'multipage' emits one page per layer in "
            "one PDF — the typical design-review deliverable. 'single' "
            "composites every selected layer onto one page (useful for "
            "a single-sheet board overview). mode='separate' (one PDF "
            "per layer in a directory) is deferred to a future tool."
        ),
    )
    mirror: bool = Field(
        default=False,
        description=(
            "Mirror the plot around the vertical axis. Useful when "
            "viewing the bottom side from its own perspective instead "
            "of through the board. Off by default."
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
    include_border_title: bool = Field(
        default=True,
        description=(
            "Include the page border and title block. On by default — "
            "review PDFs read better with the title block as context."
        ),
    )
    black_and_white: bool = Field(
        default=False,
        description=(
            "Render in black and white. On for print-friendly or "
            "colorblind-safe output; off by default for screen viewing "
            "where colored layers aid readability."
        ),
    )
    negative: bool = Field(
        default=False,
        description="Emit a negative (inverted) plot. Niche — for film-based fab workflows.",
    )
    drill_shape: Literal["none", "small", "real"] = Field(
        default="real",
        description=(
            "How drill holes appear in the plot. 'real' draws them at "
            "actual size (what a fab reviewer expects). 'small' uses a "
            "fixed small marker (readable overview at low zoom). "
            "'none' suppresses drill marks entirely."
        ),
    )
    theme: str | None = Field(
        default=None,
        description=(
            "KiCAD color theme name to use. Null falls through to the "
            "default theme installed on the host. Useful when a team "
            "standardises on a specific theme for review PDFs."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the planned argv without invoking kicad-cli.",
    )


class PcbExportPdfOutput(ToolOutput):
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


class PcbExportPdfTool(Tool[PcbExportPdfInput, PcbExportPdfOutput]):
    """Render a .kicad_pcb to a PDF plot via `kicad-cli pcb export pdf`."""

    name = "pcb_export_pdf"
    version = "0.1.0"
    description = (
        "Render a .kicad_pcb to a multi-layer PDF plot via "
        "`kicad-cli pcb export pdf`. Defaults produce a multipage PDF "
        "with the fab-standard 7-layer set. Supports mirror, drill-shape, "
        "theme, and dry_run."
    )
    input_model = PcbExportPdfInput
    output_model = PcbExportPdfOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: PcbExportPdfInput) -> PcbExportPdfOutput:
        pcb_path = input.pcb_path.expanduser().resolve()
        if not pcb_path.exists():
            return PcbExportPdfOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbExportPdfOutput(
                status="pcb_not_found",
                pcb_path=str(pcb_path),
                note=(
                    f"not a .kicad_pcb file: {pcb_path} (got suffix "
                    f"{pcb_path.suffix!r})."
                ),
            )

        if input.output_path is None:
            output_path = pcb_path.with_suffix(".pdf")
        else:
            output_path = input.output_path.expanduser().resolve()

        layers = input.layers if input.layers else list(_DEFAULT_LAYERS)

        argv: list[str] = [
            "pcb",
            "export",
            "pdf",
            "--output",
            str(output_path),
            "--layers",
            ",".join(layers),
        ]
        if input.common_layers:
            argv.extend(["--common-layers", ",".join(input.common_layers)])
        # kicad-cli's mode flags are the --mode-* family — each is a
        # no-arg switch. We always emit exactly one.
        if input.mode == "single":
            argv.append("--mode-single")
        else:
            argv.append("--mode-multipage")
        if input.mirror:
            argv.append("--mirror")
        if input.exclude_refdes:
            argv.append("--exclude-refdes")
        if input.exclude_value:
            argv.append("--exclude-value")
        if input.include_border_title:
            argv.append("--include-border-title")
        if input.black_and_white:
            argv.append("--black-and-white")
        if input.negative:
            argv.append("--negative")
        # drill-shape-opt: kicad-cli accepts 0/1/2 for none/small/real.
        drill_map = {"none": "0", "small": "1", "real": "2"}
        argv.extend(["--drill-shape-opt", drill_map[input.drill_shape]])
        if input.theme is not None:
            argv.extend(["--theme", input.theme])
        argv.append(str(pcb_path))

        if input.dry_run:
            return PcbExportPdfOutput(
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
            return PcbExportPdfOutput(
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
            return PcbExportPdfOutput(
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
                timeout=_PDF_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return PcbExportPdfOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli timed out after {exc.timeout:.0f}s",
            )
        except CliError as exc:
            return PcbExportPdfOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return PcbExportPdfOutput(
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
            return PcbExportPdfOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note="kicad-cli exited 0 but the PDF is not on disk.",
            )
        try:
            size = output_path.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            return PcbExportPdfOutput(
                status="output_missing",
                pcb_path=str(pcb_path),
                output_path=str(output_path),
                cli_argv=argv,
                note="PDF exists but is zero bytes.",
            )

        return PcbExportPdfOutput(
            status="ok",
            pcb_path=str(pcb_path),
            output_path=str(output_path),
            size_bytes=size,
            cli_argv=argv,
        )


__all__ = [
    "PcbExportPdfInput",
    "PcbExportPdfOutput",
    "PcbExportPdfTool",
]
