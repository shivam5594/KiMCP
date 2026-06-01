"""STDIO transport — newline-delimited JSON on stdin/stdout.

Per `transport.md`:
- stderr is reserved for logging; never carries JSON-RPC bytes.
- Partial lines are buffered until a newline arrives.
- Invalid JSON on stdin is logged and skipped (not a fatal error).

The writer uses a small executor off-load to avoid blocking the loop on
stdout, while serializing writes behind an asyncio lock so interleaved
messages cannot split.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

log = logging.getLogger(__name__)


class StdioTransport:
    """Async STDIO transport suitable for local MCP integrations."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self._write_lock = asyncio.Lock()
        self._closed = False

    # Construction -------------------------------------------------------

    @classmethod
    async def create(cls) -> StdioTransport:
        """Attach to the process stdin/stdout streams."""
        loop = asyncio.get_running_loop()

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        return cls(reader=reader)

    # Transport protocol -------------------------------------------------

    async def read_message(self) -> dict[str, Any] | None:
        while True:
            if self._closed:
                return None
            try:
                line = await self._reader.readline()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("stdin read failed")
                return None

            if not line:
                # EOF — caller treats this as graceful shutdown
                return None

            stripped = line.strip()
            if not stripped:
                continue

            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                log.warning("discarding invalid JSON on stdin: %r", stripped[:200])
                continue
            if not isinstance(parsed, dict):
                log.warning("discarding non-object JSON on stdin: %r", stripped[:200])
                continue
            return parsed

    async def write_message(self, msg: dict[str, Any]) -> None:
        if self._closed:
            log.debug("write attempted after close; dropping message")
            return
        data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n"
        async with self._write_lock:
            # stdout.write is blocking; offload so the loop stays responsive
            # under heavy output (e.g., large suggestion payloads).
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _blocking_write, data)

    async def close(self) -> None:
        self._closed = True


def _blocking_write(data: str) -> None:
    sys.stdout.write(data)
    sys.stdout.flush()


__all__ = ["StdioTransport"]
