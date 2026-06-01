"""Shared small enums and aliases used across the package.

Kept deliberately small — only types with no useful behavior. Types with
domain logic live next to their domain module.
"""

from __future__ import annotations

from enum import StrEnum


class Backend(StrEnum):
    """Which KiCAD integration surface did (or can) service an operation.

    See `.claude/skills/kimcp-architecture/backends.md`.
    """

    IPC = "ipc"
    CLI = "cli"
    SEXPR = "sexpr"
    SWIG = "swig"


class Severity(StrEnum):
    """Severity attached to domain-knowledge suggestions.

    See `.claude/skills/kimcp-architecture/schemas.md` for the Suggestion shape.
    """

    INFO = "info"
    HINT = "hint"
    WARN = "warn"
    ERROR = "error"


class ToolClass(StrEnum):
    """Read/mutate/destructive/external classification per `safety.md`."""

    READ = "read"
    MUTATE = "mutate"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"


__all__ = ["Backend", "Severity", "ToolClass"]
