"""Unit tests for the tool registry."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool
from kimcp.tools.builtin.ping import PingTool
from kimcp.tools.builtin.version import VersionTool
from kimcp.tools.registry import ToolRegistry


class _NoopInput(BaseModel):
    x: int = 0


class _NoopOutput(ToolOutput):
    x: int


class _NoopTool(Tool[_NoopInput, _NoopOutput]):
    name = "noop"
    version = "0.1.0"
    description = "test tool"
    input_model = _NoopInput
    output_model = _NoopOutput

    async def run(self, input):
        return _NoopOutput(x=input.x)


def test_register_and_get() -> None:
    reg = ToolRegistry()
    tool = _NoopTool()
    reg.register(tool)
    assert reg.get("noop") is tool
    assert reg.all_tools() == [tool]


def test_duplicate_register_raises() -> None:
    reg = ToolRegistry()
    reg.register(_NoopTool())
    with pytest.raises(ValueError):
        reg.register(_NoopTool())


def test_tool_without_name_raises() -> None:
    class _AnonTool(_NoopTool):
        name = ""

    reg = ToolRegistry()
    with pytest.raises(ValueError):
        reg.register(_AnonTool())


def test_mcp_tool_list_shape() -> None:
    reg = ToolRegistry()
    reg.register(PingTool())
    reg.register(VersionTool())

    listing = reg.mcp_tool_list()
    names = {t["name"] for t in listing}
    assert names == {"ping", "version"}

    for entry in listing:
        assert {"name", "description", "inputSchema"} <= set(entry.keys())
        assert entry["inputSchema"].get("type") == "object"


def test_load_entry_points_registers_builtins() -> None:
    """The package's own entry points should find the three built-in tools."""
    reg = ToolRegistry()
    added = reg.load_entry_points()
    # The package registers 3 built-in tools in pyproject.toml.
    names = {t.name for t in reg.all_tools()}
    assert {"ping", "version", "config_show"} <= names
    assert added >= 3
