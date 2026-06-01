"""Standard output envelope embedded in every tool result.

Shape matches `.claude/skills/kimcp-architecture/schemas.md` exactly:

    meta: {
      backend_used, live_sync, duration_ms,
      warnings, suggestions, snapshot_ref
    }

`ToolOutput` is a base class — every tool's `output_model` inherits it so the
envelope is guaranteed present without per-tool boilerplate.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kimcp._types import Backend, Severity


class Suggestion(BaseModel):
    """A cited suggestion from the domain-knowledge engine.

    Per ADR-0013, every suggestion must cite the rule it came from so users
    can audit and override.
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str = Field(
        ...,
        description="Stable id from the originating sibling skill (e.g., 'SI-014').",
    )
    skill: str = Field(
        ...,
        description="Skill that owns the rule (e.g., 'signal-integrity').",
    )
    severity: Severity
    message: str = Field(..., description="One-line human-readable summary.")
    why: str = Field(..., description="Reasoning behind the suggestion, citing the rule.")
    fix_hint: str = Field("", description="Actionable next step, if any.")
    references: list[str] = Field(
        default_factory=list,
        description="Datasheets, app notes, standards (IPC-2221, etc.) backing the rule.",
    )


class Meta(BaseModel):
    """Envelope metadata attached to every tool output."""

    backend_used: Backend | None = Field(
        default=None,
        description="Which backend serviced the call; None for backend-agnostic tools.",
    )
    live_sync: bool = Field(
        default=True,
        description="True when GUI-visible state is in sync with the returned result.",
    )
    duration_ms: int = Field(default=0, description="Wall-clock time the tool took.")
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(
        default_factory=list,
        description="Suggestions emitted by the domain-knowledge engine.",
    )
    snapshot_ref: str | None = Field(
        default=None,
        description="Snapshot reference if a snapshot was taken (git SHA or copy path).",
    )


class ToolOutput(BaseModel):
    """Base class for every tool's `output_model`.

    Tools subclass this and add their own domain fields. The envelope's `meta`
    field is always present.
    """

    meta: Meta = Field(default_factory=Meta)


__all__ = ["Meta", "Suggestion", "ToolOutput"]
