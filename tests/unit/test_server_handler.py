"""Unit tests for `Server._handle_tools_call` — the dispatch seam (M4).

Everything above the dispatcher (name validation, arguments validation,
unknown-tool handling) is already exercised via `tests/e2e/test_stdio_smoke.py`.
This file focuses on the two *new* behaviors M4 added to that handler:

1. Tools with empty ``preferred_backends`` bypass the dispatcher entirely —
   ``meta.backend_used`` stays ``None`` and ``dispatcher.pick`` is never called.
2. Tools with non-empty ``preferred_backends`` run through the dispatcher —
   ``meta.backend_used`` is populated with the winner, and
   ``RpcError(BACKEND_UNAVAILABLE)`` propagates verbatim when nothing fits.

The tests use synthetic tools declared in-file rather than reaching for
production builtins. The builtins are intentionally dispatcher-agnostic
(see comments in ``kicad_version.py`` / ``kicad_ipc_status.py``) so pinning
the dispatch path against them would silently become a no-op if those
tools' policies ever changed.
"""

from __future__ import annotations

from typing import Literal, cast

import pytest
from pydantic import BaseModel

from kimcp._types import Backend, ToolClass
from kimcp.errors import BACKEND_UNAVAILABLE, RpcError
from kimcp.schemas.envelope import ToolOutput
from kimcp.server import Server
from kimcp.tools.base import Tool


class _Input(BaseModel):
    pass


class _Output(ToolOutput):
    marker: Literal["ran"] = "ran"


class _AgnosticTool(Tool[_Input, _Output]):
    """Synthetic dispatcher-skip case: no preferred backend."""

    name = "_agnostic_probe"
    version = "0.0.1"
    description = "Test-only tool with no preferred backend."
    input_model = _Input
    output_model = _Output
    classification = ToolClass.READ
    preferred_backends = ()

    async def run(self, input: _Input) -> _Output:
        return _Output()


class _SexprTool(Tool[_Input, _Output]):
    """Synthetic dispatcher-hit case.

    Declares SEXPR as preferred because ``SexprBackend.probe()`` is
    unconditionally True (pure-Python, no KiCAD install required) — the
    dispatch path is deterministic on any host.
    """

    name = "_sexpr_probe"
    version = "0.0.1"
    description = "Test-only tool preferring the S-expression backend."
    input_model = _Input
    output_model = _Output
    classification = ToolClass.READ
    preferred_backends = (Backend.SEXPR,)

    async def run(self, input: _Input) -> _Output:
        return _Output()


class _IpcOnlyTool(Tool[_Input, _Output]):
    """Synthetic dispatcher-gate case.

    Declares IPC as the only acceptable backend. Used to prove that when the
    preferred backend isn't marked available, the dispatcher raises and the
    handler propagates without invoking ``run``.
    """

    name = "_ipc_only_probe"
    version = "0.0.1"
    description = "Test-only tool that only runs on IPC."
    input_model = _Input
    output_model = _Output
    classification = ToolClass.READ
    preferred_backends = (Backend.IPC,)

    def __init__(self) -> None:
        self.ran = False

    async def run(self, input: _Input) -> _Output:  # pragma: no cover - must not run
        self.ran = True
        return _Output()


async def _call(server: Server, tool_name: str) -> dict[str, object]:
    """Invoke a tool through the handler's tools/call entrypoint.

    Returns the envelope dict the tool produced. The handler wraps it in
    the MCP-spec ``{content, structuredContent, isError}`` shape — for
    these dispatch-seam tests we want to assert on envelope fields
    directly, so we unwrap once here. The MCP wrapping is covered
    end-to-end in ``tests/e2e/test_stdio_smoke.py``.
    """
    params = {"name": tool_name, "arguments": {}}
    raw = await server._handle_tools_call(params)
    return cast(dict[str, object], raw["structuredContent"])


# -- (1) dispatch-skip: agnostic tool ---------------------------------------


@pytest.mark.asyncio
async def test_agnostic_tool_skips_dispatcher() -> None:
    """Empty `preferred_backends` → no dispatch, `meta.backend_used = None`.

    Spy on `dispatcher.pick` to prove it's never called; avoids a false
    positive where the dispatcher happened to return a winner that matched
    the default None.
    """
    server = Server()
    server.register_tool(_AgnosticTool())

    calls: list[object] = []
    original_pick = server.dispatcher.pick

    def spy_pick(*args: object, **kwargs: object) -> Backend:
        calls.append((args, kwargs))
        return original_pick(*args, **kwargs)  # type: ignore[arg-type]

    server.dispatcher.pick = spy_pick  # type: ignore[method-assign]

    result = await _call(server, "_agnostic_probe")

    assert calls == []
    assert result["meta"]["backend_used"] is None  # type: ignore[index]
    assert result["marker"] == "ran"


# -- (2) dispatch-hit: preferred-backend available --------------------------


@pytest.mark.asyncio
async def test_dispatch_hit_annotates_backend_used() -> None:
    """Non-empty `preferred_backends` + backend available → `meta.backend_used`
    names the winner. Uses SEXPR (always probes True) for determinism."""
    server = Server()
    server.register_tool(_SexprTool())
    # Probe backends so availability is populated. SEXPR flips True; the
    # others don't matter for this test.
    await server.probe_backends()

    result = await _call(server, "_sexpr_probe")

    assert result["meta"]["backend_used"] == "sexpr"  # type: ignore[index]
    assert result["marker"] == "ran"


# -- (3) dispatch-gate: no preferred backend available ----------------------


@pytest.mark.asyncio
async def test_dispatch_raises_when_preferred_unavailable() -> None:
    """Preferred backend not marked available → `RpcError(BACKEND_UNAVAILABLE)`
    propagates without invoking `tool.run`."""
    server = Server()
    tool = _IpcOnlyTool()
    server.register_tool(tool)
    # Explicitly mark IPC unavailable — no probe needed for this path.
    server.availability.mark(Backend.IPC, False)

    with pytest.raises(RpcError) as excinfo:
        await _call(server, "_ipc_only_probe")

    assert excinfo.value.code == BACKEND_UNAVAILABLE
    # data carries the discriminator the dispatcher emits: what was preferred
    # vs. what's actually available. The client uses this to tell the user
    # *which* backend is missing.
    assert excinfo.value.data is not None
    assert excinfo.value.data["preferred"] == ["ipc"]
    # run() must not have been invoked — dispatch gates before the call.
    assert tool.ran is False


# -- (4) dispatch-fallback: second preference wins when first is down -------


class _IpcThenSexprTool(Tool[_Input, _Output]):
    """Synthetic multi-backend tool. IPC preferred but SEXPR acceptable."""

    name = "_ipc_then_sexpr_probe"
    version = "0.0.1"
    description = "Test-only tool preferring IPC then falling back to SEXPR."
    input_model = _Input
    output_model = _Output
    classification = ToolClass.READ
    preferred_backends = (Backend.IPC, Backend.SEXPR)

    async def run(self, input: _Input) -> _Output:
        return _Output()


@pytest.mark.asyncio
async def test_dispatch_falls_back_to_next_preferred() -> None:
    """IPC first-preferred but unavailable → dispatcher falls back to SEXPR.

    Pins the fallback-annotation contract: `meta.backend_used` reflects the
    winner, not the first-preferred. Would catch a regression that silently
    annotated with `preferred_backends[0]` instead of the dispatcher's return.
    """
    server = Server()
    server.register_tool(_IpcThenSexprTool())
    server.availability.mark(Backend.IPC, False)
    server.availability.mark(Backend.SEXPR, True)

    result = await _call(server, "_ipc_then_sexpr_probe")

    assert result["meta"]["backend_used"] == "sexpr"  # type: ignore[index]


# -- (5) duration_ms is populated even on dispatch path ---------------------


@pytest.mark.asyncio
async def test_duration_ms_populated_after_dispatch() -> None:
    """Integration sanity: the existing duration annotation still happens
    after the dispatcher ran — regression guard for accidentally short-
    circuiting the timing logic."""
    server = Server()
    server.register_tool(_SexprTool())
    await server.probe_backends()

    result = await _call(server, "_sexpr_probe")

    # `duration_ms` is an int; small but present on every dispatch path.
    assert isinstance(result["meta"]["duration_ms"], int)  # type: ignore[index]
    assert result["meta"]["duration_ms"] >= 0  # type: ignore[index]
