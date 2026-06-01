"""Transport contract (`.claude/skills/kimcp-architecture/transport.md`).

Keep this interface tiny — anything concrete about framing, streaming, or
auth belongs in the specific transport implementation, not here.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """Minimal async message transport the JSON-RPC layer sits on top of."""

    async def read_message(self) -> dict[str, Any] | None:
        """Return the next message, or None on graceful EOF."""
        ...

    async def write_message(self, msg: dict[str, Any]) -> None:
        """Send a message."""
        ...

    async def close(self) -> None:
        """Release OS resources. Safe to call more than once."""
        ...


__all__ = ["Transport"]
