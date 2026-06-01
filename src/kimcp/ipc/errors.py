"""Typed errors for the KiCAD IPC surface.

Higher layers should not need to string-match the OS-level error that came
back from a failed socket connect. Each failure mode gets a distinct class
here so callers can decide: retry, escalate, or tell the user to start
KiCAD.

Note: "socket not configured / not discoverable" is deliberately *not* an
exception type. `resolve_ipc_socket` returns `None` and `IpcBackend.probe`
returns `False` for that case — same contract as `CliBackend`, where
"CLI not installed" is also a boolean signal rather than a raise. See
`kimcp.cli.errors` for the matching rationale.
"""

from __future__ import annotations


class IpcError(Exception):
    """Base class for all KiCAD IPC interaction errors."""


class IpcSocketUnreachableError(IpcError):
    """Raised when the socket path exists but we cannot connect to it.

    Typical cause: KiCAD isn't running but left a stale socket, or another
    process is holding the socket without implementing the IPC server.
    """

    def __init__(self, message: str, *, socket_path: str) -> None:
        super().__init__(message)
        self.socket_path = socket_path


class IpcProbeTimeoutError(IpcError):
    """Raised when a probe attempt exceeded the configured timeout.

    Distinct from `IpcSocketUnreachableError`: the socket responded to the
    connect() syscall but the subsequent handshake / accept took too long.
    Signals a wedged KiCAD rather than a missing one.
    """

    def __init__(self, message: str, *, socket_path: str, timeout: float) -> None:
        super().__init__(message)
        self.socket_path = socket_path
        self.timeout = timeout


class IpcCallError(IpcError):
    """Raised when KiCAD returned a non-OK ``ApiStatusCode`` for an RPC call.

    The transport was healthy (we sent a request, we got an envelope back),
    but the server rejected the call. Carries the numeric ``ApiStatusCode``
    and any ``error_message`` string KiCAD attached so the diagnostic layer
    can distinguish "bad request" (4xx-ish) from "not ready" / "busy" /
    "token mismatch" and react accordingly.

    Kept separate from :class:`IpcSocketUnreachableError` /
    :class:`IpcProbeTimeoutError` because the remedy is different:
    a non-OK status means *the RPC itself* was refused — reconnecting or
    waiting won't help unless the status code specifically says so.

    ``status_code`` is an ``int`` rather than a typed enum to keep the
    error class importable without a hard dep on ``kipy.proto.*``. The
    enum values live in :mod:`kipy.proto.common.envelope_pb2` (see
    ``ApiStatusCode``); callers that want to branch on them should do the
    import locally next to the branch. Keeping protobuf out of the error
    hierarchy preserves the "no optional-extra imports at package load
    time" invariant (pinned by test_ipc_package_does_not_import_optional_extras).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_message: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_message = error_message


__all__ = [
    "IpcCallError",
    "IpcError",
    "IpcProbeTimeoutError",
    "IpcSocketUnreachableError",
]
