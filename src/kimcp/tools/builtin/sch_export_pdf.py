"""sch_export_pdf — render a .kicad_sch to a PDF (M25).

The schematic-side twin of :mod:`pcb_export_pdf`. Shells out to
``kicad-cli sch export pdf`` to produce a PDF containing each sheet of
a schematic. The tool is the primary deliverable for schematic review:
design review meetings, CAD-to-EE handoff, safety gates, and the
canonical archive artifact for a released design all read the PDF.

Status enum mirrors :mod:`pcb_export_pdf`:

* **ok**              — kicad-cli wrote the PDF with nonzero size.
* **dry_run**         — caller passed ``dry_run=True``.
* **sch_not_found**   — missing or wrong-suffix input.
* **cli_failed**      — kicad-cli errored.
* **output_missing**  — exit 0 but PDF missing or zero bytes.
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

_SCH_PDF_TIMEOUT_SEC = 180.0


# -- input / output --------------------------------------------------------


class SchExportPdfInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    output_path: Path | None = Field(
        default=None,
        description=(
            "Destination PDF path. Defaults to the schematic's stem "
            "with a '.pdf' suffix next to the input."
        ),
    )
    pages: list[str] | None = Field(
        default=None,
        description=(
            "Page numbers or names to include (hierarchical schematics). "
            "Null emits every page — what review PDFs typically want. "
            "Pass e.g. ['1', '3'] to emit only specific sheets."
        ),
    )
    theme: str | None = Field(
        default=None,
        description="KiCAD color theme name. Null uses the host default.",
    )
    black_and_white: bool = Field(
        default=False,
        description="Render in black and white (print-friendly / colorblind-safe).",
    )
    exclude_drawing_sheet: bool = Field(
        default=False,
        description=(
            "Omit the page border / drawing sheet. Off by default — "
            "the title block belongs on a review PDF."
        ),
    )
    no_background_color: bool = Field(
        default=False,
        description=(
            "Skip the background color fill. Useful for printing on "
            "colored paper or composing the PDF into a larger document."
        ),
    )
    define_vars: dict[str, str] | None = Field(
        default=None,
        description=(
            "Text-variable overrides passed as `--define-var NAME=VAL`. "
            "Lets callers swap e.g. REV or DATE without mutating the "
            "schematic. Keys must match the schematic's declared "
            "variable names."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the planned argv without invoking kicad-cli.",
    )


class SchExportPdfOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "cli_failed",
        "output_missing",
    ]
    sch_path: str | None = Field(default=None)
    output_path: str | None = Field(default=None)
    size_bytes: int | None = Field(default=None)
    cli_argv: list[str] | None = Field(default=None)
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class SchExportPdfTool(Tool[SchExportPdfInput, SchExportPdfOutput]):
    """Render a .kicad_sch to a PDF via `kicad-cli sch export pdf`."""

    name = "sch_export_pdf"
    version = "0.1.0"
    description = (
        "Render a .kicad_sch to a multi-page PDF via "
        "`kicad-cli sch export pdf`. Defaults emit every hierarchical "
        "page in color with the drawing sheet. Supports theme, B&W, "
        "page selection, and variable overrides."
    )
    input_model = SchExportPdfInput
    output_model = SchExportPdfOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: SchExportPdfInput) -> SchExportPdfOutput:
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists() or sch_path.is_dir():
            return SchExportPdfOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchExportPdfOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        if input.output_path is None:
            output_path = sch_path.with_suffix(".pdf")
        else:
            output_path = input.output_path.expanduser().resolve()

        argv: list[str] = [
            "sch",
            "export",
            "pdf",
            "--output",
            str(output_path),
        ]
        if input.pages:
            argv.extend(["--pages", ",".join(input.pages)])
        if input.theme is not None:
            argv.extend(["--theme", input.theme])
        if input.black_and_white:
            argv.append("--black-and-white")
        if input.exclude_drawing_sheet:
            argv.append("--exclude-drawing-sheet")
        if input.no_background_color:
            argv.append("--no-background-color")
        if input.define_vars:
            for k, v in input.define_vars.items():
                argv.extend(["--define-var", f"{k}={v}"])
        argv.append(str(sch_path))

        if input.dry_run:
            return SchExportPdfOutput(
                status="dry_run",
                sch_path=str(sch_path),
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
            return SchExportPdfOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return SchExportPdfOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"could not create output directory: {exc}",
            )

        try:
            result = await run_cli(
                tuple(argv),
                cli_path=Path(cli_path),
                timeout=_SCH_PDF_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return SchExportPdfOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli timed out after {exc.timeout:.0f}s",
            )
        except CliError as exc:
            return SchExportPdfOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return SchExportPdfOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                cli_argv=argv,
                note=(
                    f"kicad-cli exited {result.exit_code}: "
                    f"{stderr_excerpt or '<no stderr output>'}"
                ),
            )

        if not output_path.exists():
            return SchExportPdfOutput(
                status="output_missing",
                sch_path=str(sch_path),
                output_path=str(output_path),
                cli_argv=argv,
                note="kicad-cli exited 0 but the PDF is not on disk.",
            )
        try:
            size = output_path.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            return SchExportPdfOutput(
                status="output_missing",
                sch_path=str(sch_path),
                output_path=str(output_path),
                cli_argv=argv,
                note="PDF exists but is zero bytes.",
            )

        return SchExportPdfOutput(
            status="ok",
            sch_path=str(sch_path),
            output_path=str(output_path),
            size_bytes=size,
            cli_argv=argv,
        )


__all__ = [
    "SchExportPdfInput",
    "SchExportPdfOutput",
    "SchExportPdfTool",
]
