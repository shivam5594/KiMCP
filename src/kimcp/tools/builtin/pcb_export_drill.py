"""pcb_export_drill — render a .kicad_pcb to fabrication drill files (M8).

Sibling to :mod:`pcb_export_gerbers`: where that tool renders copper /
mask / silk / paste layers, this one shells out to ``kicad-cli pcb export
drill`` to produce the Excellon (or Gerber-drill) files a fab uses to
drive the drilling and routing machines. Together the two tools cover
the minimum output set for a turnkey fab order — most fab upload forms
expect both or they reject the job.

Status enum mirrors :mod:`pcb_export_gerbers` so callers can share a
single dispatch branch across the two export tools:

* **ok**                — kicad-cli ran cleanly and produced ≥ 1 file.
* **dry_run**           — caller passed ``dry_run=True``; we report the
                          planned argv + resolved output_dir without
                          invoking the CLI. Per ADR-0008, every mutating
                          tool supports dry-run.
* **pcb_not_found**     — input path is missing or not a .kicad_pcb.
* **cli_failed**        — kicad-cli didn't run cleanly (timeout, non-zero
                          exit, or the binary disappeared between probe
                          and call). ``note`` carries the reason.
* **no_files_produced** — kicad-cli exited 0 but no new files appeared
                          in ``output_dir``. Defensive — usually means
                          the board has no drilled holes (unusual but
                          possible for an experimental flex-PCB outline)
                          or the filesystem returned a stale listing.

File discovery uses the same before/after dir-diff as :mod:`pcb_export_gerbers`;
the envelope only reports files that appeared during this call. A
meta.warnings entry fires when the dir already had files at start, since
mixing stale drill files with a fresh export is a fab-upload footgun.

Why MUTATE and not READ: this tool writes to the filesystem. Even
though it doesn't touch the PCB itself, the safety model (see
``.claude/skills/kimcp-architecture/safety.md``) classifies any tool
that produces files as MUTATE so the host can prompt before running it
in policy-sensitive contexts. That's also why ``dry_run`` is wired
through — every MUTATE tool gets it per ADR-0008.
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

# Default CLI timeout for drill export. Drill generation is strictly
# faster than gerbers (fewer layer passes, no silkscreen rendering); a
# dense 6-layer board with thousands of vias finishes well under 30 s.
# 120 s gives headroom for the odd large panel or a slow filesystem.
# A ``timeout_sec`` knob can be added when real-world boards motivate
# it — same posture as ``pcb_drc`` and ``pcb_export_gerbers``.
_DRILL_TIMEOUT_SEC = 120.0


# -- envelope sub-models ---------------------------------------------------


class DrillFile(BaseModel):
    """One artifact produced by ``kicad-cli pcb export drill``.

    ``kind`` is best-effort from the filename (kicad-cli doesn't emit a
    manifest). Recognised patterns:

    * ``<board>-PTH.drl`` / ``-PTH.gbr``  → ``"pth"``
    * ``<board>-NPTH.drl`` / ``-NPTH.gbr`` → ``"npth"``
    * ``<board>-drl_map.<ext>``            → ``"map"``
    * plain ``<board>.drl``                → ``"combined"``

    Anything else reports ``kind=None`` — the field is informational,
    not load-bearing, so a schema-drift in kicad-cli's naming surfaces
    as ``None`` rather than a tool crash.
    """

    model_config = ConfigDict(extra="allow")

    path: str = Field(
        ...,
        description="Absolute, resolved path to the produced drill file.",
    )
    size_bytes: int = Field(
        ...,
        description="On-disk size in bytes — useful for sanity-checking that "
        "a file wasn't silently empty.",
    )
    extension: str = Field(
        ...,
        description=(
            "File extension lowercased, no leading dot. Typical values: "
            "'drl' (Excellon), 'gbr' (Gerber drill or drill-map), "
            "'pdf' / 'ps' / 'dxf' / 'svg' (drill map in various formats)."
        ),
    )
    kind: Literal["combined", "pth", "npth", "map"] | None = Field(
        default=None,
        description=(
            "Best-effort classifier parsed from the filename. 'combined' is "
            "the single PTH+NPTH Excellon file kicad-cli emits by default. "
            "'pth' / 'npth' appear when excellon_separate_th=True. 'map' is "
            "the optional drill map. Null when the filename doesn't match "
            "any known convention."
        ),
    )


# -- input / output --------------------------------------------------------


class PcbExportDrillInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    output_dir: Path | None = Field(
        default=None,
        description=(
            "Directory to write drill files into. Defaults to a sibling "
            "'drill/' folder next to the .kicad_pcb. Created if it doesn't "
            "exist; pre-existing files are preserved but a warning fires "
            "(fab builds should land in a clean dir)."
        ),
    )
    drill_format: Literal["excellon", "gerber"] = Field(
        default="excellon",
        description=(
            "Drill file format. 'excellon' is the industry-standard RS-274-X "
            "(.drl) every fab accepts and is what kicad-cli defaults to. "
            "'gerber' emits Gerber-X2-encoded drill files for the rare fab "
            "that prefers a unified Gerber bundle."
        ),
    )
    drill_origin: Literal["absolute", "plot"] = Field(
        default="absolute",
        description=(
            "Coordinate origin. 'absolute' uses the page origin (what most "
            "fabs expect and kicad-cli defaults to). 'plot' uses the "
            "auxiliary/drill origin set in pcbnew — only pick this if your "
            "fab has specifically requested it."
        ),
    )
    excellon_units: Literal["in", "mm"] = Field(
        default="in",
        description=(
            "Units for Excellon coordinates. 'in' matches kicad-cli's "
            "default and the historical Excellon 2 convention; 'mm' is "
            "accepted by most modern fabs and mandatory for some "
            "IPC-2581-oriented workflows. Ignored when drill_format='gerber'."
        ),
    )
    excellon_zeros_format: Literal[
        "decimal", "suppressleading", "suppresstrailing", "keep"
    ] = Field(
        default="decimal",
        description=(
            "Numeric encoding in the Excellon file. 'decimal' emits an "
            "explicit decimal point (unambiguous; kicad-cli default). "
            "Zero-suppression variants exist for legacy CAM software that "
            "can't parse decimals — only pick them if your fab has asked. "
            "Ignored when drill_format='gerber'."
        ),
    )
    excellon_oval_format: Literal["route", "alternate"] = Field(
        default="alternate",
        description=(
            "How to represent oval (slot) holes. 'alternate' uses the G85 "
            "slot command (kicad-cli default, widely supported). 'route' "
            "emits a routing path — only needed for drill machines that "
            "can't interpret G85. Ignored when drill_format='gerber'."
        ),
    )
    excellon_mirror_y: bool = Field(
        default=False,
        description=(
            "Mirror the Y axis in the Excellon file. Some fabs with legacy "
            "machines request this; most don't. Ignored when "
            "drill_format='gerber'."
        ),
    )
    excellon_min_header: bool = Field(
        default=False,
        description=(
            "Emit a minimal Excellon header (no comments or tool-list "
            "annotations). Useful when a strict CAM parser rejects the "
            "extended header kicad-cli writes by default. Ignored when "
            "drill_format='gerber'."
        ),
    )
    excellon_separate_th: bool = Field(
        default=False,
        description=(
            "Emit two Excellon files — one for plated through-holes (PTH) "
            "and one for non-plated (NPTH) — instead of a single combined "
            "file. Some fabs require the split; most accept the combined "
            "form. Ignored when drill_format='gerber'."
        ),
    )
    generate_map: bool = Field(
        default=False,
        description=(
            "Also produce a drill map file (human-readable layout of every "
            "drill hit with its tool number). Off by default since most "
            "fabs don't use it; enable for internal documentation or when "
            "a fab requests a map."
        ),
    )
    map_format: Literal["pdf", "gerberx2", "ps", "dxf", "svg"] = Field(
        default="pdf",
        description=(
            "Drill-map output format. 'pdf' is the human-readable default. "
            "'gerberx2' lands next to the other gerbers in the fab bundle. "
            "'ps' / 'dxf' / 'svg' cover legacy workflows. Only meaningful "
            "when generate_map=True."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If True, validate inputs and report the planned argv + resolved "
            "output_dir without invoking kicad-cli. Per ADR-0008, every "
            "mutating tool supports dry-run."
        ),
    )


class PcbExportDrillOutput(ToolOutput):
    status: Literal[
        "ok",
        "dry_run",
        "pcb_not_found",
        "cli_failed",
        "no_files_produced",
    ]
    pcb_path: str | None = Field(
        default=None,
        description="Resolved absolute path to the .kicad_pcb. Null only when "
        "the file couldn't be located at all.",
    )
    output_dir: str | None = Field(
        default=None,
        description="Resolved absolute path to the output directory. Populated "
        "whenever we got far enough to compute it.",
    )
    generated_files: list[DrillFile] = Field(
        default_factory=list,
        description=(
            "Files that appeared in output_dir during this call, sorted by "
            "filename for determinism. Pre-existing files are NOT included."
        ),
    )
    total_files: int = Field(
        default=0,
        description="Length of generated_files. Mirrored at the top level so "
        "callers don't have to len() the list.",
    )
    total_bytes: int = Field(
        default=0,
        description="Sum of size_bytes across generated_files. Useful for "
        "build-output telemetry.",
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


class PcbExportDrillTool(Tool[PcbExportDrillInput, PcbExportDrillOutput]):
    """Render a .kicad_pcb to fabrication drill files via `kicad-cli pcb export drill`."""

    name = "pcb_export_drill"
    version = "0.1.0"
    description = (
        "Render a .kicad_pcb to fabrication drill files (Excellon by default, "
        "inches, decimal zeros, absolute origin) using "
        "`kicad-cli pcb export drill`. Returns the list of generated files "
        "with sizes and best-effort kind hints (pth/npth/map/combined). "
        "Supports dry_run to preview the invocation without writing any files."
    )
    input_model = PcbExportDrillInput
    output_model = PcbExportDrillOutput
    # MUTATE because we write to the filesystem (ADR-0008). Not DESTRUCTIVE
    # — we never delete or overwrite the source PCB; the worst case is
    # adding files to a directory the caller chose.
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

    async def run(self, input: PcbExportDrillInput) -> PcbExportDrillOutput:
        # 1. Resolve the PCB path + do pre-flight checks before we pay for
        # a subprocess. `expanduser` catches `~/…`, `resolve` normalizes
        # both relative and absolute paths.
        pcb_path = input.pcb_path.expanduser().resolve()

        if not pcb_path.exists():
            return PcbExportDrillOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbExportDrillOutput(
                status="pcb_not_found",
                pcb_path=str(pcb_path),
                note=(
                    f"not a .kicad_pcb file: {pcb_path} (got suffix "
                    f"{pcb_path.suffix!r}). pcb_export_drill runs on a "
                    "board file, not a project or schematic."
                ),
            )

        # 2. Resolve the output directory. Default lands next to the PCB
        # so the caller doesn't have to think about layout for the common
        # case. Custom paths get expanduser + resolve like the input PCB.
        if input.output_dir is None:
            output_dir = pcb_path.parent / "drill"
        else:
            output_dir = input.output_dir.expanduser().resolve()

        # 3. Build the argv. We do this before the CLI probe so dry_run
        # can return the planned invocation even when kicad-cli is absent.
        # We pass every flag kicad-cli accepts — for the ``gerber`` format
        # the excellon_* flags are harmlessly ignored, matching kicad-cli's
        # own behavior. This keeps the argv-building branch count low.
        argv: list[str] = [
            "pcb",
            "export",
            "drill",
            "--output",
            str(output_dir),
            "--format",
            input.drill_format,
            "--drill-origin",
            input.drill_origin,
            "--excellon-units",
            input.excellon_units,
            "--excellon-zeros-format",
            input.excellon_zeros_format,
            "--excellon-oval-format",
            input.excellon_oval_format,
        ]
        if input.excellon_mirror_y:
            argv.append("--excellon-mirror-y")
        if input.excellon_min_header:
            argv.append("--excellon-min-header")
        if input.excellon_separate_th:
            argv.append("--excellon-separate-th")
        if input.generate_map:
            argv.extend(["--generate-map", "--map-format", input.map_format])
        argv.append(str(pcb_path))

        # 4. Dry-run short-circuit. Per ADR-0008, every mutating tool
        # must support dry-run — caller gets the resolved paths and
        # planned argv but we don't touch the filesystem (no mkdir, no
        # subprocess). This is the path used by safety prompts in the
        # MCP host before approving the live call.
        if input.dry_run:
            return PcbExportDrillOutput(
                status="dry_run",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
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
            return PcbExportDrillOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        # 6. Create the output directory + snapshot pre-existing files so
        # we can later diff for newly-produced drill files. mkdir is safe
        # to call against an existing directory thanks to exist_ok.
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return PcbExportDrillOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                note=f"could not create output directory: {exc}",
            )

        pre_existing = _scan_dir(output_dir)
        dirty_dir_warning: str | None = None
        if pre_existing:
            # Loud signal — a clean per-build dir is the only way to know
            # for sure that the drill files in there match the .kicad_pcb
            # the caller just plotted. We don't fail (caller may have
            # deliberately picked a shared dir), but we do warn.
            dirty_dir_warning = (
                f"output_dir already contained {len(pre_existing)} file(s) "
                "before the export. Generated_files only lists artifacts "
                "newly created by this call; pre-existing files were left "
                "alone. Consider using a clean per-build directory to "
                "avoid mixing stale drill outputs."
            )

        # 7. Invoke kicad-cli.
        try:
            result = await run_cli(
                tuple(argv),
                cli_path=Path(cli_path),
                timeout=_DRILL_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return PcbExportDrillOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=(
                    f"kicad-cli timed out after {exc.timeout:.0f}s — "
                    "the board may have an unusually large drill count; "
                    "re-run with the host KiCAD interactively to confirm."
                ),
            )
        except CliError as exc:
            return PcbExportDrillOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return PcbExportDrillOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=(
                    f"kicad-cli exited {result.exit_code}: "
                    f"{stderr_excerpt or '<no stderr output>'}"
                ),
            )

        # 8. Discover newly-created files. Set diff against the pre-snapshot
        # gives us only the artifacts this call produced.
        post_existing = _scan_dir(output_dir)
        new_files = sorted(post_existing - pre_existing, key=lambda p: p.name)

        if not new_files:
            out_empty = PcbExportDrillOutput(
                status="no_files_produced",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=(
                    "kicad-cli exited cleanly but no new files appeared in "
                    "output_dir. Likely causes: the board has no drilled "
                    "holes, or the filesystem returned a stale listing. "
                    "Confirm the board has vias / PTH pads."
                ),
            )
            if dirty_dir_warning is not None:
                out_empty.meta.warnings.append(dirty_dir_warning)
            return out_empty

        generated = [_describe_file(p) for p in new_files]
        out = PcbExportDrillOutput(
            status="ok",
            pcb_path=str(pcb_path),
            output_dir=str(output_dir),
            generated_files=generated,
            total_files=len(generated),
            total_bytes=sum(f.size_bytes for f in generated),
            cli_argv=argv,
        )
        if dirty_dir_warning is not None:
            out.meta.warnings.append(dirty_dir_warning)
        return out


# -- discovery helpers (module-level for testability) ----------------------


def _scan_dir(d: Path) -> set[Path]:
    """Return the set of regular files directly under ``d``.

    Non-recursive on purpose — kicad-cli writes drill files as siblings,
    not nested directories. Resolves each entry so the set diff against
    a later scan is symlink-stable. Returns an empty set if the directory
    doesn't exist (caller may invoke this before mkdir, or against a
    path the user typo'd).
    """
    if not d.exists():
        return set()
    return {p.resolve() for p in d.iterdir() if p.is_file()}


def _describe_file(path: Path) -> DrillFile:
    """Build a :class:`DrillFile` for one produced artifact.

    ``kind`` is inferred procedurally rather than via regex — the filename
    patterns are stable enough (kicad-cli hasn't renamed drill output in
    years) that the branching is cheap, and an explicit ladder reads
    better than a multi-group regex that another contributor has to
    unpack. An unrecognised filename reports ``kind=None``.
    """
    try:
        size = path.stat().st_size
    except OSError:
        # Should never happen on a path we just diff-discovered, but a
        # racing rm-rf would surface here. Surface 0 so the envelope
        # stays parseable rather than throwing mid-discovery.
        size = 0

    extension = path.suffix.lstrip(".").lower()
    name = path.name
    kind: Literal["combined", "pth", "npth", "map"] | None = None
    if name.endswith("-PTH.drl") or name.endswith("-PTH.gbr"):
        kind = "pth"
    elif name.endswith("-NPTH.drl") or name.endswith("-NPTH.gbr"):
        kind = "npth"
    elif "drl_map" in name:
        kind = "map"
    elif extension == "drl":
        # Plain <board>.drl — combined PTH+NPTH (kicad-cli default).
        kind = "combined"

    return DrillFile(
        path=str(path),
        size_bytes=size,
        extension=extension,
        kind=kind,
    )


__all__ = [
    "DrillFile",
    "PcbExportDrillInput",
    "PcbExportDrillOutput",
    "PcbExportDrillTool",
]
