"""Transport layer — STDIO today, HTTP+SSE later (ADR-0007)."""

from __future__ import annotations

from kimcp.transport.base import Transport
from kimcp.transport.stdio import StdioTransport

__all__ = ["StdioTransport", "Transport"]
