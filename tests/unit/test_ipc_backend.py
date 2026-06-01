"""Unit tests for `IpcBackend` — probe caching, lazy I/O, error handling.

Mirrors the shape of `test_cli_backend.py`. Uses an in-process Unix socket
server as the "peer" so the whole code path (resolve → probe → cache)
runs for real, without KiCAD.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from kimcp.backends.ipc import IpcBackend
from kimcp.ipc.errors import IpcProbeTimeoutError

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


# -- probe happy paths -----------------------------------------------------


@pytest.mark.asyncio
async def test_probe_true_when_socket_reachable(socket_tmp_path: Path) -> None:
    sock_path = socket_tmp_path / "reachable.sock"
    server = await _start_accepting_server(sock_path)
    try:
        backend = IpcBackend(configured_path=str(sock_path))
        assert await backend.probe() is True
        assert backend.available is True
        assert backend.socket_path == str(sock_path.resolve())
        assert backend.probe_note is None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_false_when_path_missing(tmp_path: Path) -> None:
    # No bind() in this test — regular `tmp_path` is fine.
    backend = IpcBackend(configured_path=str(tmp_path / "nope.sock"))
    assert await backend.probe() is False
    assert backend.available is False
    assert backend.socket_path is None
    # User-actionable hint in the probe note.
    assert backend.probe_note is not None
    assert "no IPC socket discovered" in backend.probe_note


@pytest.mark.asyncio
async def test_probe_false_when_socket_stale(socket_tmp_path: Path) -> None:
    """Path exists but no one is listening → connection refused."""
    import socket as _socket

    sock_path = socket_tmp_path / "stale.sock"
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.bind(str(sock_path))
    finally:
        s.close()

    backend = IpcBackend(configured_path=str(sock_path))
    assert await backend.probe() is False
    assert backend.socket_path == str(sock_path.resolve())
    assert backend.probe_note is not None
    assert "did not accept" in backend.probe_note


@pytest.mark.asyncio
async def test_probe_false_on_probe_timeout(
    socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe timeout collapses to False + diagnostic note (not a raise).

    The backend is the layer that decides whether to surface the raise or
    translate it — dispatchers want a boolean. The typed error is still
    captured in ``probe_note`` so the diagnostic tool can show it.
    """
    sock_path = socket_tmp_path / "wedged.sock"
    # Create the path so resolve succeeds.
    server = await _start_accepting_server(sock_path)
    try:
        # Force the probe to time out by stubbing the probe_socket call.
        async def fake_probe(path: str, *, timeout: float) -> bool:
            raise IpcProbeTimeoutError(
                f"probe exceeded {timeout}s for {path}",
                socket_path=path,
                timeout=timeout,
            )

        monkeypatch.setattr("kimcp.backends.ipc.probe_socket", fake_probe)

        backend = IpcBackend(configured_path=str(sock_path), probe_timeout=0.01)
        assert await backend.probe() is False
        assert backend.available is False
        assert backend.probe_note is not None
        assert "exceeded" in backend.probe_note
    finally:
        server.close()
        await server.wait_closed()


# -- cache semantics -------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_caches_first_result(socket_tmp_path: Path) -> None:
    sock_path = socket_tmp_path / "cache.sock"
    server = await _start_accepting_server(sock_path)
    try:
        backend = IpcBackend(configured_path=str(sock_path))
        first = await backend.probe()
        # Stop the server — a fresh connect would now fail.
        server.close()
        await server.wait_closed()
        second = await backend.probe()
        assert first is True
        # Cached — returns the old verdict even though the socket is now dead.
        assert second is True
    finally:
        # Second close is a no-op if already closed.
        server.close()


@pytest.mark.asyncio
async def test_probe_refresh_rechecks(socket_tmp_path: Path) -> None:
    sock_path = socket_tmp_path / "refresh.sock"
    server = await _start_accepting_server(sock_path)
    backend = IpcBackend(configured_path=str(sock_path))
    assert await backend.probe() is True

    server.close()
    await server.wait_closed()
    # Tear the socket file too so resolve_ipc_socket returns None on refresh
    # — exercises the "socket went away entirely" branch.
    if sock_path.exists():
        sock_path.unlink()

    assert await backend.probe(refresh=True) is False
    assert backend.socket_path is None


# -- __init__ laziness (pins the I3 pattern carried over from CliBackend) --


def test_init_does_not_resolve_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """`__init__` must not touch the filesystem. Resolution is deferred
    to `probe()` so constructing a backend during server startup stays
    pure and never throws surprising OSError."""
    calls: list[str] = []

    def spy_resolve(configured: str) -> None:
        calls.append(configured)
        return None

    monkeypatch.setattr("kimcp.backends.ipc.resolve_ipc_socket", spy_resolve)

    backend = IpcBackend(configured_path="/definitely/not/a/real/path.sock")

    assert calls == []
    assert backend.socket_path is None
    assert backend.probed is False
    assert backend.available is False
    assert backend.probe_note is None


# -- no optional-extra imports at package-load time (pins the contract) ----


def test_ipc_package_does_not_import_optional_extras() -> None:
    """Optional ``[ipc]`` extra deps (``pynng``, ``kipy``) must not be
    pulled in at module-import time. KiMCP is installable without the
    extra; importing either package top-level would break that promise.

    Also pins the negative on ``grpc`` — ADR-0014 mistakenly described
    the transport as gRPC before ADR-0015 corrected it; keeping the
    check here guarantees the core never accidentally regains a
    ``grpcio`` dependency from old docs or AI-assisted code.

    Runs in a fresh subprocess so ``sys.modules`` starts clean — sibling
    tests that ``pytest.importorskip("pynng")`` at module top would
    otherwise poison an in-process check here (collection imports them
    before this test runs).
    """
    import subprocess

    script = (
        "import sys\n"
        "for mod in ('kimcp.ipc', 'kimcp.ipc.errors', 'kimcp.ipc.probe', "
        "'kimcp.ipc.socket', 'kimcp.backends.ipc'):\n"
        "    __import__(mod)\n"
        "bad = [m for m in ('pynng', 'kipy', 'grpc') if m in sys.modules]\n"
        "if bad:\n"
        "    raise SystemExit('leaked optional-extra imports at module top: '\n"
        "                     + ','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        "kimcp.ipc* / kimcp.backends.ipc must not import `pynng` / `kipy` / "
        "`grpc` at module top — transport deps belong to the optional `ipc` "
        "extra and should be imported lazily next to the first real RPC "
        f"call.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
