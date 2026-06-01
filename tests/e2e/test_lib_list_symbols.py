"""E2E for lib_list_symbols."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.tools.builtin.lib_list_symbols import LibListSymbolsTool

pytestmark = [pytest.mark.e2e]


_LIB = """\
(kicad_symbol_lib
\t(version 20240108)
\t(generator "kicad_symbol_editor")
\t(symbol "R"
\t\t(property "Reference" "R" (at 0 0 0))
\t\t(property "Value" "R" (at 0 0 0))
\t\t(symbol "R_1_1"
\t\t\t(pin passive line (at 0 2.54 270) (length 1.27) (name "~") (number "1"))
\t\t\t(pin passive line (at 0 -2.54 90) (length 1.27) (name "~") (number "2"))
\t\t)
\t)
\t(symbol "C"
\t\t(property "Reference" "C" (at 0 0 0))
\t\t(property "Value" "C" (at 0 0 0))
\t\t(symbol "C_1_1"
\t\t\t(pin passive line (at 0 2.54 270) (length 1.27) (name "~") (number "1"))
\t\t\t(pin passive line (at 0 -2.54 90) (length 1.27) (name "~") (number "2"))
\t\t)
\t)
)
"""


def _write_lib(tmp_path: Path) -> Path:
    lib = tmp_path / "Device.kicad_sym"
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
    server.register_tool(LibListSymbolsTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "lib_list_symbols",
                    "arguments": {"lib_path": str(lib)},
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["total"] == 2
    names = {s["name"] for s in result["symbols"]}
    assert names == {"R", "C"}
    assert result["meta"]["backend_used"] == Backend.SEXPR.value


@pytest.mark.asyncio
async def test_e2e_filter(tmp_path: Path, memory_transport_factory) -> None:
    lib = _write_lib(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(LibListSymbolsTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "lib_list_symbols",
                    "arguments": {
                        "lib_path": str(lib),
                        "name_contains": "r",
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    # Case-insensitive → "R" matches.
    assert result["status"] == "ok"
    assert result["total"] == 1
    assert result["symbols"][0]["name"] == "R"


@pytest.mark.asyncio
async def test_e2e_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    lib = _write_lib(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(LibListSymbolsTool())
    # No SEXPR availability marked.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "lib_list_symbols",
                    "arguments": {"lib_path": str(lib)},
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
