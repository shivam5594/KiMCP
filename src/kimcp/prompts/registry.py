"""Prompt registry — discovery + argument validation for the prompts layer.

Mirrors ``ToolRegistry`` shape-for-shape so a future ``mcp_prompt_list``
admin command reads like the tool one. Two small departures:

* **No entry-point loader (yet).** Prompts are registered in-process
  from ``server.py``. The skill doc calls out third-party prompt
  extensibility via entry points, but holding that until we see a
  concrete third party lets the in-process registration stabilize
  first. When we add it, the API is the same ``ENTRY_POINT_GROUP`` +
  ``load_entry_points`` pattern the tool registry already uses.

* **Argument validation lives here, not in the handler.** Tools get
  validation via Pydantic on ``input_model``; prompts don't have a
  Pydantic surface, so the registry is the natural place to check
  required-arg presence before ``render`` sees the dict. That keeps
  every concrete prompt free of guard code.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from kimcp.errors import INVALID_PARAMS, METHOD_NOT_FOUND, RpcError
from kimcp.prompts.base import Prompt

log = logging.getLogger(__name__)


class PromptRegistry:
    def __init__(self) -> None:
        self._prompts: dict[str, Prompt] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, prompt: Prompt) -> None:
        if not prompt.name:
            raise ValueError(
                f"prompt {prompt.__class__.__name__} has no `name` class attribute"
            )
        if prompt.name in self._prompts:
            raise ValueError(f"duplicate prompt name: {prompt.name}")
        self._prompts[prompt.name] = prompt

    def unregister(self, name: str) -> None:
        self._prompts.pop(name, None)

    def get(self, name: str) -> Prompt | None:
        return self._prompts.get(name)

    def all_prompts(self) -> Iterable[Prompt]:
        return list(self._prompts.values())

    # ------------------------------------------------------------------
    # MCP surface
    # ------------------------------------------------------------------

    def mcp_prompt_list(self) -> list[dict[str, Any]]:
        """Shape suitable for the MCP ``prompts/list`` response.

        Entries are sorted by name for deterministic output (matches
        ``ResourceProvider.list_resources`` and what the admin CLI
        already does for tools).
        """
        items: list[dict[str, Any]] = []
        for prompt in sorted(self._prompts.values(), key=lambda p: p.name):
            entry: dict[str, Any] = {"name": prompt.name}
            if prompt.description:
                entry["description"] = prompt.description
            if prompt.arguments:
                entry["arguments"] = [arg.to_mcp() for arg in prompt.arguments]
            items.append(entry)
        return items

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def render(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate + render. Raises ``RpcError`` on unknown name / bad args.

        Returns the full ``prompts/get`` result body:
        ``{"description"?: str, "messages": [...]}``.
        """
        prompt = self._prompts.get(name)
        if prompt is None:
            # Use METHOD_NOT_FOUND to match how ``tools/call`` reports an
            # unknown tool — the client-side semantics are the same
            # ("that name doesn't exist"), so clients can share branch
            # handling.
            raise RpcError(METHOD_NOT_FOUND, f"unknown prompt: {name}")

        # Normalize: coerce non-string scalar args to strings rather
        # than refusing them. MCP arg values are supposed to be strings
        # per the spec, but some clients send ints / bools for
        # convenience. Normalizing up-front keeps every concrete prompt
        # from reimplementing the coercion.
        normalized: dict[str, str] = {}
        for key, value in arguments.items():
            if not isinstance(key, str):
                raise RpcError(
                    INVALID_PARAMS,
                    "prompt argument keys must be strings",
                    {"offending_key": repr(key)},
                )
            if value is None:
                continue  # treat explicit null as absent
            normalized[key] = value if isinstance(value, str) else str(value)

        # Required-arg check. Happens after normalization so "provided
        # but empty string" is distinguishable from "not provided at
        # all" — the former passes, the latter fails.
        missing = [
            arg.name
            for arg in prompt.arguments
            if arg.required and arg.name not in normalized
        ]
        if missing:
            raise RpcError(
                INVALID_PARAMS,
                f"prompt {name!r} is missing required argument(s): {', '.join(missing)}",
                {"prompt": name, "missing": missing},
            )

        messages = prompt.render(normalized)
        body: dict[str, Any] = {"messages": messages}
        if prompt.description:
            body["description"] = prompt.description
        return body


__all__ = ["PromptRegistry"]
