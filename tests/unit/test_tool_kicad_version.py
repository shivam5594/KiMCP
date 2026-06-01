"""Unit tests for the `kicad_version` built-in tool.

Covers the three output states (found / too_old / not_found) by pointing
the injected CliBackend at an executable stub, an old-version stub, and
a missing path respectively.
"""

from __future__ import annotations

import stat
import sys
import textwrap
from pathlib import Path
from typing import cast

import pytest

from kimcp._types import Backend
from kimcp.backends.cli import CliBackend
from kimcp.cli.version import KiCadVersion
from kimcp.tools.builtin.kicad_version import KiCadVersionInput, KiCadVersionTool


def _write_stub(tmp_path: Path, version_line: str) -> Path:
    stub = tmp_path / "kicad-cli-stub"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys
            sys.stdout.write({version_line!r})
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


@pytest.mark.asyncio
async def test_status_found_when_version_ok(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, "Application: kicad-cli\nVersion: 9.0.1, release build\n")
    tool = KiCadVersionTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))

    out = await tool.run(KiCadVersionInput())
    assert out.status == "found"
    assert out.detected_version == "9.0.1"
    assert "9.0.1" in (out.detected_version_raw or "")
    assert out.cli_path == str(stub.resolve())
    assert out.min_version == "9.0.0"


@pytest.mark.asyncio
async def test_status_too_old_when_below_min(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, "Application: kicad-cli\nVersion: 8.0.5, release build\n")
    tool = KiCadVersionTool()
    tool.set_cli_backend(CliBackend(configured_path=str(stub), min_version="9.0.0"))

    out = await tool.run(KiCadVersionInput())
    assert out.status == "too_old"
    assert out.detected_version == "8.0.5"
    assert out.cli_path == str(stub.resolve())


@pytest.mark.asyncio
async def test_status_not_found_when_path_missing(tmp_path: Path) -> None:
    tool = KiCadVersionTool()
    tool.set_cli_backend(CliBackend(configured_path=str(tmp_path / "nope"), min_version="9.0.0"))

    out = await tool.run(KiCadVersionInput())
    assert out.status == "not_found"
    assert out.cli_path is None
    assert out.detected_version is None
    assert out.detected_version_raw is None


@pytest.mark.asyncio
async def test_tool_without_injection_builds_default_backend(tmp_path: Path) -> None:
    """Bare entry-point load: `run` must not crash even when server-side
    dependency injection hasn't happened yet."""
    tool = KiCadVersionTool()
    out = await tool.run(KiCadVersionInput())
    # Status is whatever the host has installed; assert the shape only.
    assert out.status in {"found", "too_old", "not_found"}
    assert out.min_version  # always populated from the default backend config


# -- probe-error propagation (design pin) ----------------------------------


class _ExplodingBackend:
    """Stand-in for CliBackend whose probe() raises an unexpected error.

    We intentionally do NOT inherit from CliBackend — the point of the
    test is that the tool depends only on a narrow protocol (probe +
    accessors) and surfaces anything unusual to the caller.
    """

    kind = Backend.CLI

    def __init__(self) -> None:
        parsed = KiCadVersion.parse("9.0.0")
        assert parsed is not None
        self._min = parsed

    @property
    def cli_path(self) -> str | None:
        return None

    @property
    def detected_version(self) -> KiCadVersion | None:
        return None

    @property
    def min_version(self) -> KiCadVersion:
        return self._min

    @property
    def probed(self) -> bool:
        return False

    async def probe(self, *, refresh: bool = False) -> bool:
        raise RuntimeError("synthetic probe failure")


@pytest.mark.asyncio
async def test_tool_propagates_unexpected_probe_errors() -> None:
    """Unexpected (non-CliError) probe failures must propagate.

    CliError subclasses are already handled inside `CliBackend.probe` and
    collapse to `status='not_found' / 'too_old'`. Anything that escapes
    the backend is a programming bug (e.g. a future refactor that forgets
    to catch a new error type). Hiding such bugs behind `not_found` would
    make triage miserable — we'd just see 'kicad missing' reports on hosts
    that have it installed. Surface loudly; the MCP layer translates the
    raise into a protocol error for the caller.
    """
    tool = KiCadVersionTool()
    # Cheat past the setter's CliBackend type annotation: the backend here
    # satisfies the protocol the tool actually uses (probe + properties),
    # which is exactly the point — the tool doesn't rely on subclassing.
    tool.set_cli_backend(cast(CliBackend, _ExplodingBackend()))

    with pytest.raises(RuntimeError, match="synthetic probe failure"):
        await tool.run(KiCadVersionInput())
