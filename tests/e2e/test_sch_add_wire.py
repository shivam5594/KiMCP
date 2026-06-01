"""End-to-end: JSON-RPC tools/call → sch_add_wire (M15).

Three-pin structure matching M12/M14 template:

* Happy path — JSON-RPC round-trip; wire lands on disk.
* Dry-run — plan round-trips, bytes untouched.
* ``Backend.SEXPR`` unavailable → ``BACKEND_UNAVAILABLE`` before the tool runs.
"""

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
from kimcp.tools.builtin.sch_add_wire import SchAddWireTool

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


def _load_config(tmp_path: Path, *, snapshot_mode: str = "copy"):
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
async def test_tools_call_sch_add_wire_ok(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)

    server = Server(
        config=_load_config(tmp_path, snapshot_mode="copy"),
        project_root=tmp_path,
    )
    server.register_tool(SchAddWireTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_wire",
                    "arguments": {
                        "sch_path": str(sch),
                        "start_x": 10.0,
                        "start_y": 20.0,
                        "end_x": 50.0,
                        "end_y": 20.0,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    assert len(transport.sent) == 1
    msg = transport.sent[0]
    assert "result" in msg, msg
    assert msg["result"]["isError"] is False
    result = msg["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["sch_path"] == str(sch.resolve())
    wire_uuid = result["wire_uuid"]
    assert isinstance(wire_uuid, str) and len(wire_uuid) > 0
    assert result["meta"]["backend_used"] == Backend.SEXPR.value
    snap_ref = result["meta"]["snapshot_ref"]
    assert snap_ref.startswith("copy:")

    # Round-trip: new wire present on disk.
    doc = SexprDocument.from_path(sch)
    assert _find_wire_by_uuid(doc.root, wire_uuid) is not None


@pytest.mark.asyncio
async def test_tools_call_sch_add_wire_dry_run(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()

    server = Server(
        config=_load_config(tmp_path, snapshot_mode="copy"),
        project_root=tmp_path,
    )
    server.register_tool(SchAddWireTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_wire",
                    "arguments": {
                        "sch_path": str(sch),
                        "start_x": 0.0,
                        "start_y": 0.0,
                        "end_x": 10.0,
                        "end_y": 0.0,
                        "dry_run": True,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    msg = transport.sent[0]
    assert "result" in msg
    result = msg["result"]["structuredContent"]
    assert result["status"] == "dry_run"
    assert result["wire_uuid"] is None
    assert result["meta"]["snapshot_ref"] is None
    assert sch.read_bytes() == before


@pytest.mark.asyncio
async def test_tools_call_sch_add_wire_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)

    server = Server(config=_load_config(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddWireTool())
    # Do NOT mark SEXPR available.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_wire",
                    "arguments": {
                        "sch_path": str(sch),
                        "start_x": 0.0,
                        "start_y": 0.0,
                        "end_x": 10.0,
                        "end_y": 0.0,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    msg = transport.sent[0]
    assert "error" in msg
    err = msg["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
    assert err["data"]["preferred"] == [Backend.SEXPR.value]
    assert sch.read_text(encoding="utf-8") == _SCH


def _find_wire_by_uuid(root: SList, wire_uuid: str) -> SList | None:
    for child in root.items:
        if not isinstance(child, SList) or child.head != "wire":
            continue
        uuid_node = child.find("uuid")
        if uuid_node is None or len(uuid_node.items) < 2:
            continue
        payload = uuid_node.items[1]
        if isinstance(payload, SAtom) and payload.text == wire_uuid:
            return child
    return None
