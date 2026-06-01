"""E2E JSON-RPC round-trip for M16 sch_add_junction."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_junction import SchAddJunctionTool

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


def _find_junction_by_uuid(root: SList, junction_uuid: str) -> SList | None:
    for child in root.items:
        if not isinstance(child, SList) or child.head != "junction":
            continue
        uuid_node = child.find("uuid")
        if uuid_node is None or len(uuid_node.items) < 2:
            continue
        payload = uuid_node.items[1]
        if isinstance(payload, SAtom) and payload.text == junction_uuid:
            return child
    return None


@pytest.mark.asyncio
async def test_tools_call_sch_add_junction_ok(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)

    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddJunctionTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_junction",
                    "arguments": {"sch_path": str(sch), "at_x": 100.0, "at_y": 50.0},
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    msg = transport.sent[0]
    assert "result" in msg, msg
    result = msg["result"]["structuredContent"]
    assert result["status"] == "ok"
    junction_uuid = result["junction_uuid"]
    assert isinstance(junction_uuid, str) and len(junction_uuid) > 0
    assert result["meta"]["backend_used"] == Backend.SEXPR.value
    assert result["meta"]["snapshot_ref"].startswith("copy:")

    doc = SexprDocument.from_path(sch)
    assert _find_junction_by_uuid(doc.root, junction_uuid) is not None


@pytest.mark.asyncio
async def test_tools_call_sch_add_junction_dry_run(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()

    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddJunctionTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_junction",
                    "arguments": {
                        "sch_path": str(sch),
                        "at_x": 0.0,
                        "at_y": 0.0,
                        "dry_run": True,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    msg = transport.sent[0]
    result = msg["result"]["structuredContent"]
    assert result["status"] == "dry_run"
    assert result["junction_uuid"] is None
    assert result["meta"]["snapshot_ref"] is None
    assert sch.read_bytes() == before


@pytest.mark.asyncio
async def test_tools_call_sch_add_junction_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddJunctionTool())
    # Do NOT mark SEXPR available.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_junction",
                    "arguments": {"sch_path": str(sch), "at_x": 0.0, "at_y": 0.0},
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    msg = transport.sent[0]
    assert "error" in msg
    assert msg["error"]["code"] == BACKEND_UNAVAILABLE
    assert sch.read_text(encoding="utf-8") == _SCH
