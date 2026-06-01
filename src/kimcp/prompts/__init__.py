"""Prompts layer — MCP ``prompts/list`` + ``prompts/get`` surface.

Public API is the small set re-exported below. Concrete prompt
implementations live in ``kimcp.prompts.builtin`` and are registered
on ``Server`` construction.
"""

from __future__ import annotations

from kimcp.prompts.base import Prompt, PromptArgument
from kimcp.prompts.registry import PromptRegistry

__all__ = ["Prompt", "PromptArgument", "PromptRegistry"]
