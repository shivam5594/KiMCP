"""kicad_ipc_status — reports whether the KiCAD IPC socket is reachable.

Thin wrapper around :class:`IpcBackend`. The server injects the backend
via ``set_ipc_backend`` post-construction so the tool reflects the live
probe result instead of re-opening the socket. If the backend hasn't
been probed yet (or the tool runs before the server wired it up), we
probe on demand so CLI callers like ``kimcp-cli tool run
kicad_ipc_status`` still work.

Output distinguishes three states:

* **reachable**  — a process accepted a connection on the socket.
* **unreachable** — the socket exists (or its path resolved) but we
  couldn't open a connection. Usually means KiCAD isn't running, or the
  socket is stale from a previous session.
* **not_found**  — no socket discovered. Check that KiCAD ≥ 9 is
  installed, running, and (if needed) that ``kicad.ipc_socket`` is set.

Each state carries enough context (``socket_path``, ``note``) for the
user to act without having to re-run with verbose logging.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kimcp._types import ToolClass
from kimcp.backends.ipc import IpcBackend
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool


class KiCadIpcStatusInput(BaseModel):
    pass


class KiCadIpcStatusOutput(ToolOutput):
    status: Literal["reachable", "unreachable", "not_found"]
    socket_path: str | None = Field(
        default=None,
        description=(
            "Absolute path to the Unix socket (POSIX) or named pipe name (Windows); "
            "null when status=='not_found'."
        ),
    )
    note: str | None = Field(
        default=None,
        description="Human-readable hint about the verdict (why unreachable, what to try).",
    )
    probe_timeout_sec: float = Field(
        ...,
        description="Seconds the probe waited before giving up (diagnostic context).",
    )


class KiCadIpcStatusTool(Tool[KiCadIpcStatusInput, KiCadIpcStatusOutput]):
    """Report whether the KiCAD IPC socket is reachable, and if not, why."""

    name = "kicad_ipc_status"
    version = "0.1.0"
    description = "Return the status of the KiCAD IPC socket: reachable, unreachable, or not_found."
    input_model = KiCadIpcStatusInput
    output_model = KiCadIpcStatusOutput
    classification = ToolClass.READ
    # Deliberately empty. This tool *reports on* IpcBackend state rather than
    # *using* IPC to service a request; a dispatcher gate on `(Backend.IPC,)`
    # would raise BACKEND_UNAVAILABLE when IPC is down, which is the exact
    # case where the user wants a graceful `status="not_found"` diagnostic.
    # The backend-subject relationship is expressed by the typed setter
    # injection (`set_ipc_backend`) and the tool's own `status` field.
    preferred_backends = ()

    def __init__(self, ipc_backend: IpcBackend | None = None) -> None:
        self._ipc_backend = ipc_backend

    def set_ipc_backend(self, backend: IpcBackend) -> None:
        self._ipc_backend = backend

    async def run(self, input: KiCadIpcStatusInput) -> KiCadIpcStatusOutput:
        backend = self._ipc_backend
        if backend is None:
            # Bare entry-point load without server-side wiring — build a
            # disposable backend with default config values. Good enough
            # for `kimcp-cli tool run kicad_ipc_status` diagnostics.
            backend = IpcBackend()

        # Ensure we have a probe result. probe() is idempotent and caches.
        reachable = await backend.probe()

        socket_path = backend.socket_path
        note = backend.probe_note

        if socket_path is None:
            status: Literal["reachable", "unreachable", "not_found"] = "not_found"
        elif reachable:
            status = "reachable"
        else:
            status = "unreachable"

        return KiCadIpcStatusOutput(
            status=status,
            socket_path=socket_path,
            note=note,
            probe_timeout_sec=backend.probe_timeout,
        )


__all__ = ["KiCadIpcStatusInput", "KiCadIpcStatusOutput", "KiCadIpcStatusTool"]
