"""Pydantic v2 models for the M8 datasheet subsystem.

Every data shape that crosses a module boundary — or that we'd ever want to
serialize to JSON for the MCP surface — lives here. Keeping the models in one
file makes it easy to audit ADR-0006 (Pydantic as source of truth) and
ADR-0009 (no hardcoded conventions; paths and sources come from config).

These models are intentionally defensive:
- Every field has a docstring-quality description.
- Timestamps are `datetime` with `UTC` awareness; serialized as ISO-8601.
- Enums are used anywhere a finite value domain exists so downstream
  consumers don't have to invent string comparisons.
- `model_config` forbids extra fields for request/response boundaries but
  allows them on internal cache records (forward compatibility while the
  schema stabilizes).

Nothing in this file does I/O. Nothing here imports httpx, pdfplumber, or
aiofiles. Models must stay loadable in a bare Python 3.11+ environment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SourceKind(str, Enum):
    """Provenance tags for fetched datasheets.

    Values mirror the `SourceAdapter` classes in `fetcher.py`. Stored as strings
    on disk so human inspection of sidecar JSON is trivial.
    """

    MANUFACTURER = "manufacturer"       # DS-001
    DIGIKEY = "digikey"                 # DS-002
    MOUSER = "mouser"                   # DS-002
    NEWARK = "newark"                   # DS-002
    OCTOPART = "octopart"               # DS-003 (URL resolver only)
    LCSC = "lcsc"                       # DS-004
    COMMUNITY = "community"             # DS-005 (opt-in only)
    WAYBACK = "wayback"                 # DS-006
    CACHE = "cache"                     # returned from local cache, not a network source
    USER_UPLOAD = "user_upload"         # bypass fetcher, user provided the PDF


class FindingSeverity(str, Enum):
    """Severity shared with the domain-knowledge engine's Suggestion schema."""

    INFO = "info"
    HINT = "hint"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"   # reserved for DS-062 silicon-rev-linked changes


class DiffClass(str, Enum):
    """DS-062 classification of diff outcomes."""

    EDITORIAL = "editorial"
    CLARIFYING = "clarifying"
    SPECIFICATION = "specification"
    SILICON_REV_LINKED = "silicon_rev_linked"
    STRUCTURAL = "structural"          # section add/remove/rename
    UNPARSED = "unparsed"              # parse failed; surface as a finding


class RevisionProvenance(str, Enum):
    """How confident we are about the revision string."""

    REV_HISTORY_TABLE = "rev_history_table"
    PDF_METADATA = "pdf_metadata"
    FILENAME = "filename"
    URL = "url"
    INFERRED = "inferred"
    USER_PROVIDED = "user_provided"


class ExtractionConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LifecycleHint(str, Enum):
    """Optional hint from the datasheet header or distributor listing."""

    ACTIVE = "active"
    PREVIEW = "preview"
    PRELIMINARY = "preliminary"         # DS-013
    MATURE = "mature"
    NRND = "nrnd"                       # not recommended for new design
    OBSOLETE = "obsolete"               # DS-044
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Shared field types
# ---------------------------------------------------------------------------


Sha256 = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{64}$",
        description="Lowercase hex sha256 digest of the raw PDF bytes.",
    ),
]


# ---------------------------------------------------------------------------
# Cache + revision records
# ---------------------------------------------------------------------------


class CacheEntry(BaseModel):
    """Sidecar JSON record written next to every cached PDF.

    One entry per `(mpn, rev, sha256)` triple — sha256 is included in the key
    because a vendor may replace the same-numbered revision in place (DS-033,
    DS-060). Keeping both on disk preserves auditability.
    """

    model_config = ConfigDict(extra="allow")  # forward-compatible sidecars

    schema_version: int = Field(
        default=1,
        description="Bumped when the on-disk sidecar format changes.",
    )
    mpn: str = Field(..., description="Manufacturer part number, exact case (DS-010).")
    manufacturer: str = Field(..., description="Manufacturer canonical name, e.g. 'STMicroelectronics'.")
    revision: str = Field(..., description="Revision label, e.g. 'Rev. 9, Jun-2024' (DS-011).")
    revision_provenance: RevisionProvenance = Field(
        default=RevisionProvenance.INFERRED,
        description="How the revision label was obtained.",
    )
    revision_date: datetime | None = Field(
        default=None,
        description="ISO timestamp parsed from the revision — None if unknown.",
    )
    lifecycle_hint: LifecycleHint = Field(default=LifecycleHint.UNKNOWN)

    sha256: Sha256
    size_bytes: int = Field(..., ge=0)
    page_count: int | None = Field(default=None, ge=0)

    source_kind: SourceKind
    source_url: AnyHttpUrl | None = Field(
        default=None,
        description="URL the PDF was fetched from (None if USER_UPLOAD or CACHE origin).",
    )
    resolver_chain: list["FetchAttempt"] = Field(
        default_factory=list,
        description="Ordered record of resolver attempts (which adapter, outcome). Audit trail.",
    )
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # On-disk pointer. Stored as a path relative to the cache root so the cache
    # is relocatable (copy the whole directory and nothing breaks).
    relative_pdf_path: str = Field(
        ...,
        description="POSIX-style path under the cache root pointing at the PDF file.",
    )

    # Optional forward-compatibility fields. These are populated by later
    # milestones but reserved here so no sidecar schema break is needed then.
    die_rev: str | None = Field(default=None, description="DS-040 — per-die-rev datasheet marker.")
    aec_q_grade: int | None = Field(default=None, ge=0, le=3, description="DS-078.")
    mil_spec: str | None = Field(default=None, description="DS-076 MIL-PRF / MIL-STD identifier.")
    ppap_available: bool | None = Field(default=None)
    imds_id: str | None = Field(default=None)

    archival: bool = Field(
        default=False,
        description="True if this came from an archive (Wayback) rather than a live vendor source. DS-006, DS-065.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Loose bag for adapter-specific context that isn't worth a dedicated field yet.",
    )

    @field_validator("mpn")
    @classmethod
    def _mpn_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("mpn must be non-empty")
        return v.strip()


class DatasheetRevision(BaseModel):
    """In-memory representation of one specific cached revision.

    Extends `CacheEntry` with the resolved on-disk absolute path and a typed
    `extraction_confidence` so callers can decide whether to trust parsed
    tables from this PDF.
    """

    model_config = ConfigDict(extra="forbid")

    cache_entry: CacheEntry
    absolute_pdf_path: Path = Field(..., description="Resolved absolute path to the PDF on disk.")
    extraction_confidence: ExtractionConfidence = Field(
        default=ExtractionConfidence.MEDIUM,
        description="Text-layer vs scanned PDF heuristic; 'low' means OCR-like content and parameter-table extraction will likely fail.",
    )


class Datasheet(BaseModel):
    """Logical datasheet for a given MPN — a collection of revisions.

    The 'current' revision is defined as the most recent `revision_date` with
    `revision_provenance` != INFERRED, falling back to newest fetched time.
    """

    model_config = ConfigDict(extra="forbid")

    mpn: str
    manufacturer: str
    revisions: list[DatasheetRevision] = Field(default_factory=list)

    def current(self) -> DatasheetRevision | None:
        """Return the 'best' revision per the docstring rule, or None if empty."""
        if not self.revisions:
            return None

        def key(r: DatasheetRevision) -> tuple[int, datetime]:
            # Prefer provenance that isn't INFERRED.
            trusted = r.cache_entry.revision_provenance != RevisionProvenance.INFERRED
            ts = r.cache_entry.revision_date or r.cache_entry.fetched_at
            return (1 if trusted else 0, ts)

        return sorted(self.revisions, key=key, reverse=True)[0]


# ---------------------------------------------------------------------------
# Fetch pipeline
# ---------------------------------------------------------------------------


class FetchAttempt(BaseModel):
    """One resolver attempt. Kept on every `FetchResult` for audit (DS-031)."""

    model_config = ConfigDict(extra="forbid")

    source_kind: SourceKind
    adapter_name: str = Field(..., description="Class name of the adapter that handled this attempt.")
    url_tried: AnyHttpUrl | None = Field(default=None)
    http_status: int | None = Field(default=None)
    outcome: Literal["hit", "miss", "skipped", "error", "rate_limited", "invalid_pdf"]
    detail: str | None = Field(default=None, description="One-line note, e.g. 'missing API key', 'content-type not pdf'.")
    duration_ms: int = Field(default=0, ge=0)
    fetched_bytes: int | None = Field(default=None, ge=0)


class FetchRequest(BaseModel):
    """Input to `DatasheetFetcher.fetch()`."""

    model_config = ConfigDict(extra="forbid")

    mpn: str
    manufacturer: str | None = Field(
        default=None,
        description="Helps disambiguate MPNs collided across vendors; passed through to adapters.",
    )
    prefer_rev: str | None = Field(
        default=None,
        description="If set, try to return this specific revision — the adapter may not honor this but Wayback/LCSC sometimes can.",
    )
    allow_community_mirrors: bool = Field(
        default=False,
        description="DS-005 is last-resort only; caller must opt in.",
    )
    allow_archive: bool = Field(
        default=True,
        description="DS-006 Wayback fallback for obsolete parts. Usually safe.",
    )
    force_refresh: bool = Field(
        default=False,
        description="Skip the cache even if a matching entry exists; used when the caller suspects the cache is stale.",
    )
    user_agent: str | None = Field(
        default=None,
        description="Override default UA; must not be used to impersonate; audit-logged.",
    )


class FetchResult(BaseModel):
    """Return of `DatasheetFetcher.fetch()`."""

    model_config = ConfigDict(extra="forbid")

    request: FetchRequest
    revision: DatasheetRevision | None = Field(
        default=None,
        description="The resolved revision, or None on total failure.",
    )
    attempts: list[FetchAttempt] = Field(default_factory=list)
    from_cache: bool = Field(default=False)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.revision is not None


# ---------------------------------------------------------------------------
# Diff output
# ---------------------------------------------------------------------------


class ParameterRow(BaseModel):
    """Normalized electrical-characteristic row."""

    model_config = ConfigDict(extra="forbid")

    parameter_name: str
    symbol: str | None = Field(default=None)
    min: float | None = Field(default=None)
    typ: float | None = Field(default=None)
    max: float | None = Field(default=None)
    unit: str | None = Field(default=None)
    conditions: str | None = Field(default=None)
    page: int | None = Field(default=None)
    section_heading: str | None = Field(default=None)
    raw_row: list[str] = Field(
        default_factory=list,
        description="Original table row before normalization, kept for audit.",
    )


class ParameterChange(BaseModel):
    """One row-level finding inside a table delta."""

    model_config = ConfigDict(extra="forbid")

    diff_class: DiffClass
    severity: FindingSeverity
    change_kind: Literal["added", "removed", "modified"]
    parameter_name: str
    symbol: str | None = None
    old: ParameterRow | None = None
    new: ParameterRow | None = None
    delta_min: float | None = None
    delta_typ: float | None = None
    delta_max: float | None = None
    unit_changed: bool = False
    errata_candidate: bool = Field(
        default=False,
        description="Flagged for the future errata-search cross-check (DS-031, DS-062).",
    )
    silicon_rev_linked: bool = Field(default=False)
    notes: str | None = None


class SectionChange(BaseModel):
    """Section-heading level finding."""

    model_config = ConfigDict(extra="forbid")

    diff_class: DiffClass = DiffClass.STRUCTURAL
    severity: FindingSeverity = FindingSeverity.INFO
    change_kind: Literal["added", "removed", "renamed", "reordered"]
    old_heading: str | None = None
    new_heading: str | None = None
    old_page: int | None = None
    new_page: int | None = None


class DatasheetDiff(BaseModel):
    """Structured diff between two revisions of the same MPN."""

    model_config = ConfigDict(extra="forbid")

    mpn: str
    manufacturer: str
    old_revision: str
    new_revision: str
    old_sha256: Sha256
    new_sha256: Sha256
    old_page_count: int | None
    new_page_count: int | None

    page_count_delta: int | None = Field(default=None)
    section_changes: list[SectionChange] = Field(default_factory=list)
    parameter_changes: list[ParameterChange] = Field(default_factory=list)
    unparsed_tables: list[str] = Field(
        default_factory=list,
        description="Headings of tables we couldn't parse confidently; included so reviewers don't assume 'no change'.",
    )
    overall_severity: FindingSeverity = FindingSeverity.INFO
    overall_class: DiffClass = DiffClass.EDITORIAL

    # Every finding references the `DS-0xx` rule it's classified under — this
    # makes the diff legal to emit as domain-knowledge engine `Suggestion`s
    # (ADR-0013) without a translation layer.
    rule_citations: list[str] = Field(
        default_factory=list,
        description="Sorted, deduplicated list of DS-0xx rule ids this diff invokes.",
    )

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_suggestions(self) -> list[dict[str, Any]]:
        """Render findings as domain-knowledge-engine Suggestion dicts.

        Shape matches `schemas.md` exactly so the caller can hand them straight
        to the engine with no translation.
        """
        out: list[dict[str, Any]] = []
        for sc in self.section_changes:
            out.append(
                {
                    "rule_id": "DS-061",
                    "skill": "datasheet-search",
                    "severity": sc.severity.value,
                    "message": f"Section {sc.change_kind}: "
                    f"{sc.old_heading or '-'} -> {sc.new_heading or '-'}",
                    "why": "Structural change between datasheet revisions (DS-061, DS-063).",
                    "fix_hint": "Review affected section; confirm design still matches new structure.",
                    "references": [
                        f"datasheet:{self.mpn}:{self.old_revision}",
                        f"datasheet:{self.mpn}:{self.new_revision}",
                    ],
                }
            )
        for pc in self.parameter_changes:
            rule = "DS-062"
            if pc.silicon_rev_linked:
                rule = "DS-062"   # same rule, silicon-rev-linked bullet
            out.append(
                {
                    "rule_id": rule,
                    "skill": "datasheet-search",
                    "severity": pc.severity.value,
                    "message": f"{pc.parameter_name} {pc.change_kind}: "
                    f"{_fmt_row(pc.old)} -> {_fmt_row(pc.new)}",
                    "why": (
                        "Specification change between datasheet revisions "
                        "(DS-062). Audit the design against new numbers."
                        if pc.diff_class == DiffClass.SPECIFICATION
                        else f"Revision diff classified as {pc.diff_class.value} (DS-062)."
                    ),
                    "fix_hint": (
                        "Open errata-search for this MPN if errata_candidate=True."
                        if pc.errata_candidate
                        else "Re-run design validators against the new spec."
                    ),
                    "references": [
                        f"datasheet:{self.mpn}:{self.old_revision}",
                        f"datasheet:{self.mpn}:{self.new_revision}",
                    ],
                }
            )
        return out


def _fmt_row(row: ParameterRow | None) -> str:
    if row is None:
        return "-"
    parts = []
    if row.min is not None:
        parts.append(f"min={row.min}")
    if row.typ is not None:
        parts.append(f"typ={row.typ}")
    if row.max is not None:
        parts.append(f"max={row.max}")
    if row.unit:
        parts.append(row.unit)
    return ", ".join(parts) if parts else (row.conditions or "-")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class DatasheetConfig(BaseModel):
    """Configuration surface for the datasheet subsystem.

    Consumed by `DatasheetFetcher`, `DatasheetCache`, and `DatasheetDiffer`.
    The main server will populate this from the merged TOML config described
    in `configuration.md` (new `[datasheet]` section).
    """

    model_config = ConfigDict(extra="forbid")

    cache_root: Path
    project_mirror: Path | None = Field(default=None)

    source_order: list[SourceKind] = Field(
        default_factory=lambda: [
            SourceKind.MANUFACTURER,
            SourceKind.DIGIKEY,
            SourceKind.MOUSER,
            SourceKind.OCTOPART,
            SourceKind.LCSC,
        ]
    )
    allow_community_mirrors: bool = False
    allow_archive: bool = True

    rate_limit_per_host_rps: float = Field(default=1.0, gt=0)
    request_timeout_s: float = Field(default=30.0, gt=0)
    max_pdf_mb: int = Field(default=32, gt=0)

    # Table-extraction / diff configuration. Nothing MPN-specific; everything
    # is parameterized so `ADR-0009` holds.
    parameter_table_headings: list[str] = Field(
        default_factory=lambda: [
            "Absolute Maximum Ratings",
            "Recommended Operating Conditions",
            "DC Characteristics",
            "AC Characteristics",
            "Electrical Characteristics",
            "Thermal Characteristics",
        ]
    )
    errata_relevant_fields: list[str] = Field(
        default_factory=lambda: [
            "operating temperature",
            "absolute maximum",
            "supply voltage",
            "esd",
            "bandgap",
            "i/o voltage",
        ]
    )

    # Per-manufacturer URL templates for the ManufacturerAdapter. Keeping this
    # as data means adding a new vendor is a config change, not a code change
    # (ADR-0009). Example: {"stmicroelectronics": "https://.../{mpn_lower}.pdf"}
    manufacturer_url_templates: dict[str, str] = Field(default_factory=dict)

    # Secrets are always indirected per configuration.md. Each is a string like
    # "env:KIMCP_DIGIKEY_CLIENT_ID" or "keychain:kimcp/digikey_client_id". The
    # adapters resolve them at call time; missing → adapter marks itself
    # `skipped`.
    digikey_client_id: str | None = None
    digikey_client_secret: str | None = None
    mouser_api_key: str | None = None
    octopart_api_key: str | None = None


# Resolve forward ref on CacheEntry
CacheEntry.model_rebuild()
