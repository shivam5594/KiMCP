"""Backend adapter contract.

Each backend declares its `kind` and an async `probe()` that returns whether
the backend is available in the current environment. Concrete backends expand
this interface with operation-specific methods wired through the dispatcher.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kimcp._types import Backend


@runtime_checkable
class BackendAdapter(Protocol):
    """Minimal contract every backend satisfies."""

    kind: Backend

    async def probe(self) -> bool:
        """Return True if this backend is usable in this session."""
        ...


__all__ = ["BackendAdapter"]
