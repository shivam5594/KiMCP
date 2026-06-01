"""JSON-RPC error codes and the `RpcError` exception.

Codes track `.claude/skills/kimcp-architecture/schemas.md` exactly. Adding a
new code requires an ADR bump and a docs update.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 standard error codes
# ---------------------------------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# ---------------------------------------------------------------------------
# KiMCP-specific codes (schemas.md)
# ---------------------------------------------------------------------------
VALIDATION_ERROR = -32000
BACKEND_UNAVAILABLE = -32001
DESTRUCTIVE_REFUSED = -32002
KICAD_VERSION_INCOMPAT = -32003
RULE_VIOLATION = -32004


@dataclass
class RpcError(Exception):
    """Raised inside handlers; the RPC layer formats this into a JSON-RPC error.

    `data` carries structured context (e.g., `rule_id`, validation errors)
    that the client can render meaningfully.
    """

    code: int
    message: str
    data: dict[str, Any] | None = None

    def __post_init__(self) -> None:  # keep Exception protocol happy
        super().__init__(f"[{self.code}] {self.message}")

    def to_rpc(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


__all__ = [
    "BACKEND_UNAVAILABLE",
    "DESTRUCTIVE_REFUSED",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "KICAD_VERSION_INCOMPAT",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "RULE_VIOLATION",
    "VALIDATION_ERROR",
    "RpcError",
]
