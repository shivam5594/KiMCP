"""End-to-end: JSON-RPC tools/call → sch_add_symbol (M14).

Same three-pin structure as the M12 e2e (the template every
schematic-mutation tool copies):

* Happy path — JSON-RPC round-trip; instance lands on disk.
* Dry-run — plan round-trips through the transport, bytes untouched.
* ``Backend.SEXPR`` unavailable → ``BACKEND_UNAVAILABLE`` before the
  tool runs.

No kicad-cli stub involved — M14 is SEXPR-backed like M12.
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
from kimcp.tools.builtin.sch_add_symbol import SchAddSymbolTool

pytestmark = [pytest.mark.e2e]


_SCH_WITH_R_SMALL = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "Device:R_Small"
\t\t\t(exclude_from_sim no)
\t\t\t(in_bom yes)
\t\t\t(on_board yes)
\t\t\t(property "Reference" "R"
\t\t\t\t(at 2.032 0 90)
\t\t\t\t(effects (font (size 1.27 1.27))))
\t\t\t(property "Value" "R_Small"
\t\t\t\t(at 0 0 90)
\t\t\t\t(effects (font (size 1.27 1.27))))
\t\t\t(symbol "R_Small_0_1"
\t\t\t\t(rectangle
\t\t\t\t\t(start -0.762 2.032)
\t\t\t\t\t(end 0.762 -2.032)))
\t\t\t(symbol "R_Small_1_1"
\t\t\t\t(pin passive line
\t\t\t\t\t(at 0 2.54 270)
\t\t\t\t\t(length 0.508)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))
\t\t\t\t(pin passive line
\t\t\t\t\t(at 0 -2.54 90)
\t\t\t\t\t(length 0.508)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "2" (effects (font (size 1.27 1.27)))))))))
"""


def _write_sch(tmp_path: Path) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_SCH_WITH_R_SMALL, encoding="utf-8")
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


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_sch_add_symbol_ok(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Full JSON-RPC → mutation lands; re-parsing confirms the new
    instance UUID + reference wired through."""
    sch = _write_sch(tmp_path)

    server = Server(
        config=_load_config(tmp_path, snapshot_mode="copy"),
        project_root=tmp_path,
    )
    server.register_tool(SchAddSymbolTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_symbol",
                    "arguments": {
                        "sch_path": str(sch),
                        "lib_id": "Device:R_Small",
                        "reference": "R1",
                        "value": "4.7k",
                        "at_x": 100.0,
                        "at_y": 50.0,
                        "footprint": "Resistor_SMD:R_0603_1608Metric",
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
    assert result["reference"] == "R1"
    instance_uuid = result["instance_uuid"]
    assert isinstance(instance_uuid, str) and len(instance_uuid) > 0
    # Dispatcher ran → backend_used stamped.
    assert result["meta"]["backend_used"] == Backend.SEXPR.value
    snap_ref = result["meta"]["snapshot_ref"]
    assert snap_ref.startswith("copy:")

    # Round-trip: the new instance is present in the written file.
    doc = SexprDocument.from_path(sch)
    found = _find_instance_by_uuid(doc.root, instance_uuid)
    assert found is not None
    lib_id = found.find("lib_id")
    assert lib_id is not None and isinstance(lib_id.items[1], SAtom)
    assert lib_id.items[1].text == "Device:R_Small"


# -- dry-run round-trips through JSON-RPC ---------------------------------


@pytest.mark.asyncio
async def test_tools_call_sch_add_symbol_dry_run(
    tmp_path: Path, memory_transport_factory
) -> None:
    """``dry_run=True`` returns a plan + leaves the file byte-identical."""
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()

    server = Server(
        config=_load_config(tmp_path, snapshot_mode="copy"),
        project_root=tmp_path,
    )
    server.register_tool(SchAddSymbolTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_symbol",
                    "arguments": {
                        "sch_path": str(sch),
                        "lib_id": "Device:R_Small",
                        "reference": "R9",
                        "value": "dry-run-value",
                        "at_x": 10.0,
                        "at_y": 20.0,
                        "dry_run": True,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    msg = transport.sent[0]
    assert "result" in msg, msg
    assert msg["result"]["isError"] is False
    result = msg["result"]["structuredContent"]
    assert result["status"] == "dry_run"
    assert result["reference"] == "R9"
    # No UUID allocated on dry_run.
    assert result["instance_uuid"] is None
    # No snapshot taken on dry_run — preserves the contract.
    assert result["meta"]["snapshot_ref"] is None
    # Bytes untouched.
    assert sch.read_bytes() == before


# -- dispatcher gate fires when SEXPR is unavailable ----------------------


@pytest.mark.asyncio
async def test_tools_call_sch_add_symbol_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Server that never probed rejects the call before the tool runs."""
    sch = _write_sch(tmp_path)

    server = Server(config=_load_config(tmp_path), project_root=tmp_path)
    server.register_tool(SchAddSymbolTool())
    # Do NOT mark SEXPR available.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_add_symbol",
                    "arguments": {
                        "sch_path": str(sch),
                        "lib_id": "Device:R_Small",
                        "reference": "R1",
                        "value": "10k",
                        "at_x": 0.0,
                        "at_y": 0.0,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    msg = transport.sent[0]
    assert "error" in msg, msg
    err = msg["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
    assert err["data"]["preferred"] == [Backend.SEXPR.value]
    # Bytes untouched — tool never ran.
    assert sch.read_text(encoding="utf-8") == _SCH_WITH_R_SMALL


# -- local helper ---------------------------------------------------------


def _find_instance_by_uuid(root: SList, instance_uuid: str) -> SList | None:
    for child in root.items:
        if not isinstance(child, SList) or child.head != "symbol":
            continue
        uuid_node = child.find("uuid")
        if uuid_node is None or len(uuid_node.items) < 2:
            continue
        atom = uuid_node.items[1]
        if isinstance(atom, SAtom) and atom.text == instance_uuid:
            return child
    return None
