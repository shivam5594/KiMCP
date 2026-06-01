"""Unit tests for the async subprocess runner.

We use `sys.executable -c '...'` as a cross-platform stand-in for
`kicad-cli` — it exits deterministically, writes predictable stdout/
stderr, and honors a sleep for timeout tests. That keeps these tests
real-subprocess without depending on kicad being installed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kimcp.cli.errors import CliNonZeroError, CliTimeoutError
from kimcp.cli.runner import run_cli

PY = Path(sys.executable)


@pytest.mark.asyncio
async def test_zero_exit_captures_stdout_and_stderr() -> None:
    result = await run_cli(
        (
            "-c",
            "import sys; sys.stdout.write('hello out'); sys.stderr.write('hello err')",
        ),
        cli_path=PY,
    )
    assert result.exit_code == 0
    assert result.stdout == "hello out"
    assert result.stderr == "hello err"
    assert result.argv[0] == str(PY)
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_non_zero_exit_raises_by_default() -> None:
    with pytest.raises(CliNonZeroError) as excinfo:
        await run_cli(
            ("-c", "import sys; sys.stderr.write('boom'); sys.exit(7)"),
            cli_path=PY,
        )
    err = excinfo.value
    assert err.exit_code == 7
    assert "boom" in err.stderr


@pytest.mark.asyncio
async def test_non_zero_exit_returned_when_check_false() -> None:
    result = await run_cli(
        ("-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"),
        cli_path=PY,
        check=False,
    )
    assert result.exit_code == 3
    assert "boom" in result.stderr


@pytest.mark.asyncio
async def test_timeout_raises_cli_timeout_error() -> None:
    # Sleep 30s; timeout 0.3s — child gets killed and we raise.
    with pytest.raises(CliTimeoutError) as excinfo:
        await run_cli(
            ("-c", "import time; time.sleep(30)"),
            cli_path=PY,
            timeout=0.3,
        )
    assert excinfo.value.timeout == 0.3
    assert str(PY) in excinfo.value.argv[0]


@pytest.mark.asyncio
async def test_cwd_is_honored(tmp_path: Path) -> None:
    result = await run_cli(
        ("-c", "import os; print(os.getcwd())"),
        cli_path=PY,
        cwd=tmp_path,
    )
    # On macOS tmp_path resolves through /private/var/folders/…; compare
    # realpath'd strings. (ASYNC240 is ignored for tests in pyproject.toml.)
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(str(tmp_path))


@pytest.mark.asyncio
async def test_env_is_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMCP_BASE", "base-value")
    result = await run_cli(
        ("-c", "import os; print(os.environ.get('KIMCP_BASE'), os.environ.get('KIMCP_EXTRA'))"),
        cli_path=PY,
        env={"KIMCP_EXTRA": "extra-value"},
    )
    # The parent env is preserved (KIMCP_BASE), and the extra is layered on.
    assert result.stdout.strip() == "base-value extra-value"


# -- timeout kill-path regression (pins audit-fix I2) ----------------------


class _FakeProc:
    """Minimal stand-in for `asyncio.subprocess.Process`.

    `communicate()` blocks forever so `asyncio.wait_for` raises TimeoutError.
    `kill()` is parameterized to optionally raise an OSError subclass to
    simulate unusually-parented children. `wait()` records that it ran so
    the test can assert the runner still reaps after a failed kill.
    """

    def __init__(self, *, kill_raises: BaseException | None = None) -> None:
        self.pid = 424242
        self._kill_raises = kill_raises
        self.kill_called = False
        self.wait_called = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        # Block past any sane test timeout so the caller's wait_for fires.
        await asyncio.sleep(60)
        return b"", b""

    def kill(self) -> None:
        self.kill_called = True
        if self._kill_raises is not None:
            raise self._kill_raises

    async def wait(self) -> int:
        self.wait_called = True
        self.returncode = -9
        return -9


@pytest.mark.asyncio
async def test_timeout_still_raises_when_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins audit-fix I2: widened OSError catch on `proc.kill()`.

    If `kill()` raises (e.g., PermissionError on a foreign child, or
    platform-specific OSError), the runner must STILL:
      1. Attempt `proc.wait()` so the event loop can reap the child.
      2. Raise `CliTimeoutError` to the caller — never silently swallow.
    """
    fake = _FakeProc(kill_raises=PermissionError("not your child"))

    async def fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return fake

    monkeypatch.setattr("kimcp.cli.runner.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(CliTimeoutError) as excinfo:
        await run_cli(("-c", "blocked"), cli_path=PY, timeout=0.05)

    assert excinfo.value.timeout == 0.05
    # The guard ran — kill was attempted even though it raised.
    assert fake.kill_called is True
    # And we still reaped (or tried to reap) the child before raising.
    assert fake.wait_called is True


@pytest.mark.asyncio
async def test_timeout_normal_kill_path_also_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric: the happy kill path (kill() returns cleanly) still raises
    CliTimeoutError and reaps the child. Guards against a regression where
    the I2 fix accidentally drops the raise on the clean branch."""
    fake = _FakeProc(kill_raises=None)

    async def fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return fake

    monkeypatch.setattr("kimcp.cli.runner.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(CliTimeoutError):
        await run_cli(("-c", "blocked"), cli_path=PY, timeout=0.05)

    assert fake.kill_called is True
    assert fake.wait_called is True
