"""Unit tests for the `kicad_ipc_status` built-in tool.

Covers the three output states (reachable / unreachable / not_found) by
injecting an IpcBackend pointed at a live peer, a stale path, and a
missing path respectively.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from kimcp.backends.ipc import IpcBackend
from kimcp.tools.builtin.kicad_ipc_status import (
    KiCadIpcStatusInput,
    KiCadIpcStatusTool,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix-socket-based tests; Windows pipe path is platform-gated.",
)


async def _start_accepting_server(path: Path) -> asyncio.AbstractServer:
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    return await asyncio.start_unix_server(_handle, path=str(path))


@pytest.mark.asyncio
async def test_status_reachable_when_peer_accepts(socket_tmp_path: Path) -> None:
    sock_path = socket_tmp_path / "reachable.sock"
    server = await _start_accepting_server(sock_path)
    try:
        tool = KiCadIpcStatusTool()
        tool.set_ipc_backend(IpcBackend(configured_path=str(sock_path)))
        out = await tool.run(KiCadIpcStatusInput())
        assert out.status == "reachable"
        assert out.socket_path == str(sock_path.resolve())
        assert out.note is None
        assert out.probe_timeout_sec > 0
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_status_unreachable_when_socket_stale(socket_tmp_path: Path) -> None:
    import socket as _socket

    sock_path = socket_tmp_path / "stale.sock"
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.bind(str(sock_path))
    finally:
        s.close()

    tool = KiCadIpcStatusTool()
    tool.set_ipc_backend(IpcBackend(configured_path=str(sock_path)))
    out = await tool.run(KiCadIpcStatusInput())
    assert out.status == "unreachable"
    assert out.socket_path == str(sock_path.resolve())
    assert out.note is not None
    assert "did not accept" in out.note


@pytest.mark.asyncio
async def test_status_not_found_when_path_missing(tmp_path: Path) -> None:
    # No bind() in this test — regular `tmp_path` is fine.
    tool = KiCadIpcStatusTool()
    tool.set_ipc_backend(IpcBackend(configured_path=str(tmp_path / "nope.sock")))
    out = await tool.run(KiCadIpcStatusInput())
    assert out.status == "not_found"
    assert out.socket_path is None
    assert out.note is not None
    assert "no IPC socket discovered" in out.note


@pytest.mark.asyncio
async def test_tool_without_injection_builds_default_backend() -> None:
    """Bare entry-point load: ``run`` must not crash even when server-side
    dependency injection hasn't happened yet. Status is whatever the host
    happens to expose; assert the shape only."""
    tool = KiCadIpcStatusTool()
    out = await tool.run(KiCadIpcStatusInput())
    assert out.status in {"reachable", "unreachable", "not_found"}
    assert out.probe_timeout_sec > 0
