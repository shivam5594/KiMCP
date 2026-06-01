"""sch_export_bom — export a .kicad_sch to a Bill of Materials (M11).

Second Thread B export tool, sibling of :mod:`sch_export_netlist`. Where
that tool emits the artifact pcbnew needs for "Update PCB from
schematic", this one emits the procurement-facing artifact a fab or
assembly house needs to actually buy the parts: a BOM listing every
component in the schematic with its reference, value, footprint, and
any custom fields the designer has attached.

Shells out to ``kicad-cli sch export bom`` and reports the single
produced file. Scoped deliberately: we expose the knobs that change
real BOM output (format, preset, exclude DNP, optional output_path)
but not the full field/group-by/label machinery from the KiCad BOM
dialog — the named ``preset`` mechanism is the canonical way to carry
custom structure without reinventing the designer's dialog in our
schema. Power users bind their preferred layout in the schematic
(``Grouped By Value``, ``Fabrication``, etc.) and pass the preset name.

Four output formats:

* **csv**  — default, comma-separated, what most fabs accept.
* **tsv**  — tab-separated, for spreadsheet pipelines that can't parse
              quoted-comma CSVs.
* **html** — human-readable; useful for review before fab upload.
* **xml**  — structured; feeds third-party BOM-management tooling.

Status enum mirrors :mod:`sch_export_netlist` so callers can share a
single dispatch branch across the export tools:

* **ok**               — kicad-cli ran cleanly and wrote the BOM.
* **dry_run**          — caller passed ``dry_run=True``; returns the
                         planned argv + resolved output_path without
                         invoking the CLI. Per ADR-0008.
* **sch_not_found**    — input path is missing or not a .kicad_sch.
* **cli_failed**       — kicad-cli didn't run cleanly (timeout, non-zero
                         exit, or the binary disappeared between probe
                         and call). ``note`` carries the reason.
* **no_file_produced** — kicad-cli exited 0 but no file appeared at
                         output_path. Defensive; usually means the
                         schematic has no symbols, or the filesystem
                         returned a stale listing.

Why MUTATE: same reasoning as ``sch_export_netlist`` — we write to the
filesystem. Safety model classifies any tool that produces files as
MUTATE so hosts can prompt before running it in policy-sensitive
contexts. ``dry_run`` is wired through per ADR-0008. We also surface an
overwrite warning when output_path already existed, matching the
sibling tool's audit-trail contract.
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

# Default CLI timeout for BOM export. BOM generation traverses the
# connectivity graph, groups symbols, and formats the output — slightly
# more work than a raw netlist but still bounded by the symbol count,
# not geometry. 120 s matches the sibling export tools; a ``timeout_sec``
# knob can arrive if real workflows motivate it.
_BOM_TIMEOUT_SEC = 120.0

# Extensions KiCAD historically associates with each BOM format. Used
# only when the caller doesn't pass ``output_path`` — explicit paths
# are honored verbatim. kicad-cli doesn't enforce an extension when
# ``-o`` is given; this map is a convenience for the common "just
# export a BOM next to the schematic" case.
_FORMAT_TO_EXT: dict[str, str] = {
    "csv": "csv",
    "tsv": "tsv",
    "html": "html",
    "xml": "xml",
}


# -- input / output --------------------------------------------------------


class SchExportBomInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    output_path: Path | None = Field(
        default=None,
        description=(
            "Destination file for the BOM. Defaults to "
            "`<sch_stem>.<ext>` next to the schematic, where `<ext>` is "
            "derived from `format` ('csv', 'tsv', 'html', 'xml'). Parent "
            "directory is created if missing. If a file already exists "
            "at this path, it will be overwritten and a warning is "
            "surfaced via meta.warnings."
        ),
    )
    format: Literal["csv", "tsv", "html", "xml"] = Field(
        default="csv",
        description=(
            "BOM output format. 'csv' is the default and what most fabs "
            "accept. 'tsv' helps spreadsheet pipelines that can't parse "
            "quoted commas. 'html' is human-readable for review. 'xml' "
            "feeds structured BOM-management tools."
        ),
    )
    preset: str | None = Field(
        default=None,
        description=(
            "Named BOM preset defined in the schematic (e.g. 'Grouped By "
            "Value', 'Fabrication'). Presets bundle the designer's "
            "field/group-by/sort choices — the canonical way to get "
            "custom BOM structure without replicating the KiCad BOM "
            "dialog here. When omitted, kicad-cli uses its built-in "
            "default layout."
        ),
    )
    exclude_dnp: bool = Field(
        default=False,
        description=(
            "Omit components marked 'Do Not Populate' from the BOM. Off "
            "by default to match kicad-cli; fabrication BOMs typically "
            "want this on, whereas design-review BOMs often want to keep "
            "DNPs visible. Pass True for a fab-ready BOM."
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


class SchExportBomOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "sch_not_found",
        "cli_failed",
        "no_file_produced",
    ]
    sch_path: str | None = Field(
        default=None,
        description=(
            "Resolved absolute path to the .kicad_sch. Null only when the "
            "file couldn't be located at all."
        ),
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Resolved absolute path the BOM was (or would be) written to. "
            "Populated whenever we got far enough to compute it."
        ),
    )
    size_bytes: int = Field(
        default=0,
        description=(
            "On-disk size of the produced BOM in bytes. Zero for non-ok "
            "statuses. Useful for sanity-checking that the file wasn't "
            "silently empty (a BOM against a symbol-less schematic would "
            "be just a header row)."
        ),
    )
    format: str = Field(
        default="",
        description="Echo of the requested format — audit trail for triage.",
    )
    cli_argv: list[str] | None = Field(
        default=None,
        description=(
            "Argv kicad-cli was (or would be) invoked with, EXCLUDING the "
            "binary path. Populated for both ok and dry_run; null otherwise. "
            "Lets callers reproduce the invocation by hand for debugging."
        ),
    )
    note: str | None = Field(
        default=None,
        description="Diagnostic string for non-ok statuses (reason + actionable hint).",
    )


# -- tool ------------------------------------------------------------------


class SchExportBomTool(Tool[SchExportBomInput, SchExportBomOutput]):
    """Export a .kicad_sch to a BOM via `kicad-cli sch export bom`."""

    name = "sch_export_bom"
    version = "0.1.0"
    description = (
        "Export a .kicad_sch file to a Bill of Materials via "
        "`kicad-cli sch export bom`. "
        "NAMING: Use a descriptive output_path that identifies the "
        "project or subcircuit (e.g. controller_board_bom.csv, not "
        "bom.csv). For multi-schematic projects, include context in the "
        "filename so BOMs are distinguishable. "
        "Supports csv (default), tsv, html, and xml formats; optional "
        "named preset selection for designer-defined field/group-by "
        "layouts; and exclude_dnp for fab-ready output. Supports dry_run."
    )
    input_model = SchExportBomInput
    output_model = SchExportBomOutput
    # MUTATE because we write to the filesystem (ADR-0008). Not DESTRUCTIVE
    # — we never touch the schematic; the worst case is overwriting a prior
    # BOM, which we surface via meta.warnings.
    classification = ToolClass.MUTATE
    mutates = True
    # CLI is the only way to service this. The dispatcher gate rejects the
    # call with BACKEND_UNAVAILABLE when `kicad-cli` isn't installed — the
    # correct signal, since the remedy is "install KiCAD", not "set a flag".
    preferred_backends = (Backend.CLI,)
    required_backends = frozenset({Backend.CLI})

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: SchExportBomInput) -> SchExportBomOutput:
        # 1. Resolve the schematic path + pre-flight checks before we pay
        # for a subprocess. `expanduser` catches `~/…`, `resolve` normalizes
        # both relative and absolute paths.
        sch_path = input.sch_path.expanduser().resolve()

        if not sch_path.exists():
            return SchExportBomOutput(
                status="sch_not_found",
                sch_path=None,
                format=input.format,
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchExportBomOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                format=input.format,
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r}). sch_export_bom runs on a "
                    "schematic file, not a project or board."
                ),
            )

        # 2. Resolve output_path. Default lands next to the schematic so
        # the caller doesn't have to think about layout for the common
        # case. Extension comes from the format → ext mapping.
        if input.output_path is None:
            ext = _FORMAT_TO_EXT.get(input.format, "csv")
            output_path = sch_path.with_name(f"{sch_path.stem}.{ext}")
        else:
            output_path = input.output_path.expanduser().resolve()

        # 3. Build argv before the CLI probe so dry_run can return the
        # planned invocation even when kicad-cli is absent. Optional
        # knobs are appended conditionally — passing an empty preset
        # string to kicad-cli isn't the same as not passing --preset, so
        # the None check matters.
        argv: list[str] = [
            "sch",
            "export",
            "bom",
            "--format-preset",
            input.format,
            "-o",
            str(output_path),
        ]
        if input.preset is not None:
            argv.extend(["--preset", input.preset])
        if input.exclude_dnp:
            argv.append("--exclude-dnp")
        argv.append(str(sch_path))

        # 4. Dry-run short-circuit. Per ADR-0008, every mutating tool
        # must support dry-run — caller gets the resolved paths and
        # planned argv but we don't touch the filesystem (no mkdir, no
        # subprocess). This is the path used by safety prompts in the
        # MCP host before approving the live call.
        if input.dry_run:
            return SchExportBomOutput(
                status="dry_run",
                sch_path=str(sch_path),
                output_path=str(output_path),
                format=input.format,
                cli_argv=argv,
                note=(
                    "dry_run=True; no files were written. Re-run with "
                    "dry_run=False to actually invoke kicad-cli."
                ),
            )

        # 5. Resolve the CLI. The dispatcher already gated on CLI
        # availability (preferred=(CLI,)), but we still need the resolved
        # path — and a safety belt covers the rare race where CLI was up
        # at probe time and gone by the call.
        backend = self._cli_backend
        if backend is None:
            backend = CliBackend()
        await backend.probe()
        cli_path = backend.cli_path
        if cli_path is None:
            return SchExportBomOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                format=input.format,
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        # 6. Ensure parent dir exists + flag pre-existing output_path so we
        # can warn on overwrite. kicad-cli overwrites silently; we preserve
        # that behavior (callers often want "re-export in place") but
        # surface the overwrite explicitly so LLM-driven workflows see it.
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return SchExportBomOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                format=input.format,
                note=f"could not create output directory: {exc}",
            )

        overwrite_warning: str | None = None
        if output_path.exists():
            overwrite_warning = (
                f"output_path {output_path} already existed before the "
                "export and was overwritten. This is fine for a re-export "
                "in place, but pass an empty target if you wanted to keep "
                "the prior file."
            )

        # 7. Invoke kicad-cli.
        try:
            result = await run_cli(
                tuple(argv),
                cli_path=Path(cli_path),
                timeout=_BOM_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return SchExportBomOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                format=input.format,
                cli_argv=argv,
                note=(
                    f"kicad-cli timed out after {exc.timeout:.0f}s — "
                    "the schematic may be unusually deep; re-run with "
                    "the host KiCAD interactively to confirm."
                ),
            )
        except CliError as exc:
            return SchExportBomOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                format=input.format,
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return SchExportBomOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                format=input.format,
                cli_argv=argv,
                note=(
                    f"kicad-cli exited {result.exit_code}: "
                    f"{stderr_excerpt or '<no stderr output>'}"
                ),
            )

        # 8. Confirm the output landed. Single-file export, so we just
        # check existence + size. kicad-cli returning 0 without writing
        # the file is rare (symbol-less schematic, filesystem stat lag),
        # but worth pinning as its own status so callers don't treat it
        # as ok.
        if not output_path.is_file():
            out_empty = SchExportBomOutput(
                status="no_file_produced",
                sch_path=str(sch_path),
                output_path=str(output_path),
                format=input.format,
                cli_argv=argv,
                note=(
                    "kicad-cli exited cleanly but no file appeared at "
                    "output_path. Likely causes: the schematic has no "
                    "symbols, or the filesystem returned a stale listing. "
                    "Confirm the schematic has at least one component."
                ),
            )
            if overwrite_warning is not None:
                out_empty.meta.warnings.append(overwrite_warning)
            return out_empty

        try:
            size = output_path.stat().st_size
        except OSError:
            # Should never happen on a file we just confirmed exists,
            # but a racing rm-rf would surface here. Report 0 so the
            # envelope stays parseable.
            size = 0

        out = SchExportBomOutput(
            status="ok",
            sch_path=str(sch_path),
            output_path=str(output_path),
            size_bytes=size,
            format=input.format,
            cli_argv=argv,
        )
        if overwrite_warning is not None:
            out.meta.warnings.append(overwrite_warning)
        return out


__all__ = [
    "SchExportBomInput",
    "SchExportBomOutput",
    "SchExportBomTool",
]
