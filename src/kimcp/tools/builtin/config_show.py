"""Config-show — returns the effective merged config.

Mirrors `kimcp-cli config show` but over the MCP surface so clients can
discover the server's configuration without shell access.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from kimcp._types import ToolClass
from kimcp.config import Config, load_config
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool


class ConfigShowInput(BaseModel):
    pass


class ConfigShowOutput(ToolOutput):
    config: dict[str, Any]


class ConfigShowTool(Tool[ConfigShowInput, ConfigShowOutput]):
    """Return the merged config as a JSON-compatible dict.

    The server overwrites `_config` post-registration so the tool reflects the
    loaded config rather than re-loading from disk. Falls back to defaults if
    the server hasn't wired it up (e.g., bare entry-point load for listing).
    """

    name = "config_show"
    version = "0.1.0"
    description = "Return the effective (merged) KiMCP configuration."
    input_model = ConfigShowInput
    output_model = ConfigShowOutput
    classification = ToolClass.READ

    def __init__(self, config: Config | None = None) -> None:
        self._config = config

    def set_config(self, config: Config) -> None:
        self._config = config

    async def run(self, input: ConfigShowInput) -> ConfigShowOutput:
        cfg = self._config if self._config is not None else load_config()
        return ConfigShowOutput(config=cfg.model_dump(mode="json"))


__all__ = ["ConfigShowInput", "ConfigShowOutput", "ConfigShowTool"]
