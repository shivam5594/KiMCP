"""Async `kicad-cli` runner.

Thin layer over `asyncio.create_subprocess_exec` that adds:

* **Timeouts** — defaults to 60 s per call, overridable per op. Long
  exports (Gerbers on a large board) raise it; short queries (`version`)
  usually set a much shorter per-call timeout from the caller side.
* **Structured results** — `CliResult(exit_code, stdout, stderr, ...)`
  so the caller never has to shell-parse.
* **Non-zero defaults to exception** — matches how nearly every caller
  actually wants to treat failure. Callers that want to inspect a
  non-zero exit pass `check=False` and inspect the `CliResult`
  directly.

We deliberately do not stream output in this layer. Streaming is a
transport-level concern (see `performance.md` §Streaming) and will be
plumbed into the HTTP+SSE transport when it lands; the CLI runner then
exposes a streaming variant alongside this one. For the operations we
ship early (probe, version, short exports), collect-and-return is
simpler and has no downside.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from kimcp.cli.errors import CliNonZeroError, CliTimeoutError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 60.0


@dataclass(frozen=True)
class CliResult:
    """Result of a kicad-cli invocation."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


async def run_cli(
    args: Sequence[str],
    *,
    cli_path: Path,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> CliResult:
    """Run `kicad-cli <args>` and return the structured result.

    Args:
        args: The subcommand + flags (e.g. `("version",)` or
            `("pcb", "export", "gerbers", ...)`).
        cli_path: Absolute path to the `kicad-cli` binary — obtained
            once at probe time from `resolve_cli_path`.
        cwd: Working directory for the subprocess. Defaults to the
            current process's cwd.
        timeout: Seconds before raising `CliTimeoutError`. The child is
            killed if it exceeds this.
        env: Extra env vars merged on top of `os.environ`. Pass None for
            the usual behavior.
        check: If True (default), raise `CliNonZeroError` on non-zero
            exit. If False, return the result either way.
    """
    argv = (str(cli_path), *args)
    log.debug("run_cli: %s (cwd=%s, timeout=%ss)", _shlex_join(argv), cwd, timeout)

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        env=_merged_env(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        # Best-effort kill + reap. We catch OSError broadly (not just
        # ProcessLookupError) because `proc.kill()` can surface PermissionError
        # on unusually-parented children and miscellaneous OSErrors on platforms
        # where the PID has already been recycled. Whatever happens, we *still*
        # attempt `proc.wait()` afterwards so the event loop can reap the child
        # (or detect a true zombie within the 1 s grace) before we raise.
        try:
            proc.kill()
        except OSError as kill_exc:
            log.warning(
                "cli child %s: kill() raised %r; still attempting wait()", proc.pid, kill_exc
            )
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except TimeoutError:
            log.warning("cli child %s refused to die within 1s of kill()", proc.pid)
        raise CliTimeoutError(
            f"kicad-cli {_shlex_join(args)} exceeded {timeout}s timeout",
            argv=argv,
            timeout=timeout,
        ) from exc

    duration_ms = int((time.monotonic() - start) * 1000)

    result = CliResult(
        argv=argv,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        duration_ms=duration_ms,
    )

    if check and result.exit_code != 0:
        raise CliNonZeroError(
            f"kicad-cli {_shlex_join(args)} exited {result.exit_code}",
            argv=argv,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result


def _merged_env(extra: Mapping[str, str] | None) -> Mapping[str, str] | None:
    if extra is None:
        return None
    merged = dict(os.environ)
    merged.update(extra)
    return merged


def _shlex_join(parts: Sequence[str]) -> str:
    # Python 3.8+ has shlex.join; use it for debug-log readability.
    return shlex.join(parts)


__all__ = ["DEFAULT_TIMEOUT_SEC", "CliResult", "run_cli"]
