"""Hand-rolled JSON-RPC 2.0 handler (ADR-0012).

Sits above a tiny transport abstraction (`kimcp.transport.Transport`) that
only knows `read_message` / `write_message`. Framework-free by design —
MCP-specific semantics are added in `kimcp.server`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from kimcp.errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    RpcError,
)

if TYPE_CHECKING:
    from kimcp.transport.base import Transport

log = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"

MethodHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class JsonRpcHandler:
    """Routes JSON-RPC methods to registered handlers.

    Notifications (no `id` key) get dispatched but never produce a response.
    """

    def __init__(self) -> None:
        self._methods: dict[str, MethodHandler] = {}

    def register(self, name: str, handler: MethodHandler) -> None:
        if name in self._methods:
            raise ValueError(f"duplicate JSON-RPC method: {name}")
        self._methods[name] = handler

    def methods(self) -> list[str]:
        return sorted(self._methods.keys())

    async def handle(self, message: Any) -> dict[str, Any] | None:
        """Handle one message. Returns the response dict, or None for notifications.

        Accepts `Any` so that a non-dict slipping through a loose transport
        gets a clean JSON-RPC error response rather than a TypeError.
        """
        if not isinstance(message, dict):
            return _error_response(None, INVALID_REQUEST, "message is not a JSON object")

        if message.get("jsonrpc") != JSONRPC_VERSION:
            return _error_response(
                message.get("id"), INVALID_REQUEST, "jsonrpc field must be '2.0'"
            )

        method = message.get("method")
        if not isinstance(method, str):
            return _error_response(message.get("id"), INVALID_REQUEST, "method must be a string")

        is_notification = "id" not in message
        request_id = message.get("id")

        handler = self._methods.get(method)
        if handler is None:
            if is_notification:
                log.debug("ignoring unknown notification: %s", method)
                return None
            return _error_response(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")

        raw_params = message.get("params", {})
        if isinstance(raw_params, list):
            params: dict[str, Any] = {"_positional": raw_params}
        elif isinstance(raw_params, dict):
            params = raw_params
        else:
            return _error_response(
                request_id, INVALID_REQUEST, "params must be a JSON object or array"
            )

        try:
            result = await handler(params)
        except RpcError as exc:
            if is_notification:
                log.warning("notification %s raised RpcError: %s", method, exc)
                return None
            return {
                "jsonrpc": JSONRPC_VERSION,
                "id": request_id,
                "error": exc.to_rpc(),
            }
        except Exception:
            log.exception("unhandled exception in method %s", method)
            if is_notification:
                return None
            return _error_response(request_id, INTERNAL_ERROR, "internal error")

        if is_notification:
            return None
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error_response(
    request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": err}


async def dispatch_loop(transport: Transport, handler: JsonRpcHandler) -> None:
    """Read → handle → write until the transport reports EOF."""
    while True:
        msg = await transport.read_message()
        if msg is None:
            log.info("transport EOF; exiting dispatch loop")
            return
        response = await handler.handle(msg)
        if response is not None:
            await transport.write_message(response)


__all__ = ["JSONRPC_VERSION", "JsonRpcHandler", "MethodHandler", "dispatch_loop"]
