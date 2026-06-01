"""E2E for sch_list_wires."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.tools.builtin.sch_list_wires import SchListWiresTool

pytestmark = [pytest.mark.e2e]


_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols)
\t(wire
\t\t(pts (xy 50 50) (xy 70 50))
\t\t(stroke (width 0) (type default))
\t\t(uuid "11111111-0000-0000-0000-000000000001")
\t)
\t(junction
\t\t(at 70 50)
\t\t(diameter 0)
\t\t(color 0 0 0 0)
\t\t(uuid "22222222-0000-0000-0000-000000000002")
\t)
\t(no_connect
\t\t(at 80 50)
\t\t(uuid "33333333-0000-0000-0000-000000000003")
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
    server.register_tool(SchListWiresTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_list_wires",
                    "arguments": {"sch_path": str(sch)},
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["total"] == 3
    assert len(result["wires"]) == 1
    assert len(result["junctions"]) == 1
    assert len(result["no_connects"]) == 1
    assert result["meta"]["backend_used"] == Backend.SEXPR.value


@pytest.mark.asyncio
async def test_e2e_include_filter(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchListWiresTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_list_wires",
                    "arguments": {
                        "sch_path": str(sch),
                        "include": ["wire"],
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert len(result["wires"]) == 1
    assert result["junctions"] == []
    assert result["no_connects"] == []


@pytest.mark.asyncio
async def test_e2e_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchListWiresTool())
    # No SEXPR availability marked.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_list_wires",
                    "arguments": {"sch_path": str(sch)},
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
