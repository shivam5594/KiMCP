"""E2E for sch_add_sheet."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.tools.builtin.sch_add_sheet import SchAddSheetTool

pytestmark = [pytest.mark.e2e]


_PARENT = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols)
)
"""


def _write_parent(tmp_path: Path) -> Path:
    sch = tmp_path / "parent.kicad_sch"
    sch.write_text(_PARENT, encoding="utf-8")
    return sch


def _cfg(tmp_path: Path):
    return load_config(
        user_global=tmp_path / "__n_u.toml",
        project_local=tmp_path / "__n_p.toml",
        session_overrides={
            "safety": {"snapshot_mode": "off", "grid_snap_mm": None},
            "kicad": {
                "cli_exe": str(tmp_path / "nope-cli"),
                "ipc_socket": str(tmp_path / "no.sock"),
            },
        },
    )


@pytest.mark.asyncio
async def test_e2e_ok(tmp_path: Path, memory_transport_factory) -> None:
    parent = _write_parent(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddSheetTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_sheet",
                    "arguments": {
                        "sch_path": str(parent),
                        "sheet_name": "Power",
                        "sheet_file": "power.kicad_sch",
                        "at_x": 100.0,
                        "at_y": 100.0,
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["child_created"] is True
    assert result["meta"]["backend_used"] == Backend.SEXPR.value
    assert (tmp_path / "power.kicad_sch").is_file()


@pytest.mark.asyncio
async def test_e2e_dry_run(tmp_path: Path, memory_transport_factory) -> None:
    parent = _write_parent(tmp_path)
    before = parent.read_text(encoding="utf-8")
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddSheetTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_sheet",
                    "arguments": {
                        "sch_path": str(parent),
                        "sheet_name": "Sub",
                        "sheet_file": "sub.kicad_sch",
                        "at_x": 10.0,
                        "at_y": 10.0,
                        "dry_run": True,
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "dry_run"
    assert parent.read_text(encoding="utf-8") == before
    assert not (tmp_path / "sub.kicad_sch").exists()


@pytest.mark.asyncio
async def test_e2e_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    parent = _write_parent(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddSheetTool())
    # No SEXPR availability marked.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_sheet",
                    "arguments": {
                        "sch_path": str(parent),
                        "sheet_name": "Sub",
                        "sheet_file": "sub.kicad_sch",
                        "at_x": 10.0,
                        "at_y": 10.0,
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
