"""Built-in tools — register via entry points alongside external packages."""

from __future__ import annotations

from kimcp.tools.builtin.config_show import ConfigShowTool
from kimcp.tools.builtin.ping import PingTool
from kimcp.tools.builtin.version import VersionTool

__all__ = ["ConfigShowTool", "PingTool", "VersionTool"]
