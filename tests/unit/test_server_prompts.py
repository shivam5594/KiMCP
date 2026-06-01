"""Unit tests for ``Server`` + prompts wiring.

The registry-level contract is in ``test_prompts_registry.py``. Here
we pin the *wire* shape: what lands in the JSON-RPC response when a
client calls ``prompts/list`` or ``prompts/get``, plus the capability
advertisement inside ``initialize``. Breaking these would silently
drop ``kimcp`` off every MCP client's prompt picker.
"""

from __future__ import annotations

from typing import Any

import pytest

from kimcp.errors import INVALID_PARAMS, METHOD_NOT_FOUND
from kimcp.server import Server


@pytest.fixture
def server() -> Server:
    return Server()


# -- initialize advertises prompts capability -----------------------------


@pytest.mark.asyncio
async def test_initialize_advertises_prompts_capability(server: Server) -> None:
    """``capabilities.prompts`` must be present so clients enable their
    prompt picker against this server. ``listChanged=false`` is the
    explicit contract; clients that subscribe to list-changed
    notifications read it.
    """
    result = await server.handler.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert result is not None
    caps = result["result"]["capabilities"]
    assert "prompts" in caps
    assert caps["prompts"] == {"listChanged": False}


# -- prompts/list ---------------------------------------------------------


@pytest.mark.asyncio
async def test_prompts_list_returns_both_builtins(server: Server) -> None:
    """Both canned workflows ship out of the box. If either goes missing,
    the ``Server.__init__`` wiring silently dropped a registration —
    the kind of regression that's invisible until a user opens the
    prompt picker and sees fewer entries than expected.
    """
    result = await server.handler.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}}
    )
    assert result is not None
    listing = result["result"]["prompts"]
    names = {entry["name"] for entry in listing}
    assert names == {"design-review", "manufacturing-handoff"}


@pytest.mark.asyncio
async def test_prompts_list_each_entry_has_required_fields(server: Server) -> None:
    """Every listed prompt must carry ``name`` + ``description`` +
    ``arguments``. The spec only mandates ``name``, but clients
    render the other two in the picker — missing them shows a blank
    menu entry.
    """
    result = await server.handler.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {}}
    )
    assert result is not None
    for entry in result["result"]["prompts"]:
        assert "name" in entry
        assert "description" in entry
        assert "arguments" in entry
        assert isinstance(entry["arguments"], list)


# -- prompts/get ----------------------------------------------------------


@pytest.mark.asyncio
async def test_prompts_get_design_review_returns_messages(server: Server) -> None:
    """End-to-end: name + args in → messages array out. The body shape
    (``{messages, description?}``) is fixed by the MCP spec; clients
    branch on it."""
    result = await server.handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "prompts/get",
            "params": {
                "name": "design-review",
                "arguments": {
                    "sch_path": "board.kicad_sch",
                    "pcb_path": "board.kicad_pcb",
                },
            },
        }
    )
    assert result is not None
    assert "error" not in result
    body = result["result"]
    assert "messages" in body
    assert len(body["messages"]) == 1
    msg = body["messages"][0]
    assert msg["role"] == "user"
    assert msg["content"]["type"] == "text"
    assert "board.kicad_sch" in msg["content"]["text"]


@pytest.mark.asyncio
async def test_prompts_get_missing_name_returns_invalid_params(server: Server) -> None:
    result = await server.handler.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "prompts/get", "params": {}}
    )
    assert result is not None
    assert result["error"]["code"] == INVALID_PARAMS


@pytest.mark.asyncio
async def test_prompts_get_non_string_name_returns_invalid_params(server: Server) -> None:
    result = await server.handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "prompts/get",
            "params": {"name": 42},
        }
    )
    assert result is not None
    assert result["error"]["code"] == INVALID_PARAMS


@pytest.mark.asyncio
async def test_prompts_get_unknown_name_returns_method_not_found(server: Server) -> None:
    """Parity with ``tools/call`` — unknown name uses METHOD_NOT_FOUND,
    not INVALID_PARAMS. Clients use the error code to decide whether
    to retry, show "prompt doesn't exist", or re-list prompts."""
    result = await server.handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "prompts/get",
            "params": {"name": "no-such-prompt"},
        }
    )
    assert result is not None
    assert result["error"]["code"] == METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_prompts_get_non_dict_arguments_returns_invalid_params(
    server: Server,
) -> None:
    result = await server.handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "prompts/get",
            "params": {"name": "design-review", "arguments": ["not", "a", "dict"]},
        }
    )
    assert result is not None
    assert result["error"]["code"] == INVALID_PARAMS


@pytest.mark.asyncio
async def test_prompts_get_missing_required_arg_returns_invalid_params(
    server: Server,
) -> None:
    """Required-arg validation surfaces as a clean JSON-RPC error with
    ``data.missing`` naming the absent field."""
    result = await server.handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "prompts/get",
            "params": {
                "name": "design-review",
                "arguments": {"sch_path": "a.kicad_sch"},  # missing pcb_path
            },
        }
    )
    assert result is not None
    err = result["error"]
    assert err["code"] == INVALID_PARAMS
    assert err["data"]["missing"] == ["pcb_path"]


@pytest.mark.asyncio
async def test_prompts_get_manufacturing_handoff_optional_arg_absent(
    server: Server,
) -> None:
    """Optional ``fab_profile`` can be omitted — end-to-end path."""
    result = await server.handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "prompts/get",
            "params": {
                "name": "manufacturing-handoff",
                "arguments": {
                    "pcb_path": "b.kicad_pcb",
                    "sch_path": "b.kicad_sch",
                },
            },
        }
    )
    assert result is not None
    assert "error" not in result
    text = result["result"]["messages"][0]["content"]["text"]
    # Verify the "no profile supplied" branch ran.
    assert "No fab profile" in text or "generic" in text.lower()


@pytest.mark.asyncio
async def test_prompts_get_no_arguments_key_treated_as_empty(server: Server) -> None:
    """If the client omits ``arguments`` entirely, handler treats it as
    ``{}`` — which still fails required-arg validation but not the
    earlier "arguments must be an object" guard. Pin so the ordering
    of guards stays predictable."""
    result = await server.handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "prompts/get",
            "params": {"name": "design-review"},
        }
    )
    assert result is not None
    assert result["error"]["code"] == INVALID_PARAMS
    assert "missing" in (result["error"].get("data") or {})


# -- ordering / determinism -----------------------------------------------


@pytest.mark.asyncio
async def test_prompts_list_is_stable_across_calls(server: Server) -> None:
    """Two back-to-back ``prompts/list`` calls must return identical
    bodies. The registry is populated at construction and never
    mutated; a regression that introduced nondeterminism (set-based
    ordering, say) would break client-side caching."""
    first = await server.handler.handle(
        {"jsonrpc": "2.0", "id": 12, "method": "prompts/list", "params": {}}
    )
    second = await server.handler.handle(
        {"jsonrpc": "2.0", "id": 13, "method": "prompts/list", "params": {}}
    )
    assert first is not None
    assert second is not None
    # Drop the echoed id — everything else must match byte-for-byte.
    first_body: dict[str, Any] = first["result"]
    second_body: dict[str, Any] = second["result"]
    assert first_body == second_body
