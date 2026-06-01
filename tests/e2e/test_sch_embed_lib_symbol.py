"""E2E JSON-RPC round-trip for M19 sch_embed_lib_symbol."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.sexpr.document import SexprDocument
from kimcp.tools.builtin.sch_add_symbol import _find_lib_symbol
from kimcp.tools.builtin.sch_embed_lib_symbol import SchEmbedLibSymbolTool

pytestmark = [pytest.mark.e2e]


_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols))
"""

_LIB = """\
(kicad_symbol_lib
\t(version 20231120)
\t(generator "kimcp-test")
\t(symbol "R_Test"
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "R"
\t\t\t(at 0 0 0)
\t\t\t(effects (font (size 1.27 1.27))))
\t\t(property "Value" "R_Test"
\t\t\t(at 0 0 0)
\t\t\t(effects (font (size 1.27 1.27))))
\t\t(symbol "R_Test_1_1"
\t\t\t(pin passive line
\t\t\t\t(at 0 2.54 270)
\t\t\t\t(length 0.508)
\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t(number "1" (effects (font (size 1.27 1.27))))))))
"""


def _write_sch(tmp_path: Path) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_SCH, encoding="utf-8")
    return sch


def _write_lib(tmp_path: Path) -> Path:
    lib = tmp_path / "Device.kicad_sym"
    lib.write_text(_LIB, encoding="utf-8")
    return lib


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


@pytest.mark.asyncio
async def test_tools_call_sch_embed_lib_symbol_ok(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)

    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchEmbedLibSymbolTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_embed_lib_symbol",
                    "arguments": {
                        "sch_path": str(sch),
                        "lib_path": str(lib),
                        "symbol_name": "R_Test",
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)

    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["lib_id"] == "Device:R_Test"
    assert result["meta"]["backend_used"] == Backend.SEXPR.value
    assert result["meta"]["snapshot_ref"].startswith("copy:")

    doc = SexprDocument.from_path(sch)
    lib_symbols = doc.root.find("lib_symbols")
    assert lib_symbols is not None
    assert _find_lib_symbol(lib_symbols, "Device:R_Test") is not None


@pytest.mark.asyncio
async def test_tools_call_sch_embed_lib_symbol_dry_run(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    before = sch.read_bytes()

    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchEmbedLibSymbolTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_embed_lib_symbol",
                    "arguments": {
                        "sch_path": str(sch),
                        "lib_path": str(lib),
                        "symbol_name": "R_Test",
                        "dry_run": True,
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "dry_run"
    assert result["lib_id"] == "Device:R_Test"
    assert sch.read_bytes() == before


@pytest.mark.asyncio
async def test_tools_call_sch_embed_lib_symbol_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    lib = _write_lib(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchEmbedLibSymbolTool())
    # No SEXPR availability.
    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_embed_lib_symbol",
                    "arguments": {
                        "sch_path": str(sch),
                        "lib_path": str(lib),
                        "symbol_name": "R_Test",
                    },
                },
            },
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
    assert sch.read_text(encoding="utf-8") == _SCH
