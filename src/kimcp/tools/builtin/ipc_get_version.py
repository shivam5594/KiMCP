"""ipc_get_version — reports the version of the *running* KiCAD via IPC.

Sibling to ``kicad_version``. Where ``kicad_version`` shells out to
``kicad-cli version`` (fast, no GUI required, but tells you about the
installed binary), this tool opens a ``pynng.Req0`` connection to the
live KiCAD IPC socket and issues a ``GetVersion`` RPC. The answer is
"what is *this specific running KiCAD instance* reporting" — which can
legitimately differ from the CLI (different install paths, KiCAD-Nightly
alongside KiCAD-Stable, or the user launched a self-built binary).

Serves two purposes:

* End-to-end proof that KiMCP speaks to KiCAD 10 over the wire. This is
  the first tool that goes beyond probing — it issues a real RPC and
  surfaces the structured reply.
* A cross-check against ``kicad_version`` for users and CI: when both
  agree, the install is coherent; when they disagree, you've found a
  config drift worth surfacing.

Output distinguishes five states so the caller (operator or LLM) can
act without re-running with verbose logging:

* **ok**            — KiCAD answered; ``version`` / ``major`` / ... filled.
* **not_found**     — no IPC socket could be resolved. Is KiCAD 9+
                      running, or is ``kicad.ipc_socket`` set?
* **unreachable**   — socket resolved but the dial / send failed. KiCAD
                      isn't running, or the socket is stale from a
                      previous session.
* **call_error**    — transport was healthy but KiCAD rejected the call
                      (non-OK ``ApiStatusCode``) or the reply didn't
                      match the expected type (kicad-python version skew
                      against this KiCAD). ``note`` carries the server's
                      ``error_message``.
* **extra_missing** — KiMCP was installed without the ``[ipc]`` extra
                      (``pynng`` / ``kipy``). Install it to unlock
                      IPC-backed tools.

The dispatcher-level backend gate (``preferred_backends=(Backend.IPC,)``)
catches the common "server came up with IPC down" case before ``run``
even fires — the caller gets ``BACKEND_UNAVAILABLE`` via JSON-RPC. The
in-tool error handling here catches the narrower race where IPC was up
at probe time but fell over between the probe and the call (KiCAD
crashed, user closed it mid-session), so the ToolOutput always tells a
coherent story instead of a raw traceback.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kimcp._types import Backend, ToolClass
from kimcp.backends.ipc import IpcBackend
from kimcp.ipc.errors import (
    IpcCallError,
    IpcError,
    IpcSocketUnreachableError,
)
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool


class IpcGetVersionInput(BaseModel):
    pass


class IpcGetVersionOutput(ToolOutput):
    status: Literal["ok", "not_found", "unreachable", "call_error", "extra_missing"]
    socket_path: str | None = Field(
        default=None,
        description=(
            "Absolute path to the resolved IPC socket / pipe. Null when the "
            "socket couldn't be discovered or the [ipc] extra is missing."
        ),
    )
    version: str | None = Field(
        default=None,
        description="Parsed `major.minor.patch` from the KiCAD IPC reply; null outside status='ok'.",
    )
    version_raw: str | None = Field(
        default=None,
        description=(
            "The `full_version` string KiCAD returned (may include git hash / "
            "build flavor); null when unavailable."
        ),
    )
    major: int | None = Field(default=None, description="Major version, null outside 'ok'.")
    minor: int | None = Field(default=None, description="Minor version, null outside 'ok'.")
    patch: int | None = Field(default=None, description="Patch version, null outside 'ok'.")
    note: str | None = Field(
        default=None,
        description="Human-readable diagnostic — populated for every non-ok status.",
    )


class IpcGetVersionTool(Tool[IpcGetVersionInput, IpcGetVersionOutput]):
    """Fetch the live KiCAD version over IPC and report it, or why we couldn't."""

    name = "ipc_get_version"
    version = "0.1.0"
    description = (
        "Ask the running KiCAD over IPC for its version. Complements "
        "`kicad_version` (which reports the installed kicad-cli)."
    )
    input_model = IpcGetVersionInput
    output_model = IpcGetVersionOutput
    classification = ToolClass.READ
    # IPC-backed tool — dispatcher gates on availability. When IPC is down at
    # server-probe time, callers get BACKEND_UNAVAILABLE up front (fail fast,
    # run `kicad_ipc_status` for the diagnostic). When IPC was up at probe
    # time but fell over mid-session, the tool's own try/except surfaces the
    # graceful `status=*` envelope instead of a raw traceback.
    preferred_backends = (Backend.IPC,)

    def __init__(self, ipc_backend: IpcBackend | None = None) -> None:
        self._ipc_backend = ipc_backend

    def set_ipc_backend(self, backend: IpcBackend) -> None:
        self._ipc_backend = backend

    async def run(self, input: IpcGetVersionInput) -> IpcGetVersionOutput:
        backend = self._ipc_backend
        if backend is None:
            # Bare entry-point load without server-side wiring — build a
            # disposable backend with default config. Good enough for
            # `kimcp-cli tool run ipc_get_version` smoke tests.
            backend = IpcBackend()

        # Lazy import of the kipy proto command pair. The ``[ipc]`` extra
        # ships kipy — but so that this tool module stays importable (and
        # entry-point-registerable) on a minimal install, the import lives
        # inside `run`. A missing ``kipy`` collapses to the same graceful
        # `extra_missing` envelope as a missing ``pynng``.
        try:
            from kipy.proto.common.commands.base_commands_pb2 import (
                GetVersion,
                GetVersionResponse,
            )
        except ImportError as exc:
            return IpcGetVersionOutput(
                status="extra_missing",
                socket_path=None,
                version=None,
                version_raw=None,
                major=None,
                minor=None,
                patch=None,
                note=(
                    "KiMCP was installed without the `[ipc]` extra — run "
                    "`pip install 'kimcp[ipc]'` (or equivalent) to enable "
                    f"IPC-backed tools. Import error: {exc}"
                ),
            )

        try:
            resp = await backend.call(GetVersion(), GetVersionResponse)
        except IpcSocketUnreachableError as exc:
            # Split by whether resolution succeeded at all — matches the
            # `not_found` / `unreachable` split in `kicad_ipc_status` so
            # sibling diagnostics are consistent.
            status: Literal[
                "ok", "not_found", "unreachable", "call_error", "extra_missing"
            ] = "not_found" if backend.socket_path is None else "unreachable"
            return IpcGetVersionOutput(
                status=status,
                socket_path=backend.socket_path,
                version=None,
                version_raw=None,
                major=None,
                minor=None,
                patch=None,
                note=str(exc),
            )
        except IpcCallError as exc:
            return IpcGetVersionOutput(
                status="call_error",
                socket_path=backend.socket_path,
                version=None,
                version_raw=None,
                major=None,
                minor=None,
                patch=None,
                note=str(exc),
            )
        except IpcError as exc:
            # Remaining IpcError subclasses collapse here — today that's
            # only the "pynng missing" case raised from IpcBackend.call(),
            # but ordering matters: the two subclasses above have already
            # been caught.
            return IpcGetVersionOutput(
                status="extra_missing",
                socket_path=backend.socket_path,
                version=None,
                version_raw=None,
                major=None,
                minor=None,
                patch=None,
                note=str(exc),
            )

        # Defensive extraction. kicad-python 0.6 (KiCAD 9.0.7 protos) has
        # `.version.major/minor/patch/full_version`. If a future KiCAD
        # bumps the envelope shape — the `KiCadVersion` message gets
        # renamed or the fields move — we fold "reply parsed by protobuf
        # but shape we didn't expect" into the existing `call_error`
        # status rather than crash. The status docstring already covers
        # this: "the reply didn't match the expected type (kicad-python
        # version skew against this KiCAD)".
        try:
            # Direct attribute access: generated protobuf messages raise
            # AttributeError when a field isn't in the compiled schema, so
            # `v.major` is how we detect "upstream renamed it". Only
            # `full_version` uses the default-form getattr — it's the one
            # optional field we want to tolerate as empty rather than miss.
            v = resp.version
            major = int(v.major)
            minor = int(v.minor)
            patch = int(v.patch)
            full_version = str(getattr(v, "full_version", ""))
        except (AttributeError, TypeError, ValueError) as exc:
            return IpcGetVersionOutput(
                status="call_error",
                socket_path=backend.socket_path,
                version=None,
                version_raw=None,
                major=None,
                minor=None,
                patch=None,
                note=(
                    "KiCAD returned a GetVersionResponse with an unexpected "
                    "shape — possible protobuf schema drift between our "
                    "kicad-python pin and the running KiCAD. Upgrade the "
                    f"[ipc] extra when upstream ships a matching release. ({exc})"
                ),
            )

        return IpcGetVersionOutput(
            status="ok",
            socket_path=backend.socket_path,
            version=f"{major}.{minor}.{patch}",
            # `full_version` is a proto string; the unset default is "".
            # Collapsing "" to None so the envelope uses null for "unknown"
            # rather than two different sentinels.
            version_raw=full_version or None,
            major=major,
            minor=minor,
            patch=patch,
            note=None,
        )


__all__ = ["IpcGetVersionInput", "IpcGetVersionOutput", "IpcGetVersionTool"]
