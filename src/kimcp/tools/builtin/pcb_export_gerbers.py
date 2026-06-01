"""pcb_export_gerbers — render a .kicad_pcb to fabrication gerbers (M7).

The first *file-producing* tool in KiMCP. Where ``pcb_drc`` returns a
verdict, this one shells out to ``kicad-cli pcb export gerbers`` and
discovers the binary artifacts on disk. The envelope therefore looks
different from the read-only tools: instead of a single status payload
you get a list of :class:`GeneratedFile` entries with file paths, sizes,
and a best-effort ``layer_hint`` parsed from the filename.

Status enum encodes the verdicts a real fabrication workflow needs to
branch on:

* **ok**                — kicad-cli ran cleanly and produced ≥ 1 file.
* **dry_run**           — caller passed ``dry_run=True``; we report the
                          planned argv + resolved output_dir but never
                          invoke the CLI. Per ADR-0008, every mutating
                          tool supports dry-run for predictability.
* **pcb_not_found**     — input path is missing or not a .kicad_pcb.
* **cli_failed**        — kicad-cli didn't run cleanly (timeout, non-zero
                          exit, or the binary disappeared between probe
                          and call). ``note`` carries the reason.
* **no_files_produced** — kicad-cli exited 0 but no new files appeared
                          in ``output_dir``. Defensive — usually means
                          the layer set was empty or the board has no
                          plottable items. We still surface it instead
                          of failing silently.

File discovery is a before/after diff of ``output_dir`` so we don't
depend on kicad-cli's stdout (it doesn't emit a manifest). Pre-existing
files in the dir are preserved; the envelope only reports files that
appeared during this call. A meta.warnings entry fires when the dir
already had files at start, since that's a smell — fab outputs should
land in a clean directory per build to avoid mixing stale layers.

Why MUTATE and not READ: this tool *writes to the filesystem*. Even
though it doesn't touch the PCB itself, the safety model (see
``.claude/skills/kimcp-architecture/safety.md``) classifies any tool
that produces files as MUTATE so the host can prompt before running it
in policy-sensitive contexts. That's also why ``dry_run`` is wired
through here — every MUTATE tool gets it per ADR-0008.
"""

from __future__ import annotations

import logging
import re
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

# Default CLI timeout for gerber export. A dense 6-layer mixed-signal
# board with full silkscreen + paste + soldermask is comfortably under
# 30 s; 180 s gives headroom for unusually large or complex outlines
# (rounded copper pours, courtyards, large keepout regions). Callers
# that know their board is huge can't override today (input surface
# stays minimal for M7); a `timeout_sec` knob arrives when real-world
# boards motivate it — same posture as ``pcb_drc``.
_GERBERS_TIMEOUT_SEC = 180.0

# Canonical 9-layer fabrication set. Default for callers that don't
# specify `layers`. This matches what every reputable PCB fab expects
# in a 2-layer FR-4 order:
#   * F.Cu / B.Cu          — copper
#   * F.Mask / B.Mask      — soldermask
#   * F.SilkS / B.SilkS    — silkscreen
#   * F.Paste / B.Paste    — solder paste stencil
#   * Edge.Cuts            — board outline
# Inner-layer boards just add In1.Cu / In2.Cu / … via the `layers`
# input. Adhesive / fab notes / courtyards stay opt-in via custom
# layer lists since fabs don't need them.
_DEFAULT_LAYERS: tuple[str, ...] = (
    "F.Cu",
    "B.Cu",
    "F.Mask",
    "B.Mask",
    "F.SilkS",
    "B.SilkS",
    "F.Paste",
    "B.Paste",
    "Edge.Cuts",
)

# KiCAD writes gerber filenames as `<board>-<Layer_Name>.<ext>` where the
# layer name's `.` is encoded as `_` (so `F.Cu` becomes `F_Cu`). The
# regex extracts the layer-name segment between the first `-` and the
# last `.`. Best-effort only: files that don't fit the convention (job
# file, drill, future schema changes) get `layer_hint=None`.
_LAYER_FILENAME_RE = re.compile(
    r"""
    ^                       # start
    .+?                     # board name (non-greedy so first `-` wins)
    -                       # separator KiCAD always emits
    (?P<layer>[A-Za-z0-9_]+)# layer name (underscored)
    \.                      # extension separator
    (?:gbr|gbl|gtl|gbo|gto|gbs|gts|gbp|gtp|gm[0-9]+|g[0-9]+|gko)
    $
    """,
    re.VERBOSE,
)


# -- envelope sub-models ---------------------------------------------------


class GeneratedFile(BaseModel):
    """One artifact produced by ``kicad-cli pcb export gerbers``.

    Pass-through plus best-effort enrichment. ``layer_hint`` is parsed
    from the filename rather than interrogated from the PCB to keep this
    tool stateless across kicad-cli versions; clients that need exact
    layer metadata should follow up with a board-read tool.
    """

    model_config = ConfigDict(extra="allow")

    path: str = Field(
        ...,
        description="Absolute, resolved path to the produced gerber file.",
    )
    size_bytes: int = Field(
        ...,
        description="On-disk size in bytes — useful for sanity-checking that "
        "a layer wasn't silently empty.",
    )
    extension: str = Field(
        ...,
        description="File extension lowercased, no leading dot (e.g. 'gbr').",
    )
    layer_hint: str | None = Field(
        default=None,
        description=(
            "Best-effort KiCAD layer name parsed from the filename "
            "(e.g. 'F.Cu', 'Edge.Cuts'). Null when the filename doesn't "
            "match the standard `<board>-<layer>.<ext>` convention "
            "— typical for the gerber job file (.gbrjob)."
        ),
    )


# -- input / output --------------------------------------------------------


class PcbExportGerbersInput(BaseModel):
    pcb_path: Path = Field(
        ...,
        description="Path to the .kicad_pcb file. Relative paths resolve against CWD.",
    )
    output_dir: Path | None = Field(
        default=None,
        description=(
            "Directory to write gerbers into. Defaults to a sibling "
            "'gerbers/' folder next to the .kicad_pcb. Created if it "
            "doesn't exist; pre-existing files are preserved but a "
            "warning fires (fab builds should land in a clean dir)."
        ),
    )
    layers: list[str] | None = Field(
        default=None,
        description=(
            "KiCAD layer names to plot (e.g. ['F.Cu', 'B.Cu', 'Edge.Cuts']). "
            "Null uses the canonical 9-layer fabrication set "
            "(F/B copper, mask, silkscreen, paste, plus Edge.Cuts), which "
            "is what most fab houses expect for a 2-layer order. "
            "Inner-layer boards add In1.Cu / In2.Cu / … explicitly."
        ),
    )
    use_protel_extensions: bool = Field(
        default=True,
        description=(
            "Use legacy Protel extensions (.gtl, .gbl, .gto, …) instead of "
            "uniform .gbr. Most fabs prefer Protel; some modern shops "
            "want .gbr only. Maps to the inverse of kicad-cli's "
            "`--no-protel-ext`."
        ),
    )
    precision: Literal[5, 6] = Field(
        default=6,
        description=(
            "Coordinate precision (digits after the decimal). 6 is the "
            "modern industry default; 5 is for legacy CAM software that "
            "can't parse 6-digit precision."
        ),
    )
    exclude_refdes: bool = Field(
        default=False,
        description="Omit reference designators (R1, U2, …) from silkscreen.",
    )
    exclude_value: bool = Field(
        default=False,
        description="Omit component values (10k, 0.1uF, …) from silkscreen.",
    )
    use_x2_format: bool = Field(
        default=True,
        description=(
            "Emit Gerber X2 attributes (recommended; modern CAM software "
            "uses them for net + pad-function metadata). Disable only "
            "for ancient fab tooling. Maps to the inverse of "
            "kicad-cli's `--no-x2`."
        ),
    )
    embed_netlist: bool = Field(
        default=True,
        description=(
            "Embed the IPC-356 netlist in the gerber job file. Enables "
            "fab-side electrical test programming. Disable to slim "
            "outputs when the fab doesn't run E-test. Maps to the "
            "inverse of kicad-cli's `--no-netlist`."
        ),
    )
    subtract_soldermask: bool = Field(
        default=False,
        description=(
            "Subtract soldermask from silkscreen so silk doesn't print "
            "over openings. Off by default to match kicad-cli; turn on "
            "if your fab requests it."
        ),
    )
    check_zones: bool = Field(
        default=False,
        description=(
            "Refill copper zones before plotting (slower but matches the "
            "'Check Zone Fills' GUI step). Catches the #1 fab-prep bug — "
            "stale zone fills — but adds noticeable latency on dense "
            "ground pours, so it's opt-in."
        ),
    )
    variant: str = Field(
        default="",
        description=(
            "Assembly variant to plot (empty string = default variant). "
            "Useful for boards with DNP variant tables."
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


class PcbExportGerbersOutput(ToolOutput):
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
    generated_files: list[GeneratedFile] = Field(
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


class PcbExportGerbersTool(Tool[PcbExportGerbersInput, PcbExportGerbersOutput]):
    """Render a .kicad_pcb to fabrication gerbers via `kicad-cli pcb export gerbers`."""

    name = "pcb_export_gerbers"
    version = "0.1.0"
    description = (
        "Render a .kicad_pcb to fabrication gerbers (Gerber X2 by default, "
        "Protel extensions, 6-digit precision) using "
        "`kicad-cli pcb export gerbers`. Returns the list of generated "
        "files with sizes and best-effort layer hints. Supports dry_run "
        "to preview the invocation without writing any files."
    )
    input_model = PcbExportGerbersInput
    output_model = PcbExportGerbersOutput
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

    async def run(self, input: PcbExportGerbersInput) -> PcbExportGerbersOutput:
        # 1. Resolve the PCB path + do pre-flight checks before we pay for
        # a subprocess. `expanduser` catches `~/…`, `resolve` normalizes
        # both relative and absolute paths.
        pcb_path = input.pcb_path.expanduser().resolve()

        if not pcb_path.exists():
            return PcbExportGerbersOutput(
                status="pcb_not_found",
                pcb_path=None,
                note=f"no such file: {pcb_path}",
            )
        if pcb_path.suffix.lower() != ".kicad_pcb":
            return PcbExportGerbersOutput(
                status="pcb_not_found",
                pcb_path=str(pcb_path),
                note=(
                    f"not a .kicad_pcb file: {pcb_path} (got suffix "
                    f"{pcb_path.suffix!r}). pcb_export_gerbers runs on a "
                    "board file, not a project or schematic."
                ),
            )

        # 2. Resolve the output directory. Default lands next to the PCB
        # so the caller doesn't have to think about layout for the common
        # case. Custom paths get expanduser + resolve like the input PCB.
        if input.output_dir is None:
            output_dir = pcb_path.parent / "gerbers"
        else:
            output_dir = input.output_dir.expanduser().resolve()

        # 3. Build the argv. We do this before the CLI probe so dry_run
        # can return the planned invocation even when kicad-cli is absent.
        # Layer list defaults to the canonical 9-layer set; ',' join is
        # how kicad-cli accepts the list.
        layers = input.layers if input.layers is not None else list(_DEFAULT_LAYERS)
        argv: list[str] = [
            "pcb",
            "export",
            "gerbers",
            "--output",
            str(output_dir),
            "--layers",
            ",".join(layers),
            "--precision",
            str(input.precision),
        ]
        # Map the user-friendly inverted booleans back to kicad-cli's
        # flag surface. We surface the positive-sense flag (e.g.
        # `use_protel_extensions=True`) because that matches how a user
        # thinks about the option; kicad-cli phrases each as a "no-"
        # flag for backward-compat reasons.
        if not input.use_protel_extensions:
            argv.append("--no-protel-ext")
        if not input.use_x2_format:
            argv.append("--no-x2")
        if not input.embed_netlist:
            argv.append("--no-netlist")
        if input.exclude_refdes:
            argv.append("--exclude-refdes")
        if input.exclude_value:
            argv.append("--exclude-value")
        if input.subtract_soldermask:
            argv.append("--subtract-soldermask")
        if input.check_zones:
            argv.append("--check-zones")
        if input.variant:
            argv.extend(["--variant", input.variant])
        argv.append(str(pcb_path))

        # 4. Dry-run short-circuit. Per ADR-0008, every mutating tool
        # must support dry-run — caller gets the resolved paths and
        # planned argv but we don't touch the filesystem (no mkdir, no
        # subprocess). This is the path used by safety prompts in the
        # MCP host before approving the live call.
        if input.dry_run:
            return PcbExportGerbersOutput(
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
            return PcbExportGerbersOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                note=(
                    "kicad-cli not found on PATH or at the configured path. "
                    "Install KiCAD or set `kicad.cli_exe` in your config."
                ),
            )

        # 6. Create the output directory + snapshot pre-existing files so
        # we can later diff for newly-produced gerbers. mkdir is safe to
        # call against an existing directory thanks to exist_ok.
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return PcbExportGerbersOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                note=f"could not create output directory: {exc}",
            )

        pre_existing = _scan_dir(output_dir)
        dirty_dir_warning: str | None = None
        if pre_existing:
            # Loud signal — a clean per-build dir is the only way to know
            # for sure that the gerbers in there match the .kicad_pcb hash
            # the caller just plotted. We don't fail (caller may have
            # deliberately picked a shared dir), but we do warn.
            dirty_dir_warning = (
                f"output_dir already contained {len(pre_existing)} file(s) "
                "before the export. Generated_files only lists artifacts "
                "newly created by this call; pre-existing files were left "
                "alone. Consider using a clean per-build directory to "
                "avoid mixing stale layer outputs."
            )

        # 7. Invoke kicad-cli.
        try:
            result = await run_cli(
                tuple(argv),
                cli_path=Path(cli_path),
                timeout=_GERBERS_TIMEOUT_SEC,
                check=False,
            )
        except CliTimeoutError as exc:
            return PcbExportGerbersOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=(
                    f"kicad-cli timed out after {exc.timeout:.0f}s — "
                    "the board may be unusually large or zone-fill is "
                    "stalling; re-run with the host KiCAD interactively "
                    "to confirm."
                ),
            )
        except CliError as exc:
            return PcbExportGerbersOutput(
                status="cli_failed",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=f"kicad-cli failed: {exc}",
            )

        if result.exit_code != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500]
            return PcbExportGerbersOutput(
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
            out_empty = PcbExportGerbersOutput(
                status="no_files_produced",
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                cli_argv=argv,
                note=(
                    "kicad-cli exited cleanly but no new files appeared in "
                    "output_dir. Likely causes: empty layer list, board has "
                    "no plottable items on the requested layers, or the "
                    "filesystem returned a stale listing. Re-check the "
                    "`layers` input."
                ),
            )
            if dirty_dir_warning is not None:
                out_empty.meta.warnings.append(dirty_dir_warning)
            return out_empty

        generated = [_describe_file(p) for p in new_files]
        out = PcbExportGerbersOutput(
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

    Non-recursive on purpose — kicad-cli writes gerbers as siblings, not
    nested directories. Resolves each entry so the set diff against a
    later scan is symlink-stable. Returns an empty set if the directory
    doesn't exist (caller may invoke this before mkdir, or against a
    path the user typo'd).
    """
    if not d.exists():
        return set()
    return {p.resolve() for p in d.iterdir() if p.is_file()}


def _describe_file(path: Path) -> GeneratedFile:
    """Build a :class:`GeneratedFile` for one produced artifact.

    Layer hint is parsed from the filename via :data:`_LAYER_FILENAME_RE`.
    Files that don't match (typically the gerber job file `.gbrjob`) get
    ``layer_hint=None`` — the field is informational, not load-bearing.
    """
    try:
        size = path.stat().st_size
    except OSError:
        # Should never happen on a path we just diff-discovered, but a
        # racing rm-rf would surface here. Surface 0 so the envelope
        # stays parseable rather than throwing mid-discovery.
        size = 0

    extension = path.suffix.lstrip(".").lower()
    layer_hint: str | None = None
    match = _LAYER_FILENAME_RE.match(path.name)
    if match is not None:
        # KiCAD encodes `.` as `_` in filenames (F.Cu → F_Cu); reverse it.
        layer_hint = match.group("layer").replace("_", ".")

    return GeneratedFile(
        path=str(path),
        size_bytes=size,
        extension=extension,
        layer_hint=layer_hint,
    )


__all__ = [
    "GeneratedFile",
    "PcbExportGerbersInput",
    "PcbExportGerbersOutput",
    "PcbExportGerbersTool",
]
