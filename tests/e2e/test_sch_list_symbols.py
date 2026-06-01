"""E2E for sch_list_symbols."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.tools.builtin.sch_list_symbols import SchListSymbolsTool

pytestmark = [pytest.mark.e2e]


_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 50 50 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "11111111-0000-0000-0000-000000000001")
\t\t(property "Reference" "R1" (at 52 48 0))
\t\t(property "Value" "1k" (at 52 52 0))
\t)
)
"""


def _write_sch(tmp_path: Path) -> Path:
    sch = tmp_path / "design.kicad_sch"
    sch.write_text(_SCH, encoding="utf-8")
    return sch


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
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchListSymbolsTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_list_symbols",
                    "arguments": {"sch_path": str(sch)},
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["total"] == 1
    assert result["symbols"][0]["reference"] == "R1"
    assert result["meta"]["backend_used"] == Backend.SEXPR.value


@pytest.mark.asyncio
async def test_e2e_filter(tmp_path: Path, memory_transport_factory) -> None:
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchListSymbolsTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_list_symbols",
                    "arguments": {
                        "sch_path": str(sch),
                        "reference_prefix": "X",
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
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchListSymbolsTool())
    # No SEXPR availability marked.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_list_symbols",
                    "arguments": {"sch_path": str(sch)},
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
