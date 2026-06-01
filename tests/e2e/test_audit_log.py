"""E2E — audit log writes through the full JSON-RPC dispatch path (M32).

Complements `tests/unit/test_server_audit.py`, which exercises the
dispatch seam directly. Here we go through ``dispatch_loop`` so a
regression that moved the audit emission out of ``_handle_tools_call``
— but into some branch the unit test's direct-invoke doesn't hit —
would surface.

The tool under test is ``sch_set_title_block`` (a MUTATE schematic tool
that's cheap to exercise without a real KiCAD install). Any MUTATE
tool would do; we pick this one because it's the oldest MUTATE we've
got, so the test doubles as a "nothing in the legacy MUTATE path
regressed" canary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from kimcp._types import Backend
from kimcp.config import load_config
from kimcp.rpc import dispatch_loop
from kimcp.safety.audit import audit_log_path
from kimcp.server import Server
from kimcp.tools.builtin.sch_set_title_block import SchSetTitleBlockTool

pytestmark = [pytest.mark.e2e]


_SCH_STUB = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
)
"""


def _cfg(tmp_path: Path, **safety_overrides: object):
    return load_config(
        user_global=tmp_path / "__n_u.toml",
        project_local=tmp_path / "__n_p.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(tmp_path / "nope-cli"),
                "ipc_socket": str(tmp_path / "no.sock"),
            },
            # Snapshot off: this project isn't a git repo and we don't want
            # copy-snapshot side effects cluttering the assertion on which
            # directories exist.
            "safety": {"snapshot_mode": "off", **safety_overrides},
        },
    )


def _write_sch(tmp_path: Path) -> Path:
    p = tmp_path / "demo.kicad_sch"
    p.write_text(_SCH_STUB, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_e2e_mutate_call_writes_audit_line(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path)
    server = Server(config=_cfg(tmp_path), project_root=tmp_path)
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
                        "title": "Audited Design",
                        "dry_run": False,
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)

    # Sanity — the call itself succeeded.
    result = cast(dict[str, object], transport.sent[0]["result"])
    assert result["isError"] is False

    # Audit log is the actual assertion.
    log_path = audit_log_path(tmp_path)
    assert log_path.is_file()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "sch_set_title_block"
    # Inputs made the summary, untruncated (they're all short).
    summary = cast(dict[str, object], entry["input_summary"])
    assert summary["title"] == "Audited Design"
    assert summary["dry_run"] is False
    # Timestamp shape — ISO-8601 UTC with Z suffix.
    assert isinstance(entry["ts"], str)
    assert entry["ts"].endswith("Z")


@pytest.mark.asyncio
async def test_e2e_audit_disabled_leaves_no_log(
    tmp_path: Path, memory_transport_factory
) -> None:
    """`safety.audit_enabled=False` → `.kimcp/audit.log` never created."""
    sch = _write_sch(tmp_path)
    server = Server(
        config=_cfg(tmp_path, audit_enabled=False),
        project_root=tmp_path,
    )
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
                        "title": "Quiet",
                        "dry_run": False,
                    },
                },
            }
        ]
    )
    await dispatch_loop(transport, server.handler)

    assert not audit_log_path(tmp_path).exists()
