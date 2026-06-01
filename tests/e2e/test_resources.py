"""End-to-end: JSON-RPC ``resources/list`` + ``resources/read`` (M13).

Exercises the full MCP resources primitive through the in-memory transport:

* ``initialize`` advertises the ``resources`` capability with the expected
  flags (``listChanged=false``, ``subscribe=false``).
* ``resources/list`` returns the KiCAD files discovered under the server's
  ``project_root``.
* ``resources/read`` returns the text content of a valid URI and rejects
  bad URIs with ``INVALID_PARAMS``.

This is the first non-``tools/*`` method family to land on the handler; pin
it carefully so future additions (prompts, roots) can copy the pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp.config import load_config
from kimcp.errors import INVALID_PARAMS
from kimcp.rpc import dispatch_loop
from kimcp.server import Server

pytestmark = [pytest.mark.e2e]


_SCH_BODY = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols))
"""


def _write_sch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SCH_BODY, encoding="utf-8")
    return path


def _bare_config(tmp_path: Path):
    """Config that won't reach out to real KiCAD backends."""
    return load_config(
        user_global=tmp_path / "__nope_user.toml",
        project_local=tmp_path / "__nope_project.toml",
        session_overrides={
            "kicad": {
                "cli_exe": str(tmp_path / "nonexistent-cli"),
                "ipc_socket": str(tmp_path / "not-a-socket.sock"),
            },
        },
    )


@pytest.mark.asyncio
async def test_initialize_advertises_resources_capability(
    tmp_path: Path, memory_transport_factory
) -> None:
    server = Server(config=_bare_config(tmp_path), project_root=tmp_path)

    transport = memory_transport_factory(
        [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
    )
    await dispatch_loop(transport, server.handler)

    caps = transport.sent[0]["result"]["capabilities"]
    # tools stays wired on; resources is newly advertised with the two
    # MCP-spec flags flipped off since we don't support subscription or
    # listChanged notifications yet.
    assert caps["tools"] == {"listChanged": False}
    assert caps["resources"] == {"listChanged": False, "subscribe": False}


@pytest.mark.asyncio
async def test_resources_list_roundtrip(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path / "board.kicad_sch")
    _write_sch(tmp_path / "sub" / "nested.kicad_sch")

    server = Server(config=_bare_config(tmp_path), project_root=tmp_path)

    transport = memory_transport_factory(
        [{"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}}]
    )
    await dispatch_loop(transport, server.handler)

    result = transport.sent[0]["result"]
    resources = result["resources"]
    # Discovery walks subdirs; ordering is POSIX-rel-path sorted.
    names = [r["name"] for r in resources]
    assert names == ["board.kicad_sch", "sub/nested.kicad_sch"]
    # URIs round-trip to the on-disk files.
    assert resources[0]["uri"] == sch.resolve().as_uri()
    # Spec-shaped descriptor fields are all present.
    for entry in resources:
        assert entry["mimeType"] == "application/x-kicad-schematic"
        assert "description" in entry
        assert "size" in entry


@pytest.mark.asyncio
async def test_resources_read_roundtrip(
    tmp_path: Path, memory_transport_factory
) -> None:
    sch = _write_sch(tmp_path / "board.kicad_sch")

    server = Server(config=_bare_config(tmp_path), project_root=tmp_path)

    transport = memory_transport_factory(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {"uri": sch.resolve().as_uri()},
            }
        ]
    )
    await dispatch_loop(transport, server.handler)

    result = transport.sent[0]["result"]
    assert len(result["contents"]) == 1
    item = result["contents"][0]
    assert item["uri"] == sch.resolve().as_uri()
    assert item["mimeType"] == "application/x-kicad-schematic"
    assert item["text"] == _SCH_BODY


@pytest.mark.asyncio
async def test_resources_read_rejects_out_of_root_uri(
    tmp_path: Path, memory_transport_factory
) -> None:
    """Path-traversal attempt comes back as a JSON-RPC error, not silent empty."""
    outside = tmp_path.parent / f"{tmp_path.name}__sibling.kicad_sch"
    outside.write_text(_SCH_BODY, encoding="utf-8")
    try:
        server = Server(config=_bare_config(tmp_path), project_root=tmp_path)

        transport = memory_transport_factory(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "resources/read",
                    "params": {"uri": outside.resolve().as_uri()},
                }
            ]
        )
        await dispatch_loop(transport, server.handler)

        msg = transport.sent[0]
        assert "error" in msg
        err = msg["error"]
        assert err["code"] == INVALID_PARAMS
        assert "outside the project root" in err["message"]
    finally:
        outside.unlink()


@pytest.mark.asyncio
async def test_resources_read_rejects_missing_uri_param(
    tmp_path: Path, memory_transport_factory
) -> None:
    server = Server(config=_bare_config(tmp_path), project_root=tmp_path)

    transport = memory_transport_factory(
        [{"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {}}]
    )
    await dispatch_loop(transport, server.handler)

    err = transport.sent[0]["error"]
    assert err["code"] == INVALID_PARAMS
    assert "'uri' is required" in err["message"]
