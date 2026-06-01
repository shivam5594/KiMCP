"""E2E for sch_list_labels."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.errors import BACKEND_UNAVAILABLE
from kimcp.rpc import dispatch_loop
from kimcp.server import Server
from kimcp.tools.builtin.sch_list_labels import SchListLabelsTool

pytestmark = [pytest.mark.e2e]


_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols)
\t(label "CLK"
\t\t(at 50 50 0)
\t\t(effects (font (size 1.27 1.27)) (justify left bottom))
\t\t(uuid "11111111-0000-0000-0000-000000000001")
\t)
\t(global_label "VCC"
\t\t(shape input)
\t\t(at 60 50 0)
\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t(uuid "22222222-0000-0000-0000-000000000001")
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
    server.register_tool(SchListLabelsTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_list_labels",
                    "arguments": {"sch_path": str(sch)},
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["total"] == 2
    kinds = {label["kind"] for label in result["labels"]}
    assert kinds == {"local", "global"}
    assert result["meta"]["backend_used"] == Backend.SEXPR.value


@pytest.mark.asyncio
async def test_e2e_filter_kind(tmp_path: Path, memory_transport_factory) -> None:
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchListLabelsTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_list_labels",
                    "arguments": {
                        "sch_path": str(sch),
                        "kind": "global",
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    result = transport.sent[0]["result"]["structuredContent"]
    assert result["status"] == "ok"
    assert result["total"] == 1
    assert result["labels"][0]["text"] == "VCC"


@pytest.mark.asyncio
async def test_e2e_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
    server.register_tool(SchListLabelsTool())
    # No SEXPR availability marked.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_list_labels",
                    "arguments": {"sch_path": str(sch)},
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)
    err = transport.sent[0]["error"]
    assert err["code"] == BACKEND_UNAVAILABLE
