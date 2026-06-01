"""Unit tests for Server lifecycle: ``aclose`` + ``run_stdio`` hooks (M5.7).

Covers the startup/shutdown contract ``run_stdio`` added in M5.7:

1. ``Server.aclose()`` forwards to ``IpcBackend.aclose`` and is
   idempotent (safe to call twice, and safe on a fresh server where
   the IPC socket was never opened).
2. ``run_stdio`` probes backends BEFORE entering the dispatch loop —
   so the dispatcher has real availability data when the first tool
   call arrives.
3. ``run_stdio`` awaits ``aclose`` in a ``finally`` block — so the
   long-lived ``pynng.Req0`` socket from an IPC-backed tool call gets
   torn down on shutdown rather than leaking.

Uses the in-memory transport from ``conftest`` to drive ``dispatch_loop``
without a subprocess. ``StdioTransport.create`` is monkeypatched to
hand back that transport so we exercise the real ``run_stdio`` body.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from kimcp._types import Backend
from kimcp.config import Config
from kimcp.server import Server

# -- Server.aclose semantics -----------------------------------------------


@pytest.mark.asyncio
async def test_aclose_forwards_to_ipc_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Server.aclose`` delegates to ``IpcBackend.aclose`` exactly once per call.

    Spy on the backend's aclose so the test is independent of whether
    pynng is installed — no real socket is required to prove the wiring.
    """
    server = Server()

    calls: list[None] = []

    async def spy_aclose() -> None:
        calls.append(None)

    monkeypatch.setattr(server._ipc_backend, "aclose", spy_aclose)

    await server.aclose()
    assert len(calls) == 1

    # Idempotent: calling twice is not a bug, it's how the stdio finally
    # blocks behave in certain shutdown races.
    await server.aclose()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_aclose_on_fresh_server_is_noop() -> None:
    """Never-called server: ``aclose`` must not throw or touch the filesystem.

    Guards against a future refactor that assumes the IPC socket is
    already open (e.g. dereferencing ``self._ipc_backend._sock`` without
    a None check).
    """
    server = Server()
    # No register_tool, no probe_backends, no call path exercised.
    await server.aclose()
    # Reaching here means no raise — exactly the contract.


# -- run_stdio wires probe + aclose ----------------------------------------


class _RecordingTransport:
    """Transport stub that drains a fixed incoming list, records sent.

    Intentionally narrow: mimics the interface ``dispatch_loop`` uses —
    ``read_message``, ``write_message``, ``close``. No more.
    """

    def __init__(self, incoming: Iterable[dict[str, Any]]) -> None:
        self._incoming = list(incoming)
        self.sent: list[dict[str, Any]] = []
        self.close_calls = 0

    async def read_message(self) -> dict[str, Any] | None:
        if not self._incoming:
            return None
        return self._incoming.pop(0)

    async def write_message(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_run_stdio_probes_before_dispatch_and_acloses_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_stdio must: probe, then dispatch, then aclose + transport.close.

    Pins the ordering (not just the set of calls). Getting the order
    wrong silently regresses dispatcher-gated tools — their first call
    would BACKEND_UNAVAILABLE because probe hadn't happened yet.
    """
    server = Server()

    order: list[str] = []

    # Spy on probe_backends and aclose — keep their real return shapes.
    real_probe = server.probe_backends

    async def spy_probe() -> dict[str, bool]:
        order.append("probe")
        return await real_probe()

    async def spy_aclose() -> None:
        order.append("aclose")

    monkeypatch.setattr(server, "probe_backends", spy_probe)
    monkeypatch.setattr(server, "aclose", spy_aclose)

    # Transport with a single initialize request. When dispatch_loop
    # sends the reply and then sees EOF, it returns — we then land in
    # the finally block where aclose runs.
    transport = _RecordingTransport(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        ]
    )

    # Intercept the StdioTransport.create coroutine from inside run_stdio.
    # The import is lazy (`from kimcp.transport.stdio import StdioTransport`
    # happens inside the method) so we patch the module the name resolves
    # from.
    from kimcp.transport import stdio as stdio_mod

    async def fake_create() -> _RecordingTransport:
        order.append("transport_create")
        return transport

    monkeypatch.setattr(stdio_mod.StdioTransport, "create", classmethod(lambda cls: fake_create()))

    await server.run_stdio()

    # Ordering contract:
    #   1. probe (availability populated)
    #   2. transport_create (stdio streams attached)
    #   3. aclose (per-session resources released)
    # dispatch_loop work happens between 2 and 3 but we don't spy on it
    # since it's transparent here.
    assert order == ["probe", "transport_create", "aclose"]
    # Transport close runs after aclose (finally-within-finally). No
    # direct order spy on it, but the call count proves it fired.
    assert transport.close_calls == 1
    # Dispatch happened — the initialize request got a reply.
    assert len(transport.sent) == 1
    assert transport.sent[0]["id"] == 1
    assert "result" in transport.sent[0]


@pytest.mark.asyncio
async def test_run_stdio_closes_transport_even_if_aclose_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested-finally contract: a buggy aclose must NOT prevent transport.close.

    If this test breaks, the shutdown path is leaking stdio pipes on
    unusual failure modes — at minimum a CI test-runner fd leak, at
    worst a wedged MCP client waiting on a pipe that never closes.
    """
    server = Server()

    async def boom() -> None:
        raise RuntimeError("synthetic aclose failure")

    monkeypatch.setattr(server, "aclose", boom)

    transport = _RecordingTransport([])

    from kimcp.transport import stdio as stdio_mod

    async def fake_create() -> _RecordingTransport:
        return transport

    monkeypatch.setattr(stdio_mod.StdioTransport, "create", classmethod(lambda cls: fake_create()))

    with pytest.raises(RuntimeError, match="synthetic aclose failure"):
        await server.run_stdio()

    # transport.close fired despite the aclose explosion.
    assert transport.close_calls == 1


@pytest.mark.asyncio
async def test_run_stdio_probe_populates_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After run_stdio finishes, the availability matrix reflects real probes.

    Cross-check against the raw probe result: both must agree on which
    backends flipped True. Catches the regression where
    ``run_stdio`` probes but forgets to plumb results into
    ``self.availability``.
    """
    server = Server()
    transport = _RecordingTransport([])

    from kimcp.transport import stdio as stdio_mod

    async def fake_create() -> _RecordingTransport:
        return transport

    monkeypatch.setattr(stdio_mod.StdioTransport, "create", classmethod(lambda cls: fake_create()))

    await server.run_stdio()

    # SexprBackend always probes True (pure-Python). That's the one
    # deterministic signal we can pin on any host. The others depend on
    # whether KiCAD / kicad-cli / a live socket happen to exist, so we
    # only assert the shape — not the values.
    availability = server.availability.as_dict()
    assert availability.get(Backend.SEXPR.value) is True
    assert Backend.CLI.value in availability
    assert Backend.IPC.value in availability
    assert Backend.SWIG.value in availability


# -- performance.file_watch wiring -----------------------------------------


def _make_config(file_watch: bool) -> Config:
    """Build a Config with only the file_watch knob toggled."""
    return Config.model_validate({"performance": {"file_watch": file_watch}})


def test_file_watch_enabled_by_default_creates_invalidator(tmp_path: Path) -> None:
    """Default config (file_watch=True) spins up a CacheInvalidator.

    Pins the wiring contract: operators relying on the default get eager
    eviction. A regression where the wiring silently disappears would
    degrade cache hit-rate accuracy without anyone noticing.
    """
    server = Server(config=_make_config(True), project_root=tmp_path)
    assert server._cache_invalidator is not None
    assert tmp_path.resolve() in server._cache_invalidator.watched_paths()


def test_file_watch_disabled_leaves_invalidator_none(tmp_path: Path) -> None:
    """``performance.file_watch=false`` is the opt-out — no thread spawned."""
    server = Server(config=_make_config(False), project_root=tmp_path)
    assert server._cache_invalidator is None
    # And the SexprBackend knows the watcher is absent, so its aclose
    # is a clean no-op.
    assert server._sexpr_backend.watcher is None


@pytest.mark.asyncio
async def test_probe_backends_starts_invalidator(tmp_path: Path) -> None:
    """``probe_backends`` is the hook that kicks off the observer thread."""
    server = Server(config=_make_config(True), project_root=tmp_path)
    assert server._cache_invalidator is not None
    assert server._cache_invalidator.is_running is False
    await server.probe_backends()
    assert server._cache_invalidator.is_running is True
    # Tidy up so the test doesn't leak a thread.
    await server.aclose()


@pytest.mark.asyncio
async def test_aclose_stops_invalidator(tmp_path: Path) -> None:
    """``Server.aclose`` delegates to SexprBackend.aclose → watcher.stop.

    Closes the same leak the IpcBackend ``aclose`` guard closes: a
    long-running host (HTTP transport, pytest repeat) would otherwise
    accumulate daemon threads on each restart.
    """
    server = Server(config=_make_config(True), project_root=tmp_path)
    assert server._cache_invalidator is not None
    await server.probe_backends()
    assert server._cache_invalidator.is_running is True

    await server.aclose()
    assert server._cache_invalidator.is_running is False


@pytest.mark.asyncio
async def test_aclose_without_watcher_is_clean(tmp_path: Path) -> None:
    """``file_watch=false`` still allows aclose to complete without raising."""
    server = Server(config=_make_config(False), project_root=tmp_path)
    # No probe / no run_stdio — just straight to aclose, the minimal
    # lifecycle an admin entry-point might exercise.
    await server.aclose()
