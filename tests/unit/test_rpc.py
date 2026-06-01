"""Unit tests for the hand-rolled JSON-RPC handler."""

from __future__ import annotations

import pytest

from kimcp.errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    VALIDATION_ERROR,
    RpcError,
)
from kimcp.rpc import JSONRPC_VERSION, JsonRpcHandler, dispatch_loop


@pytest.mark.asyncio
async def test_basic_method_call() -> None:
    handler = JsonRpcHandler()

    async def echo(params):
        return {"echoed": params.get("value")}

    handler.register("echo", echo)
    resp = await handler.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "echo", "params": {"value": 5}}
    )
    assert resp == {"jsonrpc": JSONRPC_VERSION, "id": 1, "result": {"echoed": 5}}


@pytest.mark.asyncio
async def test_invalid_jsonrpc_version() -> None:
    handler = JsonRpcHandler()
    resp = await handler.handle({"jsonrpc": "1.0", "id": 1, "method": "anything"})
    assert resp is not None and resp["error"]["code"] == INVALID_REQUEST


@pytest.mark.asyncio
async def test_unknown_method() -> None:
    handler = JsonRpcHandler()
    resp = await handler.handle({"jsonrpc": "2.0", "id": 9, "method": "missing"})
    assert resp is not None and resp["error"]["code"] == METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_notification_returns_none() -> None:
    handler = JsonRpcHandler()
    called = []

    async def note(params):
        called.append(params)
        return None

    handler.register("note", note)
    resp = await handler.handle({"jsonrpc": "2.0", "method": "note", "params": {"x": 1}})
    assert resp is None
    assert called == [{"x": 1}]


@pytest.mark.asyncio
async def test_rpc_error_propagates() -> None:
    handler = JsonRpcHandler()

    async def boom(params):
        raise RpcError(VALIDATION_ERROR, "bad input", {"field": "x"})

    handler.register("boom", boom)
    resp = await handler.handle({"jsonrpc": "2.0", "id": 2, "method": "boom"})
    assert resp is not None
    assert resp["error"]["code"] == VALIDATION_ERROR
    assert resp["error"]["data"] == {"field": "x"}


@pytest.mark.asyncio
async def test_unhandled_exception_becomes_internal_error() -> None:
    handler = JsonRpcHandler()

    async def explode(params):
        raise RuntimeError("kaboom")

    handler.register("explode", explode)
    resp = await handler.handle({"jsonrpc": "2.0", "id": 3, "method": "explode"})
    assert resp is not None and resp["error"]["code"] == INTERNAL_ERROR


@pytest.mark.asyncio
async def test_duplicate_register_raises() -> None:
    handler = JsonRpcHandler()

    async def foo(params):
        return None

    handler.register("foo", foo)
    with pytest.raises(ValueError):
        handler.register("foo", foo)


@pytest.mark.asyncio
async def test_dispatch_loop_drains_messages(memory_transport_factory) -> None:
    handler = JsonRpcHandler()

    async def add(params):
        return params["a"] + params["b"]

    handler.register("add", add)

    transport = memory_transport_factory(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "add", "params": {"a": 1, "b": 2}},
            {"jsonrpc": "2.0", "id": 2, "method": "add", "params": {"a": 10, "b": 20}},
        ]
    )
    await dispatch_loop(transport, handler)

    assert [m["result"] for m in transport.sent] == [3, 30]
