"""E2E for lib_search_symbol."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.tools.builtin.lib_search_symbol import LibSearchSymbolTool

pytestmark = [pytest.mark.e2e]


_LIB = """\
(kicad_symbol_lib
\t(version 20240108)
\t(generator "kicad_symbol_editor")
\t(symbol "LM358"
\t\t(property "Reference" "U" (at 0 0 0))
\t\t(property "Value" "LM358" (at 0 0 0))
\t\t(property "Description" "Dual operational amplifier" (at 0 0 0))
\t\t(property "ki_keywords" "dual opamp" (at 0 0 0))
\t)
\t(symbol "R_Small"
\t\t(property "Reference" "R" (at 0 0 0))
\t\t(property "Value" "R_Small" (at 0 0 0))
\t\t(property "Description" "Resistor" (at 0 0 0))
\t\t(property "ki_keywords" "R resistor" (at 0 0 0))
\t)
)
"""


def _write_lib(tmp_path: Path) -> Path:
    lib = tmp_path / "Demo.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    return lib


def _cfg(tmp_path: Path):
    return load_config(
        user_global=tmp_path / "__n_u.toml",
        project_local=tmp_path / "__n_p.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(tmp_path / "nope-cli"),
                "ipc_socket": str(tmp_path / "no.sock"),
            }
        },
    )


@pytest.mark.asyncio
async def test_e2e_ok(tmp_path: Path, memory_transport_factory) -> None:
    lib = _write_lib(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(LibSearchSymbolTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "lib_search_symbol",
                    "arguments": {
                        "lib_paths": [str(lib)],
                        "query": "opamp",
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["total"] == 1
    assert result["results"][0]["entry"]["name"] == "LM358"
    assert result["meta"]["backend_used"] == Backend.SEXPR.value


@pytest.mark.asyncio
async def test_e2e_no_match(tmp_path: Path, memory_transport_factory) -> None:
    lib = _write_lib(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(LibSearchSymbolTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "lib_search_symbol",
                    "arguments": {
                        "lib_paths": [str(lib)],
                        "query": "microcontroller",
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_e2e_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    lib = _write_lib(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(LibSearchSymbolTool())
    # No SEXPR availability marked.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "lib_search_symbol",
                    "arguments": {
                        "lib_paths": [str(lib)],
                        "query": "anything",
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
