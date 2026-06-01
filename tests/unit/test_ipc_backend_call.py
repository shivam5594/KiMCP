"""End-to-end tests for `IpcBackend.call()` against an in-process pynng stub.

Stands up a real ``pynng.Rep0`` listener in the same event loop as the
backend so the whole path (dial → envelope pack → asend → arecv_msg →
envelope parse → Unpack) runs for real without KiCAD. Covers:

* Happy paths — Ping → Empty, GetVersion → GetVersionResponse.
* Session-token handshake — empty on first request, echoed on second.
* Non-OK ``ApiStatusCode`` → :class:`IpcCallError`.
* Unpackable reply → :class:`IpcCallError` (type-skew signal).
* Reconnect-once on ``NNGException`` — transparent stutter across KiCAD
  restart vs hard failure when the peer is gone for good.
* ``aclose()`` teardown + idempotence.
* Missing ``[ipc]`` extra → :class:`IpcError` at call-site.

Unix-socket-based — the ``pynng`` + ``kipy.proto.*`` deps are required
here and skipped on Windows (the Windows named-pipe path will land with
a separate live-KiCAD integration suite in a later milestone).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from kimcp.backends.ipc import IpcBackend
from kimcp.ipc.errors import IpcCallError, IpcError, IpcSocketUnreachableError

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="pynng+kipy Unix-socket tests; Windows named-pipe path is separate.",
)

# Imports gated on the `[ipc]` extra. Skip the whole module if either is
# absent so `pytest` on a minimal install doesn't error out during
# collection — the import-contract test in `test_ipc_backend.py` is the
# one that enforces the "must not import at module top" invariant.
# Using `importorskip` (which returns the module) instead of a plain
# `import` keeps the module-top safe when the extra is absent.
pynng = pytest.importorskip("pynng")
kipy_envelope = pytest.importorskip("kipy.proto.common.envelope_pb2")
kipy_base_commands = pytest.importorskip("kipy.proto.common.commands.base_commands_pb2")
kipy_base_types = pytest.importorskip("kipy.proto.common.types.base_types_pb2")
empty_pb2 = pytest.importorskip("google.protobuf.empty_pb2")


# -- in-process KiCAD stub --------------------------------------------------


class StubKicad:
    """A ``pynng.Rep0`` listener that plays KiCAD for a single test.

    The test configures ``responder`` — a callable ``(ApiRequest) -> ApiResponse``
    — and the stub serves every incoming envelope until ``aclose()`` is
    called. ``received`` captures parsed requests so tests can assert on
    header fields (``kicad_token``, ``client_name``) and on the packed
    inner command.

    Kept small on purpose: the stub is a fixture, not a reusable fake of
    KiCAD. If a future test needs richer behavior, compose it from a new
    responder callable rather than growing this class.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.received: list[Any] = []  # list[ApiRequest]
        self.responder: Any = None  # (ApiRequest) -> ApiResponse
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._server = pynng.Rep0()
        self._server.listen(self.url)
        self._task = asyncio.create_task(self._serve())

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._task is not None:
            # Give the serve loop a beat to exit on the close.
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _serve(self) -> None:
        assert self._server is not None
        while True:
            try:
                msg = await self._server.arecv_msg()
            except pynng.exceptions.Closed:
                return
            except pynng.exceptions.NNGException:
                return
            req = kipy_envelope.ApiRequest()
            req.ParseFromString(msg.bytes)
            self.received.append(req)
            assert self.responder is not None, "test forgot to set StubKicad.responder"
            resp = self.responder(req)
            try:
                await self._server.asend(resp.SerializeToString())
            except pynng.exceptions.Closed:
                return
            except pynng.exceptions.NNGException:
                return


def _ok_response(inner_message: Any, *, token: str = "") -> Any:
    """Build an ``AS_OK`` ``ApiResponse`` wrapping ``inner_message``."""
    resp = kipy_envelope.ApiResponse()
    resp.status.status = kipy_envelope.ApiStatusCode.AS_OK
    resp.message.Pack(inner_message)
    resp.header.kicad_token = token
    return resp


def _error_response(*, code: int, message: str) -> Any:
    """Build a non-OK ``ApiResponse`` with the given code / message."""
    resp = kipy_envelope.ApiResponse()
    resp.status.status = code
    resp.status.error_message = message
    return resp


@pytest.fixture
async def stub_kicad(socket_tmp_path: Path):
    """Async fixture that yields a started `StubKicad`.

    Using ``socket_tmp_path`` (not ``tmp_path``) so the ``sun_path``
    stays under the AF_UNIX ~104-char limit on macOS.
    """
    sock_path = socket_tmp_path / "kicad.sock"
    stub = StubKicad(url=f"ipc://{sock_path}")
    await stub.start()
    try:
        yield stub, sock_path
    finally:
        await stub.stop()


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_call_ping_ok(stub_kicad: tuple[StubKicad, Path]) -> None:
    """Round-trip a ``Ping`` command → ``Empty`` response."""
    stub, sock_path = stub_kicad

    def responder(_req: Any) -> Any:
        return _ok_response(empty_pb2.Empty(), token="token-ping")

    stub.responder = responder

    backend = IpcBackend(configured_path=str(sock_path))
    try:
        resp = await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
        assert isinstance(resp, empty_pb2.Empty)
        # Token cached on the backend after the first round trip.
        assert backend.kicad_token == "token-ping"
        assert backend.connected is True
        assert backend.socket_path == str(sock_path.resolve())

        # The stub received our envelope with the expected header fields.
        assert len(stub.received) == 1
        sent = stub.received[0]
        assert sent.header.client_name == backend.client_name
        # First request goes out with an empty token (handshake contract).
        assert sent.header.kicad_token == ""
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_call_get_version_ok(stub_kicad: tuple[StubKicad, Path]) -> None:
    """Round-trip GetVersion and verify the ``KiCadVersion`` payload survives."""
    stub, sock_path = stub_kicad

    fake_version = kipy_base_types.KiCadVersion(
        major=10, minor=0, patch=0, full_version="10.0.0-stub"
    )
    get_version_resp = kipy_base_commands.GetVersionResponse()
    get_version_resp.version.CopyFrom(fake_version)

    def responder(_req: Any) -> Any:
        return _ok_response(get_version_resp, token="token-gv")

    stub.responder = responder

    backend = IpcBackend(configured_path=str(sock_path))
    try:
        resp = await backend.call(
            kipy_base_commands.GetVersion(),
            kipy_base_commands.GetVersionResponse,
        )
        assert resp.version.major == 10
        assert resp.version.minor == 0
        assert resp.version.patch == 0
        assert resp.version.full_version == "10.0.0-stub"
    finally:
        await backend.aclose()


# -- session token handshake -----------------------------------------------


@pytest.mark.asyncio
async def test_call_echoes_token_on_second_call(stub_kicad: tuple[StubKicad, Path]) -> None:
    """First reply hands the backend a token; the second request carries it."""
    stub, sock_path = stub_kicad

    # Reply token is whatever KiCAD decides; we pin it so we can assert on
    # the echoed value without racing the handshake.
    def responder(_req: Any) -> Any:
        return _ok_response(empty_pb2.Empty(), token="session-42")

    stub.responder = responder

    backend = IpcBackend(configured_path=str(sock_path))
    try:
        await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
        await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)

        assert len(stub.received) == 2
        assert stub.received[0].header.kicad_token == ""
        assert stub.received[1].header.kicad_token == "session-42"
        # Client name is stable across the session.
        assert stub.received[0].header.client_name == backend.client_name
        assert stub.received[1].header.client_name == backend.client_name
    finally:
        await backend.aclose()


# -- error paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_call_raises_on_non_ok_status(stub_kicad: tuple[StubKicad, Path]) -> None:
    """Non-OK status collapses to ``IpcCallError`` with code + message preserved."""
    stub, sock_path = stub_kicad

    def responder(_req: Any) -> Any:
        return _error_response(
            code=kipy_envelope.ApiStatusCode.AS_BAD_REQUEST,
            message="unknown command",
        )

    stub.responder = responder

    backend = IpcBackend(configured_path=str(sock_path))
    try:
        with pytest.raises(IpcCallError) as exc_info:
            await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)

        # AS_BAD_REQUEST is the 3rd enum value; pin on the numeric so the
        # test doesn't require importing ApiStatusCode at the caller.
        assert exc_info.value.status_code == kipy_envelope.ApiStatusCode.AS_BAD_REQUEST
        assert exc_info.value.error_message == "unknown command"
        # Message should mention the command name for diagnosability.
        assert "Ping" in str(exc_info.value)
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_call_raises_on_unpackable_response(stub_kicad: tuple[StubKicad, Path]) -> None:
    """AS_OK with a mismatched inner type → ``IpcCallError`` (skew signal)."""
    stub, sock_path = stub_kicad

    def responder(_req: Any) -> Any:
        # Pack a KiCadVersion but the caller will ask for Empty — Unpack
        # returns False because the type_urls don't match.
        version = kipy_base_types.KiCadVersion(
            major=10, minor=0, patch=0, full_version="10.0.0"
        )
        return _ok_response(version, token="token-skew")

    stub.responder = responder

    backend = IpcBackend(configured_path=str(sock_path))
    try:
        with pytest.raises(IpcCallError) as exc_info:
            await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
        assert "could not unpack" in str(exc_info.value)
        # The type_url mismatch is the headline — make sure the error
        # surfaces something specific enough to diagnose.
        assert "Empty" in str(exc_info.value)
    finally:
        await backend.aclose()


# -- transport failure / reconnect -----------------------------------------


@pytest.mark.asyncio
async def test_call_reconnects_once_on_transport_failure(
    stub_kicad: tuple[StubKicad, Path],
) -> None:
    """Verify the reconnect-once path fires when asend hits a broken socket.

    Triggering a realistic NNGException is fiddly: pynng's Req0 dialer
    auto-reconnects under the covers, so a mere KiCAD restart tends to
    be invisible to our send-side. We therefore force the failure
    deterministically by closing the backend's cached ``pynng.Req0``
    socket out-of-band — the next ``asend`` raises
    ``pynng.exceptions.Closed`` (an ``NNGException`` subclass), which is
    exactly the signal our retry loop is written to handle.

    Post-retry expectations:
      * The token was cleared before the retry (so the handshake
        restarts cleanly).
      * The retry's envelope carried an empty token.
      * The call ultimately succeeded (``connected is True`` again).
    """
    stub, sock_path = stub_kicad
    stub.responder = lambda _req: _ok_response(empty_pb2.Empty(), token="fresh")

    backend = IpcBackend(configured_path=str(sock_path), call_timeout=1.0)
    try:
        await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
        assert backend.connected is True
        assert backend.kicad_token == "fresh"
        assert len(stub.received) == 1
        assert stub.received[0].header.kicad_token == ""

        # Simulate a transport-level failure: close the cached pynng
        # socket behind the backend's back. The object reference is
        # still live, but the next asend will raise Closed.
        # `_sock` is typed `object | None` on the backend (so the module
        # stays importable without pynng) — here we know it's a
        # pynng.Req0 so closing it is safe.
        assert backend._sock is not None
        backend._sock.close()  # type: ignore[attr-defined]

        # Next call: retry loop catches the NNGException, re-dials,
        # re-handshakes, succeeds.
        await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
        assert backend.connected is True
        # Stub has seen TWO completed round-trips now: the original and
        # the reconnect-retry. The retry envelope carried a cleared
        # token — verifying the "fresh handshake" contract.
        assert len(stub.received) == 2
        assert stub.received[1].header.kicad_token == ""
        # After the retry's reply lands, we have a new cached token.
        assert backend.kicad_token == "fresh"
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_call_raises_when_peer_is_permanently_gone(socket_tmp_path: Path) -> None:
    """If both the initial send and the reconnect fail, surface a hard error."""
    sock_path = socket_tmp_path / "gone.sock"
    url = f"ipc://{sock_path}"

    # Stand up the stub just long enough for the first call to succeed so
    # the backend has a live socket — the reconnect path is what we're
    # exercising, not the initial-dial failure path.
    stub = StubKicad(url)
    stub.responder = lambda _req: _ok_response(empty_pb2.Empty(), token="tok")
    await stub.start()

    backend = IpcBackend(configured_path=str(sock_path), call_timeout=0.25)
    try:
        await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)

        # Kill the peer for good.
        await stub.stop()
        if sock_path.exists():
            sock_path.unlink()

        # Second call: asend/arecv fails → reconnect attempt → dial fails
        # because nothing is listening → IpcSocketUnreachableError.
        with pytest.raises(IpcSocketUnreachableError):
            await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
        assert backend.connected is False
    finally:
        await backend.aclose()


# -- aclose ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_socket_and_clears_token(
    stub_kicad: tuple[StubKicad, Path],
) -> None:
    stub, sock_path = stub_kicad
    stub.responder = lambda _req: _ok_response(empty_pb2.Empty(), token="t")

    backend = IpcBackend(configured_path=str(sock_path))
    await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
    # Bundle pre/post state into tuples so mypy treats each side as a
    # fresh read — an `assert backend.connected is True` would narrow
    # the property to `Literal[True]` and flag the post-aclose check as
    # unreachable.
    pre_close = (backend.connected, backend.kicad_token)
    assert pre_close == (True, "t")

    await backend.aclose()
    post_close = (backend.connected, backend.kicad_token)
    assert post_close == (False, "")

    # And aclose is idempotent — double-close is a no-op, not a raise.
    await backend.aclose()


@pytest.mark.asyncio
async def test_aclose_on_fresh_backend_is_noop() -> None:
    """Never-called backend: aclose() must not throw or touch the filesystem."""
    backend = IpcBackend(configured_path="/not/going/to/touch/this.sock")
    await backend.aclose()
    assert backend.connected is False


# -- dial failure (no peer to begin with) ----------------------------------


@pytest.mark.asyncio
async def test_call_raises_when_initial_dial_fails(socket_tmp_path: Path) -> None:
    """No peer ever listened — the first ``call()`` must surface a typed error."""
    sock_path = socket_tmp_path / "never.sock"
    backend = IpcBackend(configured_path=str(sock_path), call_timeout=0.25)
    try:
        with pytest.raises(IpcSocketUnreachableError):
            await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
        assert backend.connected is False
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_call_raises_when_socket_not_discoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolve_ipc_socket`` returns None → IpcSocketUnreachableError, no dial."""
    # `configured_path="auto"` + no env + no platform candidates = no resolution.
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setattr("kimcp.ipc.socket._platform_candidates", lambda: ())

    backend = IpcBackend(configured_path="auto", call_timeout=0.25)
    try:
        with pytest.raises(IpcSocketUnreachableError) as exc_info:
            await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
        # The error message should mention KiCAD / ipc_socket so the user
        # knows where to look.
        assert "KiCAD" in str(exc_info.value) or "ipc_socket" in str(exc_info.value)
    finally:
        await backend.aclose()


# -- missing [ipc] extra ---------------------------------------------------


@pytest.mark.asyncio
async def test_call_raises_ipc_error_when_pynng_missing(
    socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pynng → ``IpcError`` with an installable hint, not ImportError.

    Pokes ``sys.modules`` to simulate an install without the ``[ipc]``
    extra. Poisoning the module to ``None`` makes ``import pynng`` raise
    ImportError inside ``call()``, which the backend must translate to
    a user-actionable ``IpcError``.
    """
    sock_path = socket_tmp_path / "noextra.sock"
    # Snapshot and remove pynng from sys.modules so a fresh import fails.
    monkeypatch.setitem(sys.modules, "pynng", None)

    backend = IpcBackend(configured_path=str(sock_path))
    try:
        with pytest.raises(IpcError) as exc_info:
            await backend.call(kipy_base_commands.Ping(), empty_pb2.Empty)
        # Must NOT be a subclass — IpcCallError / IpcSocketUnreachableError
        # are reachability signals, not config-problem signals.
        assert not isinstance(exc_info.value, IpcCallError)
        assert not isinstance(exc_info.value, IpcSocketUnreachableError)
        # Actionable message: tell the user to install the extra.
        assert "[ipc]" in str(exc_info.value) or "ipc]" in str(exc_info.value)
    finally:
        # backend never connected, aclose is still a no-op.
        await backend.aclose()
