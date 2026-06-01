"""Unit tests for `CliBackend` — probe caching + min_version gating.

Uses a small shell-stub approach: we write a tiny Python script that
emits a KiCAD-style `kicad-cli version` banner and make it executable.
The backend's `configured_path` then points at the stub, so the whole
code path (resolve → run → parse → gate) runs for real — just without
needing KiCAD installed.
"""

from __future__ import annotations

import stat
import sys
import textwrap
from pathlib import Path

import pytest

from kimcp.backends.cli import CliBackend


def _write_stub(tmp_path: Path, version_line: str, *, exit_code: int = 0) -> Path:
    """Write an executable stub that emulates `kicad-cli version` output."""
    stub = tmp_path / "kicad-cli-stub"
    script = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import sys
        sys.stdout.write({version_line!r})
        sys.exit({exit_code})
        """
    )
    stub.write_text(script)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


VERSION_OK = "Application: kicad-cli\nVersion: 9.0.1+9.0.0-0-10.fc40, release build\n"
VERSION_TOO_OLD = "Application: kicad-cli\nVersion: 8.0.5, release build\n"
VERSION_NIGHTLY_10 = "Application: kicad-cli\nVersion: 10.0.0-rc1, development build\n"
VERSION_GARBAGE = "what even is this output\n"


@pytest.mark.asyncio
async def test_probe_true_when_version_meets_min(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, VERSION_OK)
    backend = CliBackend(configured_path=str(stub), min_version="9.0.0")
    assert await backend.probe() is True
    assert backend.detected_version is not None
    assert backend.detected_version.as_tuple() == (9, 0, 1)
    assert backend.cli_path == str(stub.resolve())


@pytest.mark.asyncio
async def test_probe_false_when_version_below_min(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, VERSION_TOO_OLD)
    backend = CliBackend(configured_path=str(stub), min_version="9.0.0")
    assert await backend.probe() is False
    # Version still detected — so diagnostics can show the mismatch.
    assert backend.detected_version is not None
    assert backend.detected_version.as_tuple() == (8, 0, 5)


@pytest.mark.asyncio
async def test_probe_true_for_newer_major(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, VERSION_NIGHTLY_10)
    backend = CliBackend(configured_path=str(stub), min_version="9.0.0")
    assert await backend.probe() is True
    assert backend.detected_version is not None
    assert backend.detected_version.as_tuple() == (10, 0, 0)


@pytest.mark.asyncio
async def test_probe_false_when_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    backend = CliBackend(configured_path=str(missing), min_version="9.0.0")
    assert await backend.probe() is False
    assert backend.cli_path is None
    assert backend.detected_version is None


@pytest.mark.asyncio
async def test_probe_false_on_unparseable_output(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, VERSION_GARBAGE)
    backend = CliBackend(configured_path=str(stub), min_version="9.0.0")
    assert await backend.probe() is False
    assert backend.detected_version is None


@pytest.mark.asyncio
async def test_probe_false_when_cli_exits_nonzero(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, "kaboom\n", exit_code=2)
    backend = CliBackend(configured_path=str(stub), min_version="9.0.0")
    assert await backend.probe() is False


@pytest.mark.asyncio
async def test_probe_caches_first_result(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, VERSION_OK)
    backend = CliBackend(configured_path=str(stub), min_version="9.0.0")
    first = await backend.probe()
    # Delete the stub to prove the second call doesn't re-exec.
    stub.unlink()
    second = await backend.probe()
    assert first is True
    assert second is True


@pytest.mark.asyncio
async def test_probe_refresh_rechecks(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, VERSION_OK)
    backend = CliBackend(configured_path=str(stub), min_version="9.0.0")
    assert await backend.probe() is True

    stub.unlink()
    assert await backend.probe(refresh=True) is False
    assert backend.cli_path is None


def test_invalid_min_version_rejected() -> None:
    with pytest.raises(ValueError, match="invalid min_version"):
        CliBackend(min_version="not-a-version")


# -- audit regressions -----------------------------------------------------


def test_init_does_not_resolve_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins audit-fix I3: `__init__` must not touch the filesystem.

    `resolve_cli_path` only runs inside `probe()` so constructing a backend
    (e.g. during server startup) stays pure and never throws surprising
    FS errors. We spy on `resolve_cli_path` and assert zero calls until
    `probe()` is awaited.
    """
    calls: list[str] = []

    def spy_resolve(configured: str) -> None:
        calls.append(configured)
        return None

    monkeypatch.setattr("kimcp.backends.cli.resolve_cli_path", spy_resolve)

    backend = CliBackend(configured_path="/definitely/not/a/real/path", min_version="9.0.0")

    # Constructor did NOT call the resolver.
    assert calls == []
    # And the observable probe-outputs are in their initial (lazy) state.
    assert backend.cli_path is None
    assert backend.detected_version is None
    assert backend.probed is False


@pytest.mark.asyncio
async def test_min_version_change_gates_same_binary(tmp_path: Path) -> None:
    """Two backends sharing the same stub binary but differing in
    `min_version` must reach different availability conclusions. This
    pins the gate arithmetic — a regression that drops the comparison
    (e.g. by always returning True after a successful parse) would let
    this pass spuriously otherwise."""
    stub = _write_stub(tmp_path, "Application: kicad-cli\nVersion: 9.0.5, release build\n")

    permissive = CliBackend(configured_path=str(stub), min_version="9.0.0")
    assert await permissive.probe() is True
    assert permissive.detected_version is not None
    assert permissive.detected_version.as_tuple() == (9, 0, 5)

    strict = CliBackend(configured_path=str(stub), min_version="9.0.6")
    assert await strict.probe() is False
    # Detected version still populated even when gated out — diagnostics
    # need to show the mismatch, not hide it.
    assert strict.detected_version is not None
    assert strict.detected_version.as_tuple() == (9, 0, 5)

    future = CliBackend(configured_path=str(stub), min_version="10.0.0")
    assert await future.probe() is False
    assert future.detected_version is not None
    assert future.detected_version.as_tuple() == (9, 0, 5)
