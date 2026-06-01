"""Version tool — reports KiMCP, Python, and OS-platform strings.

Useful for diagnostic logs attached to bug reports.
"""

from __future__ import annotations

import platform
import sys

from pydantic import BaseModel

from kimcp import __version__
from kimcp._types import ToolClass
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool


class VersionInput(BaseModel):
    pass


class VersionOutput(ToolOutput):
    kimcp_version: str
    python_version: str
    platform: str


class VersionTool(Tool[VersionInput, VersionOutput]):
    name = "version"
    version = "0.1.0"
    description = "Return KiMCP, Python, and platform versions."
    input_model = VersionInput
    output_model = VersionOutput
    classification = ToolClass.READ

    async def run(self, input: VersionInput) -> VersionOutput:
        return VersionOutput(
            kimcp_version=__version__,
            python_version=sys.version.split()[0],
            platform=platform.platform(),
        )


__all__ = ["VersionInput", "VersionOutput", "VersionTool"]
