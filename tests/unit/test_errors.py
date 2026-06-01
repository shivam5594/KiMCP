"""Unit tests for ``kimcp.errors``.

Pin the JSON-RPC + KiMCP-specific error code values against
``schemas.md`` — any numeric drift here silently breaks clients that
branch on error codes (e.g. retry on ``BACKEND_UNAVAILABLE`` but fail
on ``VALIDATION_ERROR``). Also cover ``RpcError`` round-tripping so the
wire format stays stable.
"""

from __future__ import annotations

import pytest

from kimcp.errors import (
    BACKEND_UNAVAILABLE,
    DESTRUCTIVE_REFUSED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    KICAD_VERSION_INCOMPAT,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RULE_VIOLATION,
    VALIDATION_ERROR,
    RpcError,
)

# -- Error code values (pin against schemas.md) ---------------------------


def test_jsonrpc_standard_codes_match_spec() -> None:
    """JSON-RPC 2.0 standard codes must match the spec exactly.

    Any drift here would make kimcp reject spec-compliant clients or
    vice versa. The values are fixed by the spec — if a test fails
    because one of these changed, reverse the change.
    """
    assert PARSE_ERROR == -32700
    assert INVALID_REQUEST == -32600
    assert METHOD_NOT_FOUND == -32601
    assert INVALID_PARAMS == -32602
    assert INTERNAL_ERROR == -32603


def test_kimcp_codes_match_schemas_md() -> None:
    """KiMCP-specific codes are load-bearing for client retry logic.

    The code → semantics mapping is owned by ``schemas.md``; changing a
    value here means an ADR bump per errors.py's own top comment.
    """
    assert VALIDATION_ERROR == -32000
    assert BACKEND_UNAVAILABLE == -32001
    assert DESTRUCTIVE_REFUSED == -32002
    assert KICAD_VERSION_INCOMPAT == -32003
    assert RULE_VIOLATION == -32004


def test_kimcp_codes_occupy_server_error_range() -> None:
    """JSON-RPC reserves -32000..-32099 for server errors.

    Pinning the range stops a future code from accidentally colliding
    with the reserved ``-32700 PARSE_ERROR`` or standard-transport
    codes.
    """
    for code in (
        VALIDATION_ERROR,
        BACKEND_UNAVAILABLE,
        DESTRUCTIVE_REFUSED,
        KICAD_VERSION_INCOMPAT,
        RULE_VIOLATION,
    ):
        assert -32099 <= code <= -32000, f"code {code} outside server-error range"


def test_all_codes_are_unique() -> None:
    """A collision would route error semantics to the wrong branch on
    the client side. Cheap to guard."""
    codes = {
        PARSE_ERROR,
        INVALID_REQUEST,
        METHOD_NOT_FOUND,
        INVALID_PARAMS,
        INTERNAL_ERROR,
        VALIDATION_ERROR,
        BACKEND_UNAVAILABLE,
        DESTRUCTIVE_REFUSED,
        KICAD_VERSION_INCOMPAT,
        RULE_VIOLATION,
    }
    assert len(codes) == 10


# -- RpcError dataclass semantics -----------------------------------------


def test_rpc_error_is_exception() -> None:
    """Handlers raise; the RPC layer catches — inheriting Exception is load-bearing."""
    err = RpcError(code=INVALID_PARAMS, message="bad input")
    assert isinstance(err, Exception)
    with pytest.raises(RpcError):
        raise err


def test_rpc_error_str_includes_code_and_message() -> None:
    """The default ``str(exc)`` is what lands in unhandled-exception traces.

    Including the code makes "which error is this?" answerable from
    stderr alone — no need to re-inspect via debugger.
    """
    err = RpcError(code=VALIDATION_ERROR, message="schema mismatch")
    assert "[-32000]" in str(err)
    assert "schema mismatch" in str(err)


def test_rpc_error_to_rpc_omits_data_when_none() -> None:
    """Wire format: ``data`` key must be absent (not null) when empty.

    JSON-RPC libraries differ on how they treat ``data: null`` vs. a
    missing key; keeping the key absent is the more conservative wire
    shape.
    """
    err = RpcError(code=METHOD_NOT_FOUND, message="unknown method")
    payload = err.to_rpc()
    assert payload == {"code": METHOD_NOT_FOUND, "message": "unknown method"}
    assert "data" not in payload


def test_rpc_error_to_rpc_carries_structured_data() -> None:
    """Validation / rule-violation errors ride ``data`` for rich client UX.

    Pins the shape the client can rely on: ``{code, message, data}``
    with ``data`` being a free-form dict (rule_id, field name, etc.).
    """
    err = RpcError(
        code=RULE_VIOLATION,
        message="clearance too tight",
        data={"rule_id": "DFM-TRC-02", "field": "track_width"},
    )
    payload = err.to_rpc()
    assert payload["code"] == RULE_VIOLATION
    assert payload["data"] == {"rule_id": "DFM-TRC-02", "field": "track_width"}


def test_rpc_error_data_default_is_none_not_empty_dict() -> None:
    """Default-factory gotcha: ``data={}`` would serialize ``data`` into the
    wire payload. Default must stay ``None`` so it's suppressed unless
    the caller opts in.
    """
    err = RpcError(code=INTERNAL_ERROR, message="oops")
    assert err.data is None


def test_rpc_error_supports_equality_via_dataclass() -> None:
    """Dataclass ``eq=True`` default gives us value-equality on ``(code, message, data)``.

    Handy for test assertions that want to compare one RpcError against
    another without picking fields apart.
    """
    a = RpcError(code=VALIDATION_ERROR, message="x", data={"k": "v"})
    b = RpcError(code=VALIDATION_ERROR, message="x", data={"k": "v"})
    c = RpcError(code=VALIDATION_ERROR, message="x", data={"k": "w"})
    assert a == b
    assert a != c
