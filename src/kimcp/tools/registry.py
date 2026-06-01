"""Tool registry with entry-point plugin discovery (ADR-0005).

Built-in tools register via the same `kimcp.tools` entry-point group as
external packages. That dogfoods the plugin mechanism from day one and lets
users disable built-ins cleanly.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any

from kimcp.tools.base import Tool

log = logging.getLogger(__name__)


class ToolRegistry:
    ENTRY_POINT_GROUP = "kimcp.tools"

    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, tool: Tool[Any, Any]) -> None:
        if not tool.name:
            raise ValueError(f"tool {tool.__class__.__name__} has no `name` class attribute")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool[Any, Any] | None:
        return self._tools.get(name)

    def all_tools(self) -> list[Tool[Any, Any]]:
        return list(self._tools.values())

    # ------------------------------------------------------------------
    # Entry-point discovery
    # ------------------------------------------------------------------

    def load_entry_points(self, *, skip_duplicates: bool = True) -> int:
        """Discover tools via `kimcp.tools` entry points.

        Returns the number of tools successfully registered. Errors in one
        entry point are logged but do not abort discovery.
        """
        added = 0
        for ep in entry_points(group=self.ENTRY_POINT_GROUP):
            try:
                loaded = ep.load()
            except Exception:
                log.exception("failed to import tool entry point %s", ep.name)
                continue

            try:
                tool = loaded() if isinstance(loaded, type) else loaded
            except Exception:
                log.exception("failed to instantiate tool from entry point %s", ep.name)
                continue

            if not isinstance(tool, Tool):
                log.warning(
                    "entry point %s did not produce a Tool instance (got %r)",
                    ep.name,
                    type(tool),
                )
                continue

            if tool.name in self._tools:
                if skip_duplicates:
                    log.debug("tool %s already registered; skipping %s", tool.name, ep.name)
                    continue
                raise ValueError(f"duplicate tool: {tool.name}")

            self._tools[tool.name] = tool
            added += 1
            log.info("registered tool from entry point %s: %s", ep.name, tool.name)

        return added

    # ------------------------------------------------------------------
    # MCP surface
    # ------------------------------------------------------------------

    def mcp_tool_list(self) -> list[dict[str, Any]]:
        """Shape suitable for the MCP `tools/list` response."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.json_schema(),
            }
            for t in self._tools.values()
        ]


__all__ = ["ToolRegistry"]
