"""E2E JSON-RPC round-trip for M18 sch_add_power."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.sexpr.document import SexprDocument
from kimcp.tools.builtin.sch_add_power import (
    SchAddPowerTool,
    _find_power_instance_by_uuid,
)
from kimcp.tools.builtin.sch_add_symbol import _find_lib_symbol

pytestmark = [pytest.mark.e2e]


_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols))
"""


def _write_sch(tmp_path: Path) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_SCH, encoding="utf-8")
    return sch


def _cfg(tmp_path: Path, *, snapshot_mode: str = "copy"):
    return load_config(
        user_global=tmp_path / "__nope_user.toml",
        project_local=tmp_path / "__nope_project.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(tmp_path / "nonexistent-cli"),
                "ipc_socket": str(tmp_path / "not-a-socket.sock"),
            },
            "safety": {"snapshot_mode": snapshot_mode, "grid_snap_mm": None},
        },
    )


@pytest.mark.asyncio
async def test_tools_call_sch_add_power_ok(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)

    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddPowerTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_power",
                    "arguments": {
                        "sch_path": str(sch),
                        "net_name": "GND",
                        "at_x": 100.0,
                        "at_y": 50.0,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["net_name"] == "GND"
    assert result["lib_id"] == "power:GND"
    assert result["lib_symbol_embedded"] is True
    assert result["meta"]["backend_used"] == Backend.SEXPR.value
    assert result["meta"]["snapshot_ref"].startswith("copy:")

    doc = SexprDocument.from_path(sch)
    # Instance is present.
    assert _find_power_instance_by_uuid(doc.root, result["instance_uuid"]) is not None
    # Lib symbol got embedded.
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    assert _find_lib_symbol(lib_symbols, "power:GND") is not None


@pytest.mark.asyncio
async def test_tools_call_sch_add_power_dry_run(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()

    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddPowerTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_power",
                    "arguments": {
                        "sch_path": str(sch),
                        "net_name": "+3V3",
                        "at_x": 0.0,
                        "at_y": 0.0,
                        "dry_run": True,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "dry_run"
    assert result["instance_uuid"] is None
    assert result["lib_symbol_embedded"] is None
    assert sch.read_bytes() == before


@pytest.mark.asyncio
async def test_tools_call_sch_add_power_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddPowerTool())
    # No SEXPR availability.
    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_power",
                    "arguments": {
                        "sch_path": str(sch),
                        "net_name": "GND",
                        "at_x": 0.0,
                        "at_y": 0.0,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
    assert sch.read_text(encoding="utf-8") == _SCH
