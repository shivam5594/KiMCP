"""E2E JSON-RPC round-trip for M20 sch_delete."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.sexpr.document import SexprDocument
from kimcp.tools.builtin.sch_add_wire import SchAddWireInput, SchAddWireTool
from kimcp.tools.builtin.sch_delete import SchDeleteTool, _find_by_uuid

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
            "safety": {"snapshot_mode": snapshot_mode},
        },
    )


async def _seed_wire(tmp_path: Path, sch: Path) -> str:
    """Place a wire and return its UUID."""
    tool = SchAddWireTool()
    tool.set_config(load_config(session_overrides={"safety": {"snapshot_mode": "off"}}))
    out = await tool.run(
        SchAddWireInput(
            sch_path=sch,
            start_x=0.0,
            start_y=0.0,
            end_x=10.0,
            end_y=0.0,
        )
    )
    assert out.wire_uuid is not None
    return out.wire_uuid


@pytest.mark.asyncio
async def test_tools_call_sch_delete_ok(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    wire_uuid = await _seed_wire(tmp_path, sch)

    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchDeleteTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_delete",
                    "arguments": {
                        "sch_path": str(sch),
                        "uuid": wire_uuid,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["deleted_head"] == "wire"
    assert result["deleted_uuid"] == wire_uuid
    assert result["meta"]["backend_used"] == Backend.SEXPR.value
    assert result["meta"]["snapshot_ref"].startswith("copy:")

    doc = SexprDocument.from_path(sch)
    assert _find_by_uuid(doc.root, wire_uuid) is None


@pytest.mark.asyncio
async def test_tools_call_sch_delete_dry_run(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    wire_uuid = await _seed_wire(tmp_path, sch)
    before = sch.read_bytes()

    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchDeleteTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_delete",
                    "arguments": {
                        "sch_path": str(sch),
                        "uuid": wire_uuid,
                        "dry_run": True,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "dry_run"
    assert result["deleted_head"] == "wire"
    assert sch.read_bytes() == before


@pytest.mark.asyncio
async def test_tools_call_sch_delete_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchDeleteTool())
    # No SEXPR availability.
    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_delete",
                    "arguments": {
                        "sch_path": str(sch),
                        "uuid": "some-uuid",
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
    assert sch.read_text(encoding="utf-8") == _SCH
