"""KiCAD IPC surface — socket discovery + reachability probe.

Per ADR-0014 the IPC API is the primary mutation backend; per ADR-0015
the transport is **protobuf-over-nng** (pynng Req/Rep), not gRPC. This
package owns the machinery the backend adapter composes: resolving the
socket path, opening a throwaway connection to verify something's
listening, and the typed error hierarchy for failure modes. Real RPC
calls live in a future submodule and import ``pynng`` + ``kipy.proto.*``
lazily from the optional ``ipc`` extra (``kicad-python``), so the core
stays importable without it.
"""

from __future__ import annotations

from kimcp.ipc.errors import (
    IpcCallError,
    IpcError,
    IpcProbeTimeoutError,
    IpcSocketUnreachableError,
)
from kimcp.ipc.probe import DEFAULT_PROBE_TIMEOUT_SEC, probe_socket
from kimcp.ipc.socket import resolve_ipc_socket

__all__ = [
    "DEFAULT_PROBE_TIMEOUT_SEC",
    "IpcCallError",
    "IpcError",
    "IpcProbeTimeoutError",
    "IpcSocketUnreachableError",
    "probe_socket",
    "resolve_ipc_socket",
]
