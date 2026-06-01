"""End-to-end: JSON-RPC tools/call → sch_set_title_block (M12).

First mutation tool to land, so this e2e is the template every future
schematic-mutation e2e will copy. Exercises the full path:

    Server boots from Config
      → registers SchSetTitleBlockTool
      → dispatcher gates on Backend.SEXPR availability
      → client calls `tools/call` via the in-memory transport
      → tool reads the .kicad_sch, mutates title_block in memory
      → snapshot runs (copy-mode here — greenfield tmp_path is not a repo)
      → atomic SexprDocument.save writes the new bytes
      → envelope comes back with status='ok' + fields_changed + snapshot_ref

No kicad-cli stub is involved — M12 is SEXPR-backed, not CLI-backed. That
asymmetry with earlier e2e tests is load-bearing: it confirms the
dispatcher routes a sexpr-preferred tool correctly.

Three pins, mirroring the sibling e2e tests:
* Happy path: JSON-RPC → mutation lands on disk.
* Dry-run round-trips through the transport without writing.
* ``Backend.SEXPR`` unavailable → ``BACKEND_UNAVAILABLE`` before tool runs.
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
from kimcp.tools.builtin.sch_set_title_block import SchSetTitleBlockTool

pytestmark = [pytest.mark.e2e]


_SCH_WITH_TITLE_BLOCK = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(title_block
\t\t(title "Old Title")
\t\t(date "2024-01-01")
\t\t(rev "A")
\t\t(company "Acme")
\t\t(comment 1 "one"))
\t(lib_symbols))
"""


def _write_sch(tmp_path: Path) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_SCH_WITH_TITLE_BLOCK, encoding="utf-8")
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
            "safety": {"snapshot_mode": snapshot_mode},
        },
    )


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_sch_set_title_block_ok(
    tmp_path: Path, memory_transport_factory
) -> None:
    """title + rev + comment2 land on disk; fields_changed is echoed back."""
    sch = _write_sch(tmp_path)

    server = Server(config=_load_config(tmp_path, snapshot_mode="copy"))
    server.register_tool(SchSetTitleBlockTool())
    # SexprBackend probe is unconditionally True, but the dispatcher only
    # consults `availability` — which is empty until probed or marked.
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_set_title_block",
                    "arguments": {
                        "sch_path": str(sch),
                        "title": "Main 5V Rail",
                        "rev": "B",
                        "comment2": "reviewed 2026-04-15",
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
    assert set(result["fields_changed"]) == {"title", "rev", "comment2"}
    # Dispatcher ran → backend_used stamped.
    assert result["meta"]["backend_used"] == Backend.SEXPR.value
    # Copy-mode snapshot → meta.snapshot_ref points at a real directory.
    snap_ref = result["meta"]["snapshot_ref"]
    assert snap_ref.startswith("copy:")
    assert Path(snap_ref[len("copy:") :]).is_dir()

    # Verify mutation actually reached the file — round-trip by reparsing.
    doc = SexprDocument.from_path(sch)
    tb = doc.root.find("title_block")
    assert tb is not None
    assert tb.find("title").items[1].text == "Main 5V Rail"  # type: ignore[union-attr]
    assert tb.find("rev").items[1].text == "B"  # type: ignore[union-attr]
    # comment2 was added fresh; comment1 untouched.
    by_slot = {
        int(c.items[1].text): c.items[2].text  # type: ignore[union-attr]
        for c in tb.find_all("comment")
    }
    assert by_slot[1] == "one"
    assert by_slot[2] == "reviewed 2026-04-15"


# -- dry-run round-trips through JSON-RPC ---------------------------------


@pytest.mark.asyncio
async def test_tools_call_sch_set_title_block_dry_run(
    tmp_path: Path, memory_transport_factory
) -> None:
    """``dry_run=True`` returns fields_changed without mutating the file."""
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()

    server = Server(config=_load_config(tmp_path, snapshot_mode="copy"))
    server.register_tool(SchSetTitleBlockTool())
    server.availability.mark(Backend.SEXPR, True)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_set_title_block",
                    "arguments": {
                        "sch_path": str(sch),
                        "title": "Would Change",
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
    assert result["fields_changed"] == ["title"]
    # No snapshot taken on dry_run — preserves the contract that
    # snapshot happens iff the file is actually about to change.
    assert result["meta"]["snapshot_ref"] is None
    # File untouched byte-for-byte.
    assert sch.read_bytes() == before


# -- dispatcher gate fires when SEXPR is unavailable ----------------------


@pytest.mark.asyncio
async def test_tools_call_sch_set_title_block_backend_unavailable(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Dispatcher raises BACKEND_UNAVAILABLE when Backend.SEXPR isn't marked.

    SEXPR is pure-Python and its probe is unconditionally True, but the
    dispatcher consults availability — not the probe — so a server that
    never probed rejects sexpr tools. This pins that contract.
    """
    sch = _write_sch(tmp_path)

    server = Server(config=_load_config(tmp_path))
    server.register_tool(SchSetTitleBlockTool())
    # Do NOT mark SEXPR available — dispatcher should reject the call.

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "sch_set_title_block",
                    "arguments": {"sch_path": str(sch), "title": "Never Applied"},
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
    # File untouched — the tool never ran.
    assert sch.read_text(encoding="utf-8") == _SCH_WITH_TITLE_BLOCK
