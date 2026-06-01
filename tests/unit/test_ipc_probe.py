"""Unit tests for the IPC socket reachability probe.

Uses an in-process ``asyncio.start_unix_server`` as the "peer" so
probe_socket actually performs a real connect/close roundtrip. No KiCAD
involvement — the peer just accepts the connection and drops it.
Windows named-pipe path is platform-gated: covered by the cross-platform
branch in probe.py; not exercised in CI until a Windows runner lands.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from kimcp.ipc.errors import IpcProbeTimeoutError
from kimcp.ipc.probe import probe_socket

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix-socket probe; Windows pipe path is platform-gated.",
)


async def _start_accepting_server(path: Path) -> asyncio.AbstractServer:
    """Start a Unix-socket server that accepts connections and drops them.

    Returns the running server so the caller can ``close()`` at teardown.
    """

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Drop immediately. The probe only cares that accept() succeeded.
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    return await asyncio.start_unix_server(_handle, path=str(path))


@pytest.mark.asyncio
async def test_probe_returns_true_for_reachable_socket(socket_tmp_path: Path) -> None:
    sock_path = socket_tmp_path / "reachable.sock"
    server = await _start_accepting_server(sock_path)
    try:
        assert await probe_socket(str(sock_path), timeout=2.0) is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_returns_false_for_missing_path(tmp_path: Path) -> None:
    # Nothing at this path — connect fails with FileNotFoundError. No bind()
    # happens, so regular `tmp_path` is fine here.
    assert await probe_socket(str(tmp_path / "nope.sock"), timeout=2.0) is False


@pytest.mark.asyncio
async def test_probe_returns_false_for_stale_socket(socket_tmp_path: Path) -> None:
    """A socket file that nobody is listening on refuses connections.

    We simulate this by creating an unbound socket file path (bind but
    don't listen, then close). The file remains but connections fail.
    """
    import socket as _socket

    sock_path = socket_tmp_path / "stale.sock"
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.bind(str(sock_path))
    finally:
        # Don't listen — path exists, but connect will fail with ECONNREFUSED.
        s.close()
    assert sock_path.exists()

    assert await probe_socket(str(sock_path), timeout=2.0) is False


@pytest.mark.asyncio
async def test_probe_returns_false_for_regular_file(tmp_path: Path) -> None:
    """A regular file at the path is not a socket. Returns False rather
    than raising — this can happen if a user aims ``ipc_socket`` at the
    wrong thing by accident. No bind() here so `tmp_path` is safe."""
    bogus = tmp_path / "not-a-socket"
    bogus.write_text("hello")
    assert await probe_socket(str(bogus), timeout=2.0) is False


@pytest.mark.asyncio
async def test_probe_raises_timeout_when_connect_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow / hung peer → ``IpcProbeTimeoutError``.

    We patch ``asyncio.open_unix_connection`` with a coroutine that sleeps
    well past the probe timeout. The outer ``wait_for`` fires and the
    probe translates that to the typed ``IpcProbeTimeoutError``. The patched
    coroutine never calls ``bind()``, so regular ``tmp_path`` is fine.
    """

    async def hung_connection(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr("kimcp.ipc.probe.asyncio.open_unix_connection", hung_connection)

    with pytest.raises(IpcProbeTimeoutError) as excinfo:
        await probe_socket(str(tmp_path / "whatever.sock"), timeout=0.05)
    assert excinfo.value.timeout == 0.05
    assert excinfo.value.socket_path.endswith("whatever.sock")
