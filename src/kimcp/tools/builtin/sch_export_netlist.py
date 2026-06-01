"""sch_export_netlist — export a .kicad_sch to a netlist file (M10).

The first Thread B export tool and the bridge between the schematic and
the PCB: a netlist is the artifact pcbnew reads when you run "Update PCB
from schematic", so this is the single most important schematic output
for downstream design flow. It's also the primary feed for SPICE
simulation and third-party BOM/import tools.

Shells out to ``kicad-cli sch export netlist --format <format> -o <path>``
and reports the single produced file. Unlike :mod:`pcb_export_drill` /
:mod:`pcb_export_gerbers` — which produce a directory of artifacts and
use a before/after dir-diff — a netlist export is strictly one file, so
discovery collapses to "does the output_path exist after the call?". We
keep the dry-run / overwrite-warning posture from the sibling tools.

Eight output formats, covering PCB-import, simulation, and legacy CAD
interop:

* **kicadsexpr**  — KiCAD native S-expression netlist (.net). Default.
                    This is what pcbnew reads during "Update PCB from
                    schematic".
* **kicadxml**    — KiCAD XML netlist (.xml). Consumable by BOM
                    generators and third-party tools.
* **cadstar**     — Cadstar-compatible netlist.
* **orcadpcb2**   — OrCAD PCB II (legacy; small, human-readable).
* **spice**       — SPICE circuit netlist for simulation (.cir).
* **spicemodel**  — SPICE subcircuit model library (.lib).
* **pads**        — PADS PowerPCB netlist.
* **allegro**     — Cadence Allegro netlist.

Status enum mirrors the other export tools so callers can share a
single dispatch branch across sch_export_netlist / pcb_export_gerbers /
pcb_export_drill:

* **ok**               — kicad-cli ran cleanly and wrote the netlist.
* **dry_run**          — caller passed ``dry_run=True``; returns the
                         planned argv + resolved output_path without
                         invoking the CLI. Per ADR-0008.
* **sch_not_found**    — input path is missing or not a .kicad_sch.
* **cli_failed**       — kicad-cli didn't run cleanly (timeout, non-zero
                         exit, or the binary disappeared between probe
                         and call). ``note`` carries the reason.
* **no_file_produced** — kicad-cli exited 0 but no file appeared at
                         output_path. Defensive; usually means the
                         filesystem returned a stale listing or the
                         schematic was empty.

Why MUTATE and not READ: this tool writes to the filesystem, same
reasoning as ``pcb_export_drill`` — even though we don't mutate the
schematic itself, the safety model classifies any tool that produces
files as MUTATE so hosts can prompt before running it in
policy-sensitive contexts. ``dry_run`` is wired through per ADR-0008.
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

# Default CLI timeout for netlist export. Netlist generation is the
# cheapest schematic operation kicad-cli exposes — no geometry, no
# rendering, just a traversal of the connectivity graph. Even a deep
# hierarchical schematic finishes well under 30 s. 120 s matches the
# sibling export tools; a ``timeout_sec`` knob can arrive if real
# workflows motivate it.
_NETLIST_TIMEOUT_SEC = 120.0

# Extensions KiCAD historically associates with each netlist format.
# Used only when the caller doesn't pass ``output_path`` — if they do,
# we honor their explicit choice. kicad-cli doesn't enforce an extension
# when ``-o`` is given; this mapping is a convenience for the common
# "just export a netlist next to the schematic" case.
_FORMAT_TO_EXT: dict[str, str] = {
    "kicadsexpr": "net",
    "kicadxml": "xml",
    "cadstar": "frp",
    "orcadpcb2": "net",
    "spice": "cir",
    "spicemodel": "lib",
    "pads": "asc",
    "allegro": "net",
}


# -- input / output --------------------------------------------------------


class SchExportNetlistInput(BaseModel):
    sch_path: Path = Field(
        ...,
        description="Path to the .kicad_sch file. Relative paths resolve against CWD.",
    )
    output_path: Path | None = Field(
        default=None,
        description=(
            "Destination file for the netlist. Defaults to "
            "`<sch_stem>.<ext>` next to the schematic, where `<ext>` is "
            "derived from `format` (e.g. 'net' for kicadsexpr, 'xml' for "
            "kicadxml). Parent directory is created if missing. If a file "
            "already exists at this path, it will be overwritten and a "
            "warning is surfaced via meta.warnings."
        ),
    )
    format: Literal[
        "kicadsexpr",
        "kicadxml",
        "cadstar",
        "orcadpcb2",
        "spice",
        "spicemodel",
        "pads",
        "allegro",
    ] = Field(
        default="kicadsexpr",
        description=(
            "Netlist format. 'kicadsexpr' is the native KiCAD format and "
            "the one pcbnew reads during 'Update PCB from schematic' — the "
            "right default for schematic→PCB flow. Other formats exist for "
            "simulation ('spice', 'spicemodel'), BOM tooling ('kicadxml'), "
            "and legacy CAD interop ('cadstar', 'orcadpcb2', 'pads', "
            "'allegro')."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If True, validate inputs and report the planned argv + "
            "resolved output_path without invoking kicad-cli. Per ADR-0008, "
            "every mutating tool supports dry-run."
        ),
    )


class SchExportNetlistOutput(ToolOutput):
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
            "Resolved absolute path the netlist was (or would be) written "
            "to. Populated whenever we got far enough to compute it."
        ),
    )
    size_bytes: int = Field(
        default=0,
        description=(
            "On-disk size of the produced netlist in bytes. Zero for "
            "non-ok statuses. Useful for sanity-checking that the file "
            "wasn't silently empty."
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


class SchExportNetlistTool(Tool[SchExportNetlistInput, SchExportNetlistOutput]):
    """Export a .kicad_sch to a netlist via `kicad-cli sch export netlist`."""

    name = "sch_export_netlist"
    version = "0.1.0"
    description = (
        "Export a .kicad_sch file to a netlist via `kicad-cli sch export "
        "netlist`. "
        "NAMING: When output_path is omitted, the netlist is named after "
        "the schematic stem (e.g. dcdc.kicad_sch → dcdc.net). For projects "
        "with a single top-level schematic, use the project name as the "
        "netlist name (e.g. controller_board.net). For multi-schematic "
        "projects or subcircuit exports, use a descriptive name that "
        "captures the circuit's function (e.g. 32v_12v_buck_converter.net, "
        "not dcdc.net). Pass output_path explicitly to control naming. "
        "Supports eight formats covering PCB import "
        "(kicadsexpr — default), BOM tooling (kicadxml), SPICE simulation "
        "(spice, spicemodel), and legacy CAD interop (cadstar, orcadpcb2, "
        "pads, allegro). Supports dry_run."
    )
    input_model = SchExportNetlistInput
    output_model = SchExportNetlistOutput
    # MUTATE because we write to the filesystem (ADR-0008). Not DESTRUCTIVE
    # — we never delete or overwrite the schematic; the worst case is
    # overwriting a prior netlist, which we surface via meta.warnings.
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

    async def run(self, input: SchExportNetlistInput) -> SchExportNetlistOutput:
        # 1. Resolve the schematic path + pre-flight checks before we pay
        # for a subprocess. `expanduser` catches `~/…`, `resolve` normalizes
        # both relative and absolute paths.
        sch_path = input.sch_path.expanduser().resolve()

        if not sch_path.exists():
            return SchExportNetlistOutput(
                status="sch_not_found",
                sch_path=None,
                format=input.format,
                note=f"no such file: {sch_path}",
            )
        if sch_path.suffix.lower() != ".kicad_sch":
            return SchExportNetlistOutput(
                status="sch_not_found",
                sch_path=str(sch_path),
                format=input.format,
                note=(
                    f"not a .kicad_sch file: {sch_path} (got suffix "
                    f"{sch_path.suffix!r}). sch_export_netlist runs on a "
                    "schematic file, not a project or board."
                ),
            )

        # 2. Resolve output_path. Default lands next to the schematic so
        # the caller doesn't have to think about layout for the common
        # case. The extension comes from the format → ext mapping; a
        # format we don't know an ext for (shouldn't happen thanks to
        # the Literal constraint, but defensive) falls back to 'net'.
        if input.output_path is None:
            ext = _FORMAT_TO_EXT.get(input.format, "net")
            output_path = sch_path.with_name(f"{sch_path.stem}.{ext}")
        else:
            output_path = input.output_path.expanduser().resolve()

        # 3. Build argv before the CLI probe so dry_run can return the
        # planned invocation even when kicad-cli is absent.
        argv: list[str] = [
            "sch",
            "export",
            "netlist",
            "--format",
            input.format,
            "-o",
            str(output_path),
            str(sch_path),
        ]

        # 4. Dry-run short-circuit. Per ADR-0008, every mutating tool
        # must support dry-run — caller gets the resolved paths and
        # planned argv but we don't touch the filesystem (no mkdir, no
        # subprocess). This is the path used by safety prompts in the
        # MCP host before approving the live call.
        if input.dry_run:
            return SchExportNetlistOutput(
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
            return SchExportNetlistOutput(
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
            return SchExportNetlistOutput(
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
                timeout=_NETLIST_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return SchExportNetlistOutput(
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
            return SchExportNetlistOutput(
                status="cli_failed",
                sch_path=str(sch_path),
                output_path=str(output_path),
                format=input.format,
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return SchExportNetlistOutput(
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
        # the file is rare (empty schematic, filesystem stat lag), but
        # worth pinning as its own status so callers don't treat it as ok.
        if not output_path.is_file():
            out_empty = SchExportNetlistOutput(
                status="no_file_produced",
                sch_path=str(sch_path),
                output_path=str(output_path),
                format=input.format,
                cli_argv=argv,
                note=(
                    "kicad-cli exited cleanly but no file appeared at "
                    "output_path. Likely causes: the schematic is empty, "
                    "or the filesystem returned a stale listing. Confirm "
                    "the schematic has at least one component + net."
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

        out = SchExportNetlistOutput(
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
    "SchExportNetlistInput",
    "SchExportNetlistOutput",
    "SchExportNetlistTool",
]
