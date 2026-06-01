"""Base class + value types for MCP prompts.

The MCP spec (2025-06-18) describes a prompt as a named, optionally
parameterized template that expands into a list of messages the LLM
sees verbatim. Prompts are the "canned workflow" primitive — a client
surfaces them as slash-commands or suggestions, and invoking one
injects a pre-shaped conversation starter into the session.

Scope choice here: ``Prompt`` is a plain Python base class, not a
Pydantic model. Prompts have no user-provided schema beyond the
``arguments`` list (name + description + required flag), so dragging
Pydantic into this layer would be weight with no payoff. The argument
dict that reaches ``render()`` is already string-keyed and
string-valued by the MCP wire format; concrete prompts can coerce at
their edge if they need richer types.

Contract:

* ``name`` — stable ID the client invokes by. Must be unique across
  the registry.
* ``description`` — one-liner that renders in the client's prompt
  picker. Keep under ~100 chars.
* ``arguments`` — tuple of ``PromptArgument`` declarations. Empty
  tuple means "no parameters".
* ``render(arguments)`` — the expansion step. Takes the validated
  argument dict and returns a list of MCP message dicts (shape:
  ``{"role": "user"|"assistant", "content": {"type": "text", "text": str}}``).
  Implementations MUST NOT raise on missing required args — the
  registry validates before calling ``render``.

Why not chain tool calls directly (as the skill doc implies): the MCP
prompt primitive is text-only. Tool chaining would mean the client
executes tools server-side on prompt expansion, which (1) breaks the
client-controlled consent model (every tool call is supposed to
surface for user approval) and (2) re-invents orchestration that's
already the LLM's job. We expand to messages that *tell* the LLM
which tools to call in what order; the LLM drives the actual
invocations with user oversight.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True)
class PromptArgument:
    """One declared parameter of a prompt.

    Matches the shape of an element in MCP's ``prompts/list`` response
    ``arguments`` array: ``{name, description?, required?}``. ``type``
    is deliberately absent — MCP doesn't type prompt arguments at the
    protocol level; everything is a string and concrete prompts parse
    at their edge.
    """

    name: str
    description: str = ""
    required: bool = False

    def to_mcp(self) -> dict[str, Any]:
        """Shape for the ``prompts/list`` response."""
        out: dict[str, Any] = {"name": self.name}
        if self.description:
            out["description"] = self.description
        if self.required:
            out["required"] = True
        return out


class Prompt(ABC):
    """Base class for an MCP prompt.

    Concrete prompts set the class-level metadata and implement
    ``render``. See module docstring for the contract.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    arguments: ClassVar[tuple[PromptArgument, ...]] = ()

    # ------------------------------------------------------------------

    @abstractmethod
    def render(self, arguments: dict[str, str]) -> list[dict[str, Any]]:
        """Expand the prompt into a message list for ``prompts/get``.

        ``arguments`` is the validated-by-registry dict — every
        ``required=True`` argument is guaranteed present. Missing
        optional arguments are simply absent from the dict (not
        auto-filled with empty strings); concrete prompts handle
        defaults explicitly so "absent" and "empty string" remain
        distinguishable.
        """


__all__ = ["Prompt", "PromptArgument"]
