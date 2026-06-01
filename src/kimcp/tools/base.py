"""Tool base class — the MCP-surface contract every tool implements.

Contract mirrors `.claude/skills/kimcp-architecture/schemas.md`:

    name, version, description
    input_model  (Pydantic)
    output_model (Pydantic; subclasses ToolOutput so `meta` is guaranteed)
    classification (read / mutate / destructive / external)
    required_backends + preferred_backends
    live_gui_visible, mutates, destructive flags
    deprecated_in / remove_in  (optional semver)
    run(input) -> output       (async)

Per ADR-0006, JSON Schema exposed to MCP clients is generated from the
Pydantic input model — never hand-maintained.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from kimcp._types import Backend, ToolClass
from kimcp.schemas.envelope import ToolOutput

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=ToolOutput)


class Tool(ABC, Generic[InputT, OutputT]):
    """Base class for an MCP tool.

    Concrete tools set the class-level metadata and implement `run`.
    """

    name: ClassVar[str] = ""
    version: ClassVar[str] = "0.0.0"
    description: ClassVar[str] = ""

    # Schemas — subclasses MUST override
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[ToolOutput]]

    classification: ClassVar[ToolClass] = ToolClass.READ
    required_backends: ClassVar[frozenset[Backend]] = frozenset()
    preferred_backends: ClassVar[tuple[Backend, ...]] = ()
    live_gui_visible: ClassVar[bool] = False
    mutates: ClassVar[bool] = False
    destructive: ClassVar[bool] = False

    deprecated_in: ClassVar[str | None] = None
    remove_in: ClassVar[str | None] = None

    # ------------------------------------------------------------------

    @abstractmethod
    async def run(self, input: InputT) -> OutputT:
        """Execute the tool against the validated input model."""

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """JSON Schema of the input model (generated; ADR-0006)."""
        return cls.input_model.model_json_schema()


__all__ = ["Tool"]
