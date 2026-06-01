"""sch_export_svg — render a .kicad_sch to per-sheet SVG files.

Twin of :mod:`sch_export_pdf` targeting embed / web-review workflows.
Key structural difference: ``kicad-cli sch export svg`` writes *one
SVG file per sheet* of the hierarchy, not a single multi-page SVG.
So the envelope mirrors :mod:`pcb_export_gerbers` — the output is a
directory, and we return a list of generated files rather than a
single ``output_path``.

Why SVG instead of just the PDF:

* Embed-friendly — drop the SVG straight into a README, wiki, or
  issue tracker without a PDF viewer round-trip.
* Text remains selectable and searchable (unlike a rasterised
  screenshot).
* Style-compositable — the consumer can apply CSS to the SVG if
  they want to re-theme the schematic for their docs palette.

Status enum:

* **ok**                — kicad-cli ran cleanly; ≥ 1 SVG landed.
* **dry_run**           — caller passed ``dry_run=True``.
* **sch_not_found**     — missing or wrong-suffix input.
* **cli_failed**        — kicad-cli errored.
* **no_files_produced** — exit 0 but no new SVG in ``output_dir``.

File discovery is a before/after diff of ``output_dir`` so we don't
depend on kicad-cli's stdout. Pre-existing SVGs are preserved; a
warning fires when the dir already had files at start, same policy
as :mod:`pcb_export_gerbers` (clean per-build dirs avoid stale-file
confusion).

MUTATE classification (filesystem write).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kimcp._types import Backend, ToolClass
from kimcp.backends.cli import CliBackend
from kimcp.cli.errors import CliError, CliTimeoutError
from kimcp.cli.runner import run_cli
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool

log = logging.getLogger(__name__)

_SCH_SVG_TIMEOUT_SEC = 180.0


# -- envelope sub-models ---------------------------------------------------


class GeneratedSchSvgFile(BaseModel):
    """One SVG file produced by ``kicad-cli sch export svg``.

    Just path + size — sheet-name extraction from the filename is
    best-effort and kicad-cli version-dependent, so we don't try to
    synthesise it. Callers who need the sheet mapping can follow up
    with ``sch_list_symbols`` or similar.
    """

    model_config = ConfigDict(extra="allow")

    path: str = Field(
        ...,
        description="Absolute, resolved path to the produced SVG file.",
    )
    size_bytes: int = Field(
        ...,
        description=(
            "On-disk size in bytes — useful for sanity-checking that a "
            "sheet wasn't silently empty."
        ),
    )


# -- input / output --------------------------------------------------------


class SchExportSvgInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    output_dir: Path | None = Field(
        default=None,
        description=(
            "Directory to write per-sheet SVGs into. Defaults to a "
            "sibling 'svg/' folder next to the schematic. Created if "
            "missing; pre-existing files are preserved but a warning "
            "fires (clean per-build dirs avoid stale-file mixing)."
        ),
    )
    pages: list[str] | None = Field(
        default=None,
        description=(
            "Page numbers or names to include (hierarchical schematics). "
            "Null emits every sheet in the hierarchy. Pass e.g. ['1', '3'] "
            "to emit only specific sheets."
        ),
    )
    theme: str | None = Field(
        default=None,
        description="KiCAD color theme name. Null falls through to the host default.",
    )
    black_and_white: bool = Field(
        default=False,
        description=(
            "Render in black and white. Useful for print-friendly docs "
            "or color-sensitive embedding contexts."
        ),
    )
    exclude_drawing_sheet: bool = Field(
        default=False,
        description=(
            "Omit the page border / drawing sheet. Often desirable for "
            "embed use cases where the title block is noise; off by "
            "default to match sch_export_pdf."
        ),
    )
    no_background_color: bool = Field(
        default=False,
        description=(
            "Skip the background color fill. Important for SVG embeds "
            "where you want the containing page's background to show "
            "through (dark-mode docs, transparent compositing)."
        ),
    )
    define_vars: dict[str, str] | None = Field(
        default=None,
        description=(
            "Text-variable overrides passed as ``--define-var NAME=VAL``. "
            "Swap REV/DATE for a doc build without mutating the "
            "schematic. Keys must match declared variable names."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If True, report the planned argv without invoking kicad-cli.",
    )


class SchExportSvgOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "cli_failed",
        "no_files_produced",
    ]
    sch_path: str | None = Field(default=None)
    output_dir: str | None = Field(default=None)
    generated_files: list[GeneratedSchSvgFile] = Field(
        default_factory=list,
        description=(
            "SVG files that appeared in output_dir during this call, "
            "sorted by filename for determinism. Pre-existing files "
            "are NOT included."
        ),
    )
    total_files: int = Field(
        default=0,
        description="Length of generated_files.",
    )
    total_bytes: int = Field(
        default=0,
        description="Sum of size_bytes across generated_files.",
    )
    cli_argv: list[str] | None = Field(default=None)
    note: str | None = Field(default=None)


# -- tool ------------------------------------------------------------------


class SchExportSvgTool(Tool[SchExportSvgInput, SchExportSvgOutput]):
    """Render a .kicad_sch to per-sheet SVGs via `kicad-cli sch export svg`."""

    name = "sch_export_svg"
    version = "0.1.0"
    description = (
        "Render a .kicad_sch to per-sheet SVG files via "
        "`kicad-cli sch export svg`. One SVG per hierarchy sheet, "
        "written into a directory. Supports theme, B&W, background "
        "suppression, page selection, and text-variable overrides."
    )
    input_model = SchExportSvgInput
    output_model = SchExportSvgOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: SchExportSvgInput) -> SchExportSvgOutput:
        sch_path = input.sch_path.expanduser().resolve()
        if not sch_path.exists() or sch_path.is_dir():
            return SchExportSvgOutput(
                status="sch_not_found",
                sch_path=None,
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchExportSvgOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r})."
                ),
            )

        if input.output_dir is None:
            output_dir = sch_path.parent / "svg"
        else:
            output_dir = input.output_dir.expanduser().resolve()

        argv: list[str] = [
            "sch",
            "export",
            "svg",
            "--output",
            str(output_dir),
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
            return SchExportSvgOutput(
                status="dry_run",
                sch_path=str(sch_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note="dry_run=True; no files were written.",
            )

        backend = self._cli_backend
        if backend is None:
            backend = CliBackend()
        await backend.probe()
        cli_path = backend.cli_path
        if cli_path is None:
            return SchExportSvgOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_dir=str(output_dir),
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return SchExportSvgOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=f"could not create output directory: {exc}",
            )

        pre_existing = _scan_dir(output_dir)
        dirty_dir_warning: str | None = None
        if pre_existing:
            dirty_dir_warning = (
                f"output_dir already contained {len(pre_existing)} file(s) "
                "before the export. generated_files only lists artifacts "
                "newly created by this call; pre-existing files were left "
                "alone. Consider a clean per-build directory to avoid "
                "mixing stale sheet exports."
            )

        try:
            result = await run_cli(
                tuple(argv),
                cli_path=Path(cli_path),
                timeout=_SCH_SVG_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return SchExportSvgOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=f"kicad-cli timed out after {exc.timeout:.0f}s",
            )
        except CliError as exc:
            return SchExportSvgOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return SchExportSvgOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=(
                    f"kicad-cli exited {result.exit_code}: "
                    f"{stderr_excerpt or '<no stderr output>'}"
                ),
            )

        post_existing = _scan_dir(output_dir)
        # Narrow to SVG files — kicad-cli occasionally drops companion
        # metadata files in the same dir, and those shouldn't count
        # toward generated_files.
        new_files = sorted(
            (
                p for p in (post_existing - pre_existing)
                if p.suffix.lower() == ".svg"
            ),
            key=lambda p: p.name,
        )

        if not new_files:
            out_empty = SchExportSvgOutput(
                status="no_files_produced",
                sch_path=str(sch_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=(
                    "kicad-cli exited cleanly but no new SVG files "
                    "appeared in output_dir. Likely causes: page filter "
                    "excluded every sheet, or the schematic is empty."
                ),
            )
            if dirty_dir_warning is not None:
                out_empty.meta.warnings.append(dirty_dir_warning)
            return out_empty

        generated = [_describe_file(p) for p in new_files]
        out = SchExportSvgOutput(
            status="ok",
            sch_path=str(sch_path),
            output_dir=str(output_dir),
            generated_files=generated,
            total_files=len(generated),
            total_bytes=sum(f.size_bytes for f in generated),
            cli_argv=argv,
        )
        if dirty_dir_warning is not None:
            out.meta.warnings.append(dirty_dir_warning)
        return out


# -- discovery helpers ------------------------------------------------------


def _scan_dir(d: Path) -> set[Path]:
    """Return the set of regular files directly under ``d``.

    Non-recursive — kicad-cli writes SVGs as siblings. Returns an
    empty set if the directory doesn't exist yet (the caller may
    invoke this before mkdir).
    """
    if not d.exists():
        return set()
    return {p.resolve() for p in d.iterdir() if p.is_file()}


def _describe_file(path: Path) -> GeneratedSchSvgFile:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return GeneratedSchSvgFile(path=str(path), size_bytes=size)


__all__ = [
    "GeneratedSchSvgFile",
    "SchExportSvgInput",
    "SchExportSvgOutput",
    "SchExportSvgTool",
]
