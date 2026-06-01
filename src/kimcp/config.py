"""Config loader per `.claude/skills/kimcp-architecture/configuration.md`.

Merge order (later overrides earlier):
  1. built-in defaults (in-code; this module's Pydantic default_factories)
  2. user-global:    ~/.config/kimcp/config.toml (platformdirs-aware)
  3. project-local:  <project_root>/.kimcp/config.toml
  4. session overrides (passed into `load_config`)
  5. per-call overrides (merged inside tool dispatch, not here)

All fields are validated by Pydantic v2. Unknown top-level keys are logged and
dropped (forward/backward migration tolerance per `configuration.md`).
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kimcp._types import Backend

log = logging.getLogger(__name__)

CONFIG_VERSION = 1


class ServerCfg(BaseModel):
    """Transport + auth knobs.

    ``transport`` is consulted by ``__main__`` to pick the I/O layer.
    ``host`` / ``port`` / ``auth_mode`` are forward-compat placeholders —
    HTTP transport is sketched in ``transport.md`` but lives behind a
    future milestone. They surface in config today so operators can
    write a full config.toml that validates across the transport switch
    without schema churn.
    """

    model_config = ConfigDict(extra="ignore")

    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8787
    auth_mode: Literal["none", "token", "oidc", "mtls"] = "none"


class KiCadCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kicad_exe: str = "auto"
    cli_exe: str = "auto"
    # IPC-first order per ADR-0014.
    preferred_backend_order: list[Backend] = Field(
        default_factory=lambda: [Backend.IPC, Backend.SEXPR, Backend.CLI, Backend.SWIG]
    )
    ipc_socket: str = "auto"
    # Minimum supported KiCAD version per ADR-0014.
    min_version: str = "9.0.0"

    @field_validator("min_version")
    @classmethod
    def _validate_min_version(cls, v: str) -> str:
        """Reject semver-shaped strings that don't parse.

        We deliberately keep the storage type `str` (so the TOML surface stays
        plain) but validate at load time — catching a typo in ``config.toml``
        as a clean Pydantic ValidationError rather than a cryptic crash inside
        CliBackend / IpcBackend's constructor downstream.
        """
        # Local import avoids a cycle: kimcp.cli.version has no dep on config,
        # but importing at module top would still pull the parser on every
        # config load even when min_version is never touched.
        from kimcp.cli.version import KiCadVersion

        if KiCadVersion.parse(v) is None:
            raise ValueError(f"kicad.min_version must be a semver-like 'N.N.N' string, got {v!r}")
        return v

    @field_validator("ipc_socket")
    @classmethod
    def _validate_ipc_socket(cls, v: str) -> str:
        """Reject obviously malformed ``ipc_socket`` values at load time.

        ``"auto"`` and the empty string pass through (treated as discovery).
        Anything else must look like a socket path or a Windows named pipe
        (``\\\\.\\pipe\\...``). We can't check reachability here — the socket
        only exists when KiCAD is running — but we can catch typos like a
        bare ``"kicad"`` string or whitespace.
        """
        if v in ("", "auto"):
            return v
        stripped = v.strip()
        if stripped != v or not stripped:
            raise ValueError(
                f"kicad.ipc_socket must not have leading/trailing whitespace, got {v!r}"
            )
        looks_like_pipe = stripped.startswith(r"\\.\pipe")
        looks_like_path = "/" in stripped or stripped.startswith("~")
        if not (looks_like_pipe or looks_like_path):
            raise ValueError(
                f"kicad.ipc_socket must be 'auto', an absolute path, '~'-prefixed path, "
                f"or a Windows named pipe (\\\\.\\pipe\\...); got {v!r}"
            )
        return v


class LibrariesCfg(BaseModel):
    """Symbol / footprint library lookup configuration.

    Forward-compat placeholders. Today ``lib_list_symbols`` and
    ``lib_search_symbol`` take explicit file/dir inputs — they don't
    consult the lib-table chain. A future ``lib_resolver`` milestone
    will read these to walk ``sym-lib-table``/``fp-lib-table`` (global,
    then project-local) and honour KiCAD path vars (``KIPRJMOD``,
    ``KICAD_3DMODEL_DIR``). The knobs exist in config now so operators
    don't hit schema churn when that lands.
    """

    model_config = ConfigDict(extra="ignore")

    symbol_lib_tables: list[str] = Field(default_factory=lambda: ["global", "project"])
    footprint_lib_tables: list[str] = Field(default_factory=lambda: ["global", "project"])
    path_vars: dict[str, str] = Field(
        default_factory=lambda: {"KIPRJMOD": "<auto>", "KICAD_3DMODEL_DIR": "<auto>"}
    )


class DomainKnowledgeCfg(BaseModel):
    """Domain-knowledge (Thread C) engine controls.

    All fields here are **forward-compat placeholders**. The engine
    itself is not yet built (Thread C is the next milestone after this
    cleanup pass). The knobs are wired through config so operators can
    pre-author a config.toml referencing the 15 sibling skills today
    and the engine will pick them up transparently when it lands.

    * ``strictness`` — ``off`` | ``hints`` | ``enforce``; controls
      whether Thread C's validators emit suggestions only, block
      MUTATE tools, or stay dormant.
    * ``enabled_skills`` — the 15-skill set documented in
      ``.claude/skills/``; dropping a skill here disables its rules.
    * ``disabled_rules`` — fine-grained override; e.g.
      ``["SI-IMP-01", "DFM-TRC-02"]`` to silence specific rules.
    * ``severity_overrides`` — map of ``rule_id`` → severity for
      deployment-specific policy (e.g. downgrade ``SI-CLK-04`` to
      ``info`` on hobby-grade projects).
    """

    model_config = ConfigDict(extra="ignore")

    strictness: Literal["off", "hints", "enforce"] = "hints"
    enabled_skills: list[str] = Field(
        default_factory=lambda: [
            "electrical-cad-best-practices",
            "kicad-best-practices",
            "signal-integrity",
            "power-integrity",
            "dfm",
            "rf-design",
            "high-voltage-design",
            "compliance-and-emc-testing",
            "battery-and-low-power",
            "simulation-workflow",
            "mechanical-integration",
            "datasheet-search",
            "errata-search",
            "vendor-search",
            "3d-models-and-footprints-search",
        ]
    )
    disabled_rules: list[str] = Field(default_factory=list)
    severity_overrides: dict[str, str] = Field(default_factory=dict)


class SafetyCfg(BaseModel):
    """Safety knobs.

    * **Wired today** — ``snapshot_mode``, ``audit_enabled``,
      ``audit_read_tools`` (consumed by MUTATE tools and
      ``Server._handle_tools_call`` per M32).
    * **Forward-compat** — ``dry_run_default`` (tools currently expose
      per-call ``dry_run`` rather than a global default);
      ``destructive_confirmation`` (no DESTRUCTIVE tools require a
      confirmation token yet); ``mass_rename_threshold`` (no mass-rename
      tool exists); ``snapshot_retention`` (snapshot module writes but
      doesn't prune yet). These will be consulted as the matching
      features land; keeping them in config avoids schema churn.
    """

    model_config = ConfigDict(extra="ignore")

    dry_run_default: bool = False
    snapshot_mode: Literal["git", "copy", "off"] = "git"
    destructive_confirmation: Literal["always", "session", "never"] = "always"
    mass_rename_threshold: int = 20
    snapshot_retention: int = 20
    # Audit-log knobs (M32). By default every MUTATE / DESTRUCTIVE /
    # EXTERNAL call gets a line appended to ``<project>/.kimcp/audit.log``.
    # READ tools are skipped by default because they're high-volume and
    # low-risk — flipping ``audit_read_tools`` in a compliance-sensitive
    # deployment is the way to opt them in. ``audit_enabled`` is the master
    # switch for the whole mechanism.
    audit_enabled: bool = True
    audit_read_tools: bool = False
    # Grid-snap guard for schematic-mutating tools. Every schematic
    # coordinate passed into ``sch_add_*`` is snapped to the nearest
    # multiple of this value (in millimetres) BEFORE being written. The
    # 2.54 mm default matches KiCAD's native 100-mil eeschema grid —
    # aligning to it eliminates ``endpoint_off_grid`` ERC warnings and
    # prevents labels/wires from dangling just-shy of a pin anchor. Set
    # to ``None`` (or ``safety.grid_snap_mm = null`` in TOML) to opt out
    # if a caller genuinely needs sub-grid precision (rare; the KiCAD
    # schematic editor can't display sub-grid alignments anyway). When
    # a snap moves a coordinate, a single warning is appended to the
    # tool's ``meta.warnings`` so callers see the corrected values
    # rather than silently accepting them.
    grid_snap_mm: float | None = 2.54

    @field_validator("grid_snap_mm")
    @classmethod
    def _validate_grid_snap_mm(cls, v: float | None) -> float | None:
        """Reject zero / negative grid sizes at load time rather than
        divide-by-zero-ing deep in a snap call. ``None`` is the documented
        opt-out value."""
        if v is None:
            return None
        if v <= 0:
            raise ValueError(
                f"safety.grid_snap_mm must be positive (or null to opt out); "
                f"got {v!r}"
            )
        return v

    # Label-vs-wire readability nudge for sch_add_label. When a new local
    # label is placed and another local label with the same net text
    # already exists on the same sheet within this distance (in mm), the
    # tool appends a meta.warnings entry suggesting a wire instead. The
    # 25 mm default catches the common "two nearby labels substituting for
    # a wire" pattern without false-positiving on legitimately long
    # same-sheet net runs. Set to ``None`` to disable the warning entirely
    # (rare — the warning is non-blocking already). global/hierarchical
    # labels are exempt: cross-sheet connectivity is their job.
    label_proximity_warn_mm: float | None = 25.0

    @field_validator("label_proximity_warn_mm")
    @classmethod
    def _validate_label_proximity_warn_mm(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if v <= 0:
            raise ValueError(
                f"safety.label_proximity_warn_mm must be positive "
                f"(or null to disable); got {v!r}"
            )
        return v

    # Anti-crowding nudge for sch_add_symbol. When a new symbol is placed
    # within this origin-to-origin distance (in mm) of an existing symbol
    # on the same sheet, the tool appends a meta.warnings entry citing
    # KICAD-317 (spread components generously). 5.08 mm = 200 mil =
    # 2 schematic-grid units — the floor below which bodies of typical
    # `_Small` symbols (KICAD-316) overlap or pack so tightly that
    # reference/value labels collide. Set to ``None`` to disable.
    # Origin-distance is a fast first-pass heuristic; true bbox-aware
    # collision detection requires parsing lib_symbol graphics, which
    # lives in a later iteration.
    symbol_spacing_warn_mm: float | None = 5.08

    @field_validator("symbol_spacing_warn_mm")
    @classmethod
    def _validate_symbol_spacing_warn_mm(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if v <= 0:
            raise ValueError(
                f"safety.symbol_spacing_warn_mm must be positive "
                f"(or null to disable); got {v!r}"
            )
        return v

    # Snapshot cadence (Perf P1b). The default of 1 means "snapshot
    # every mutating call" — matches the original ADR-0008 semantics
    # and is the safest for one-off edits. For batched workloads (the
    # MK-II session logged 178 mutations) the per-call git snapshot
    # costs ~126 ms × N, so raising this to e.g. 10 means one snapshot
    # per 10 calls — recovery still possible (git checkout the prior
    # snapshot commit), at ~1/10 the wall clock. Set to a very large
    # value (e.g. 1000) for "essentially session-scoped" snapshots, or
    # use ``safety.snapshot_mode = "off"`` to disable entirely. The
    # counter is per-project-root and per-server-session — restarting
    # the server resets it, but git history persists.
    snapshot_every_n_calls: int = 1

    @field_validator("snapshot_every_n_calls")
    @classmethod
    def _validate_snapshot_every_n_calls(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"safety.snapshot_every_n_calls must be >= 1 "
                f"(use snapshot_mode='off' to disable); got {v}"
            )
        return v


class PerformanceCfg(BaseModel):
    """Performance / cache tuning.

    * **Wired today** — ``cache_max_mb`` caps the sexpr ``ParseCache``
      (Server constructs one cache sized from this knob); ``file_watch``
      gates the watchdog-based ``CacheInvalidator`` (ADR-0012 per
      ``performance.md``).
    * **Forward-compat** — ``parallel_exports`` (no batch-export tool
      yet; ``pcb_export_*`` and ``sch_export_*`` run one at a time);
      ``rust_sexpr_parser`` (native-code parser is a sketched optional-
      extra in ``performance.md`` — no implementation yet). Both will
      be honoured when the matching features land.
    """

    model_config = ConfigDict(extra="ignore")

    cache_max_mb: int = 256
    file_watch: bool = True
    parallel_exports: int = 4
    rust_sexpr_parser: Literal["auto", "on", "off"] = "auto"


class ObservabilityCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    log_level: Literal["trace", "debug", "info", "warn", "error"] = "info"
    # Path to an optional rotating file sink. Empty string (the default) means
    # "stderr only" — safest behaviour for tests, CI, and first-run users.
    # Setting a path enables a RotatingFileHandler (10 MB x 5 files per
    # `observability.md`).
    log_path: str = ""
    # Log format. "text" is the classic "%(asctime)s %(levelname)s ..." line;
    # "json" emits one-line JSON per event with the required correlation
    # fields (ts/level/event/session_id/tool/request_id/duration_ms/backend/
    # error) per `observability.md`. JSON is the recommended setting for
    # long-running hosts and compliance-sensitive deployments.
    log_format: Literal["text", "json"] = "text"
    # Tracing (OpenTelemetry) and metrics (Prometheus) are sketched in
    # `observability.md` but deferred past the M32 cleanup pass — wiring them
    # requires new optional-extra deps (opentelemetry-api/sdk,
    # prometheus-client) plus span instrumentation across every tool call.
    # These knobs are preserved as forward-compat placeholders; flipping them
    # today has no runtime effect. They'll be honoured when the observability
    # layer lands as its own milestone.
    trace_enabled: bool = False
    metrics_port: int = 0


class FabProfileCfg(BaseModel):
    """Fab profile — `preset` chooses a preset; custom values live alongside.

    `extra="allow"` so preset-specific keys (min_trace, min_space, etc.) can be
    inlined without us enumerating every field of every preset here.

    **Forward-compat** today: the preset is parsed and validated but not yet
    consulted by any tool. DFM checks (Thread C) and future
    ``pcb_check_fab_capabilities`` will read it to tune rule thresholds to
    the target fab's design rules. Keeping the knob in config now so a
    project-local ``fab_profile.preset = "jlc_4-6_layer"`` lives through
    subsequent milestones without edit.
    """

    model_config = ConfigDict(extra="allow")

    preset: str = "jlc_4-6_layer"


class ExternalApisCfg(BaseModel):
    """Distributor/vendor API credentials.

    Values MUST be indirections (`env:NAME`, `keychain:service/account`,
    `file:path`) — never plaintext secrets. See `security.md`.

    **Forward-compat**: no tool reads this section yet. Upcoming
    external-API tools (datasheet search, distributor lookup, vendor
    catalog browse) will resolve credentials through these indirections
    at call time. Having the schema validate today means operators can
    provision secrets ahead of feature landing.
    """

    model_config = ConfigDict(extra="allow")


class Config(BaseModel):
    """Merged, validated configuration."""

    model_config = ConfigDict(extra="ignore")

    version: int = CONFIG_VERSION
    server: ServerCfg = Field(default_factory=ServerCfg)
    kicad: KiCadCfg = Field(default_factory=KiCadCfg)
    libraries: LibrariesCfg = Field(default_factory=LibrariesCfg)
    domain_knowledge: DomainKnowledgeCfg = Field(default_factory=DomainKnowledgeCfg)
    safety: SafetyCfg = Field(default_factory=SafetyCfg)
    performance: PerformanceCfg = Field(default_factory=PerformanceCfg)
    observability: ObservabilityCfg = Field(default_factory=ObservabilityCfg)
    fab_profile: FabProfileCfg = Field(default_factory=FabProfileCfg)
    external_apis: ExternalApisCfg = Field(default_factory=ExternalApisCfg)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("failed to read config %s: %s", path, exc)
        return {}
    # tomllib.load always returns dict[str, Any] for valid TOML; guard defensively.
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` into `base`. Returns a new dict."""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def user_global_config_path() -> Path:
    """Return the XDG-aware user-global config path."""
    return Path(platformdirs.user_config_dir("kimcp")) / "config.toml"


def project_config_path(project_root: Path | None = None) -> Path:
    root = project_root or Path.cwd()
    return root / ".kimcp" / "config.toml"


def load_config(
    *,
    project_root: Path | None = None,
    session_overrides: dict[str, Any] | None = None,
    user_global: Path | None = None,
    project_local: Path | None = None,
) -> Config:
    """Load the merged, validated config.

    Args are optional overrides primarily for testing. Production callers pass
    nothing and let `load_config()` discover the default locations.
    """
    user_path = user_global if user_global is not None else user_global_config_path()
    project_path = project_local if project_local is not None else project_config_path(project_root)

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, _load_toml(user_path))
    merged = _deep_merge(merged, _load_toml(project_path))
    if session_overrides:
        merged = _deep_merge(merged, session_overrides)

    return Config.model_validate(merged)


__all__ = [
    "CONFIG_VERSION",
    "Config",
    "DomainKnowledgeCfg",
    "ExternalApisCfg",
    "FabProfileCfg",
    "KiCadCfg",
    "LibrariesCfg",
    "ObservabilityCfg",
    "PerformanceCfg",
    "SafetyCfg",
    "ServerCfg",
    "load_config",
    "project_config_path",
    "user_global_config_path",
]
