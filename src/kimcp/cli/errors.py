"""Typed errors for `kicad-cli` interactions.

Higher layers should never have to string-match stderr to tell "cli ran
but failed" from "cli hung". Each is a distinct exception here.

Note: "cli not installed" is deliberately *not* an exception type today.
`resolve_cli_path` returns `None` and `CliBackend.probe` returns `False`
for that case — consistent with every other backend's probe contract
(IPC/SEXPR/SWIG all just return bool). A `CliNotFoundError` will be
reintroduced when the dispatcher needs to distinguish "no binary" from
"binary ran and failed" at the tool-result surface; until then it would
be dead API.
"""

from __future__ import annotations


class CliError(Exception):
    """Base class for all kicad-cli errors."""


class CliTimeoutError(CliError):
    """Raised when a kicad-cli invocation exceeded the configured timeout."""

    def __init__(self, message: str, *, argv: tuple[str, ...], timeout: float) -> None:
        super().__init__(message)
        self.argv = argv
        self.timeout = timeout


class CliNonZeroError(CliError):
    """Raised when `kicad-cli` exits with a non-zero status.

    Carries the structured result so callers can surface stderr back to
    the user and decide whether the failure is recoverable.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: tuple[str, ...],
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(message)
        self.argv = argv
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


__all__ = ["CliError", "CliNonZeroError", "CliTimeoutError"]
