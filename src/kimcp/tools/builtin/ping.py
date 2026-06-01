"""Ping — liveness check. No backend required.

Exists so a newly installed KiMCP can be probed end-to-end without a running
KiCAD instance. Also serves as the minimal reference-tool for tutorials.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kimcp._types import ToolClass
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool


class PingInput(BaseModel):
    message: str = Field("ping", description="Payload to echo back.")


class PingOutput(ToolOutput):
    echo: str


class PingTool(Tool[PingInput, PingOutput]):
    name = "ping"
    version = "0.1.0"
    description = "Liveness check — echoes the input message back to the caller."
    input_model = PingInput
    output_model = PingOutput
    classification = ToolClass.READ

    async def run(self, input: PingInput) -> PingOutput:
        return PingOutput(echo=input.message)


__all__ = ["PingInput", "PingOutput", "PingTool"]
