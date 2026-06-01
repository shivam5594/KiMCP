"""Unit tests for the `ipc_get_version` built-in tool.

Covers the five output states (ok / not_found / unreachable / call_error /
extra_missing) by pointing the injected IpcBackend at:

* An in-process pynng.Rep0 stub that plays KiCAD → ``ok``.
* A missing path → ``not_found``.
* A path whose peer dies mid-session → ``unreachable``.
* A stub that returns ``AS_BAD_REQUEST`` → ``call_error``.
* A process where ``pynng`` is absent from ``sys.modules`` → ``extra_missing``.

Shares the ``StubKicad`` shape with ``test_ipc_backend_call.py`` — kept
inline here rather than hoisted into a shared fixture module because
only two tests need it, and the stub is deliberately small (a real
shared fake of KiCAD would be out of scope for unit tests).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from kimcp.backends.ipc import IpcBackend
from kimcp.tools.builtin.ipc_get_version import (
    IpcGetVersionInput,
    IpcGetVersionOutput,
    IpcGetVersionTool,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="pynng+kipy Unix-socket tests; Windows named-pipe path is separate.",
)

# Module-top importorskip so `pytest` on a minimal install (no `[ipc]`
# extra) skips this whole file at collection rather than erroring. The
# import-contract guard lives in `test_ipc_backend.py`.
pynng = pytest.importorskip("pynng")
kipy_envelope = pytest.importorskip("kipy.proto.common.envelope_pb2")
kipy_base_commands = pytest.importorskip("kipy.proto.common.commands.base_commands_pb2")
kipy_base_types = pytest.importorskip("kipy.proto.common.types.base_types_pb2")


# -- in-process KiCAD stub --------------------------------------------------


class StubKicad:
    """A ``pynng.Rep0`` listener that plays KiCAD for a single test.

    Same contract as the stub in ``test_ipc_backend_call.py``: tests set
    ``responder`` to a ``(ApiRequest) -> ApiResponse`` callable, and the
    server loop drives it until ``stop()``.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.received: list[Any] = []
        self.responder: Any = None
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
    resp = kipy_envelope.ApiResponse()
    resp.status.status = kipy_envelope.ApiStatusCode.AS_OK
    resp.message.Pack(inner_message)
    resp.header.kicad_token = token
    return resp


def _error_response(*, code: int, message: str) -> Any:
    resp = kipy_envelope.ApiResponse()
    resp.status.status = code
    resp.status.error_message = message
    return resp


@pytest.fixture
async def stub_kicad(socket_tmp_path: Path):
    """Async fixture yielding a started `StubKicad` + the socket path."""
    sock_path = socket_tmp_path / "kicad.sock"
    stub = StubKicad(url=f"ipc://{sock_path}")
    await stub.start()
    try:
        yield stub, sock_path
    finally:
        await stub.stop()


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_ok_when_server_answers(stub_kicad: tuple[StubKicad, Path]) -> None:
    """Stub responds with a KiCadVersion → tool unpacks it into the envelope."""
    stub, sock_path = stub_kicad

    fake_version = kipy_base_types.KiCadVersion(
        major=10, minor=0, patch=1, full_version="10.0.1-stub"
    )
    get_version_resp = kipy_base_commands.GetVersionResponse()
    get_version_resp.version.CopyFrom(fake_version)
    stub.responder = lambda _req: _ok_response(get_version_resp, token="session-token")

    backend = IpcBackend(configured_path=str(sock_path))
    tool = IpcGetVersionTool()
    tool.set_ipc_backend(backend)
    try:
        out = await tool.run(IpcGetVersionInput())
        assert isinstance(out, IpcGetVersionOutput)
        assert out.status == "ok"
        assert out.version == "10.0.1"
        assert out.version_raw == "10.0.1-stub"
        assert out.major == 10
        assert out.minor == 0
        assert out.patch == 1
        assert out.note is None
        # Socket path is whatever the backend resolved.
        assert out.socket_path == str(sock_path.resolve())
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_status_ok_collapses_empty_full_version_to_none(
    stub_kicad: tuple[StubKicad, Path],
) -> None:
    """Proto default for a string field is ''; we prefer None for 'unknown'."""
    stub, sock_path = stub_kicad

    # Deliberately omit `full_version`; proto defaults it to "".
    fake_version = kipy_base_types.KiCadVersion(major=9, minor=0, patch=0)
    get_version_resp = kipy_base_commands.GetVersionResponse()
    get_version_resp.version.CopyFrom(fake_version)
    stub.responder = lambda _req: _ok_response(get_version_resp)

    backend = IpcBackend(configured_path=str(sock_path))
    tool = IpcGetVersionTool()
    tool.set_ipc_backend(backend)
    try:
        out = await tool.run(IpcGetVersionInput())
        assert out.status == "ok"
        assert out.version == "9.0.0"
        assert out.version_raw is None  # not the empty string
    finally:
        await backend.aclose()


# -- error paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_status_not_found_when_socket_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resolve_ipc_socket` returns None → status='not_found', socket_path=None.

    Must NOT say ``unreachable`` — that's the ``kicad_ipc_status`` contract,
    and diverging here would make the two tools confusing when read
    side-by-side.
    """
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setattr("kimcp.ipc.socket._platform_candidates", lambda: ())

    backend = IpcBackend(configured_path="auto", call_timeout=0.25)
    tool = IpcGetVersionTool()
    tool.set_ipc_backend(backend)
    try:
        out = await tool.run(IpcGetVersionInput())
        assert out.status == "not_found"
        assert out.socket_path is None
        assert out.version is None
        assert out.major is None
        assert out.note is not None
        # Error text should point the user somewhere.
        assert "KiCAD" in out.note or "ipc_socket" in out.note
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_status_unreachable_when_peer_gone(socket_tmp_path: Path) -> None:
    """Peer was up at first call, then gone — reconnect fails → unreachable.

    Exercises the branch where `socket_path` IS populated (resolved
    successfully at least once) but dial/send fails. Distinct from
    `not_found` where resolution itself never succeeded.
    """
    sock_path = socket_tmp_path / "gone.sock"
    stub = StubKicad(url=f"ipc://{sock_path}")
    stub.responder = lambda _req: _ok_response(
        kipy_base_commands.GetVersionResponse(), token="tok"
    )
    await stub.start()

    backend = IpcBackend(configured_path=str(sock_path), call_timeout=0.25)
    tool = IpcGetVersionTool()
    tool.set_ipc_backend(backend)
    try:
        # First call warms the socket + populates `socket_path`.
        first = await tool.run(IpcGetVersionInput())
        assert first.status == "ok"

        # Kill the peer for good.
        await stub.stop()
        if sock_path.exists():
            sock_path.unlink()

        # Second call: transport fails → reconnect fails → 'unreachable'.
        out = await tool.run(IpcGetVersionInput())
        assert out.status == "unreachable"
        # The backend kept `socket_path` populated from the successful
        # first-call dial — that's what distinguishes this from 'not_found'.
        assert out.socket_path is not None
        assert out.version is None
        assert out.note is not None
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_status_call_error_when_server_rejects(
    stub_kicad: tuple[StubKicad, Path],
) -> None:
    """Stub returns AS_BAD_REQUEST → status='call_error', note preserves it."""
    stub, sock_path = stub_kicad
    stub.responder = lambda _req: _error_response(
        code=kipy_envelope.ApiStatusCode.AS_BAD_REQUEST,
        message="no such command",
    )

    backend = IpcBackend(configured_path=str(sock_path))
    tool = IpcGetVersionTool()
    tool.set_ipc_backend(backend)
    try:
        out = await tool.run(IpcGetVersionInput())
        assert out.status == "call_error"
        assert out.socket_path == str(sock_path.resolve())
        assert out.version is None
        assert out.major is None
        assert out.note is not None
        # KiCAD's error_message should surface somewhere in the note so
        # the operator can tell "bad request" from "busy" / "not ready".
        assert "no such command" in out.note
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_status_extra_missing_when_pynng_absent(
    socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poison pynng in sys.modules → backend raises IpcError → status='extra_missing'.

    The tool's job is to translate that into a graceful envelope instead
    of propagating the raise — this is the shape the operator sees when
    KiMCP was installed without ``pip install 'kimcp[ipc]'``.
    """
    sock_path = socket_tmp_path / "noextra.sock"
    monkeypatch.setitem(sys.modules, "pynng", None)

    backend = IpcBackend(configured_path=str(sock_path))
    tool = IpcGetVersionTool()
    tool.set_ipc_backend(backend)
    try:
        out = await tool.run(IpcGetVersionInput())
        assert out.status == "extra_missing"
        # Backend never resolved the path (pynng import fails first), so
        # socket_path stays None.
        assert out.socket_path is None
        assert out.version is None
        assert out.note is not None
        # Actionable hint: mention the extra.
        assert "[ipc]" in out.note or "ipc]" in out.note
    finally:
        await backend.aclose()


# -- envelope-shape drift (KiCAD 10 hardening) -----------------------------


class _FakeBackend:
    """Minimal IpcBackend-shaped stand-in that returns whatever response
    object the test stages. Bypasses the pynng wire entirely — we only
    need to exercise the tool's response-parsing branch, not the transport.
    """

    socket_path = "/tmp/fake-kicad.sock"

    def __init__(self, response: Any) -> None:
        self._response = response

    async def call(self, _request: Any, _response_type: Any) -> Any:
        return self._response

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_status_call_error_on_unexpected_response_shape() -> None:
    """Proto field drift (``.version.major`` missing) collapses to call_error.

    Pins the KiCAD-10 hardening contract: if a future KiCAD restructures
    the ``GetVersionResponse`` envelope in a way our kicad-python pin
    doesn't understand, the tool must NOT crash — it folds into the
    existing ``call_error`` status so callers see one coherent failure
    mode instead of a raw ``AttributeError`` escape.
    """
    # Response shaped like a protobuf reply whose `.version` object lost
    # its `.major` field (simulating a future schema rename). The tool
    # catches AttributeError via its defensive block.
    import types
    fake_response = types.SimpleNamespace(
        version=types.SimpleNamespace(full_version="x.y.z")
    )

    tool = IpcGetVersionTool()
    tool.set_ipc_backend(cast(IpcBackend, _FakeBackend(fake_response)))

    out = await tool.run(IpcGetVersionInput())
    assert out.status == "call_error"
    assert out.version is None
    assert out.major is None
    assert out.note is not None
    assert "schema drift" in out.note.lower() or "unexpected" in out.note.lower()


# -- injection / entry-point behavior --------------------------------------


@pytest.mark.asyncio
async def test_tool_without_injection_builds_default_backend() -> None:
    """Bare entry-point load: ``run`` must not crash even without the server
    wiring up ``set_ipc_backend``. Status is whatever the host exposes.
    """
    tool = IpcGetVersionTool()
    out = await tool.run(IpcGetVersionInput())
    assert out.status in {"ok", "not_found", "unreachable", "call_error", "extra_missing"}


def test_tool_declares_ipc_preferred_backend() -> None:
    """Pins the architectural choice: this tool is IPC-backed, and the
    dispatcher should gate on it. If someone later sets
    ``preferred_backends = ()`` to make the tool run "no matter what",
    re-read the module docstring — the status-envelope-vs-dispatcher
    rationale is load-bearing.
    """
    from kimcp._types import Backend

    assert IpcGetVersionTool.preferred_backends == (Backend.IPC,)
