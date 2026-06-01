"""IPC backend — socket reachability probe + protobuf-over-nng RPCs (M3/M5).

Thin dispatcher-facing adapter over ``kimcp.ipc.*``. Responsibilities:

* Resolve the IPC socket path (config → env → platform defaults) —
  lazily, on first probe or call, so ``__init__`` stays pure.
* Open a throwaway connection to verify something is listening
  (``probe()``).
* Open a long-lived ``pynng.Req0`` socket on demand for real RPCs
  (``call()``), cache the ``kicad_token`` handed back on the first
  reply, and transparently reconnect once on mid-session transport
  failures.
* Shut the socket down cleanly (``aclose()``).

Per ADR-0015 the transport is **protobuf-over-nng (Req/Rep via pynng)**,
not gRPC. The pynng + ``kipy.proto.*`` imports live inside the methods
that need them so KiMCP stays installable without the optional ``[ipc]``
extra (tests pin this via
``test_ipc_package_does_not_import_optional_extras``).

Out of scope for this milestone:

* A typed RPC catalog. ``call()`` is the low-level primitive; tool-level
  wrappers (``ipc_get_version``, ``ipc_get_open_documents``, …) will arrive
  alongside specific tools and hand protobuf objects down.
* Version gating via IPC. The CLI backend still owns version detection;
  when a caller needs the KiCAD version they consult ``CliBackend`` —
  one source of truth, loose coupling. ``GetVersion`` is exercised in
  tests as a round-trip smoke check, not as the canonical version
  source.

Probe-result cache semantics (intentional, mirrors ``CliBackend``):
    Both success AND failure are cached on the first ``probe()`` call.
    The dispatcher re-probes on demand by passing ``refresh=True``; it
    does not silently retry on every call. If the user starts KiCAD
    mid-session, a refresh is required to pick it up.

Call-socket lifetime:
    ``__init__`` opens nothing. The first ``call()`` dials a
    ``pynng.Req0`` socket against the resolved path, subsequent calls
    reuse it. A transport failure mid-session closes the socket, clears
    the cached token, and reconnects **once** inside the same ``call()``
    — so a KiCAD restart is a transparent stutter, not a hard failure.
    If the reconnect also fails, ``IpcSocketUnreachableError`` surfaces.
    ``aclose()`` is the explicit teardown hook for shutdown paths.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING, TypeVar, cast

from kimcp._types import Backend
from kimcp.ipc.errors import (
    IpcCallError,
    IpcError,
    IpcSocketUnreachableError,
)
from kimcp.ipc.probe import DEFAULT_PROBE_TIMEOUT_SEC, probe_socket
from kimcp.ipc.socket import resolve_ipc_socket

if TYPE_CHECKING:
    # Typing-only imports — the actual pynng / protobuf machinery is
    # imported lazily inside the methods that touch the wire, so the
    # module stays importable without the `[ipc]` extra installed.
    from google.protobuf.message import Message

log = logging.getLogger(__name__)

# Default call timeout mirrors ``kicad-python``'s default (2000ms).
# Short enough that a wedged KiCAD surfaces quickly; callers issuing
# longer-running RPCs (e.g. DRC, 3D render) should construct the backend
# with an explicit ``call_timeout``. Separate from the probe timeout —
# probes are meant to fail fast; calls can legitimately wait on work.
DEFAULT_CALL_TIMEOUT_SEC = 2.0

# Response-type TypeVar. String bound avoids a runtime protobuf import
# at module load (the TypeVar bound is only resolved by type checkers
# / ``get_type_hints``, never at instantiation). Keeps the no-optional-
# extra-imports invariant intact.
R = TypeVar("R", bound="Message")


class IpcBackend:
    kind = Backend.IPC

    def __init__(
        self,
        *,
        configured_path: str = "auto",
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT_SEC,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SEC,
        client_name: str | None = None,
    ) -> None:
        self._configured_path = configured_path
        self._probe_timeout = probe_timeout
        self._call_timeout = call_timeout
        # Client name is stable for the backend's lifetime so KiCAD's log
        # correlates calls from the same KiMCP instance. Randomized suffix
        # so multiple KiMCP processes talking to one KiCAD stay
        # distinguishable (kicad-python uses `anonymous-<8>`; we prefer a
        # recognizable prefix for signal in the KiCAD-side logs).
        self._client_name = client_name or f"kimcp-{secrets.token_hex(4)}"

        # Probe outputs — populated lazily on first ``probe()`` call so that
        # constructing a backend never touches the filesystem or opens a
        # socket. (Same pin as CliBackend, audit-fix I3.)
        self._probed: bool = False
        self._socket_path: str | None = None
        self._available: bool = False
        self._probe_note: str | None = None

        # Call-time state — opened on first ``call()``, reused thereafter,
        # torn down on ``aclose()`` or on reconnect-once. Typed as
        # ``object | None`` (not ``pynng.Socket | None``) so we don't need
        # pynng at import time; the real type is enforced by construction.
        self._sock: object | None = None
        # Session token — empty string on first call, populated from the
        # first reply's ``ApiResponseHeader.kicad_token`` and echoed on
        # subsequent requests. Matches kicad-python's convention.
        self._kicad_token: str = ""

    # -- read-only accessors for tools / diagnostics --------------------------

    @property
    def socket_path(self) -> str | None:
        return self._socket_path

    @property
    def probed(self) -> bool:
        return self._probed

    @property
    def available(self) -> bool:
        return self._available

    @property
    def probe_note(self) -> str | None:
        """Human-readable hint about *why* the last probe reached its verdict.

        Populated on every ``probe()`` call. Examples:
            - ``None`` when available.
            - ``"no socket discovered"`` when resolve returned ``None``.
            - ``"socket refused connection — is KiCAD running?"``
            - ``"probe exceeded 2.0s — KiCAD may be wedged"``
        """
        return self._probe_note

    @property
    def probe_timeout(self) -> float:
        return self._probe_timeout

    @property
    def call_timeout(self) -> float:
        return self._call_timeout

    @property
    def client_name(self) -> str:
        return self._client_name

    @property
    def kicad_token(self) -> str:
        """Session token returned by KiCAD on the first successful call.

        Empty string before the first round-trip. Exposed read-only so the
        diagnostic tool can surface connection state (the token itself is
        not a secret — it identifies *which* KiCAD instance we're talking
        to, useful when multiple are running).
        """
        return self._kicad_token

    @property
    def connected(self) -> bool:
        """True iff a ``pynng.Req0`` socket is currently dialed.

        Mirrors kicad-python's ``KiCadClient.connected``. Does not re-check
        liveness — a socket can be dialed but wedged; use ``probe()`` for
        that signal.
        """
        return self._sock is not None

    # -- probe ---------------------------------------------------------------

    async def probe(self, *, refresh: bool = False) -> bool:
        """Return ``True`` iff the IPC socket is reachable right now.

        First call performs the work; subsequent calls return the cached
        flag. Pass ``refresh=True`` to re-check (e.g. after the user
        started KiCAD mid-session, or after a reconnect).
        """
        if self._probed and not refresh:
            return self._available

        self._probed = True
        self._available = False
        self._probe_note = None

        resolved = resolve_ipc_socket(self._configured_path)
        self._socket_path = resolved

        if resolved is None:
            self._probe_note = (
                "no IPC socket discovered — is KiCAD 9+ running, or is `kicad.ipc_socket` set?"
            )
            log.debug("ipc backend: %s (configured=%s)", self._probe_note, self._configured_path)
            return False

        try:
            reachable = await probe_socket(resolved, timeout=self._probe_timeout)
        except IpcError as exc:
            # Timeout / structured probe failure. Keep the typed error
            # message in `probe_note` for the diagnostic tool to show.
            self._probe_note = str(exc)
            log.warning("ipc backend probe raised: %s", exc)
            return False

        if not reachable:
            self._probe_note = (
                f"socket {resolved!r} did not accept a connection — "
                "KiCAD may not be running or the socket is stale"
            )
            log.info("ipc backend: %s", self._probe_note)
            return False

        self._available = True
        return True

    # -- call ----------------------------------------------------------------

    async def call(
        self,
        command: Message,
        response_type: type[R],
    ) -> R:
        """Send an IPC RPC and return the unpacked response message.

        The first call dials a long-lived ``pynng.Req0`` socket against
        the resolved path; subsequent calls reuse it. A transport failure
        mid-session tears down the socket, clears the cached token, and
        retries the RPC **once** against a fresh socket — this mirrors
        kicad-python's ``KiCadClient._connect`` reconnect contract and
        turns a KiCAD restart into a transparent stutter for the caller.

        Raises:
            IpcError: the optional ``[ipc]`` extra (``pynng`` + ``kipy``)
                is not installed. Surface as a config error.
            IpcSocketUnreachableError: the socket could not be resolved,
                the dial failed, or the send/recv failed on both the
                initial attempt *and* the reconnect. Signals "KiCAD is
                genuinely gone" rather than "restarting".
            IpcCallError: KiCAD answered with a non-OK ``ApiStatusCode``
                (bad request / busy / not ready / etc.) or the reply
                message could not be unpacked into ``response_type``.
                Distinct from transport errors — the remedy is typically
                a caller-side fix, not a reconnect.
        """
        # Lazy imports — pynng + kipy.proto.* belong to the optional
        # ``[ipc]`` extra. We guard the pynng import explicitly because
        # it's the most load-bearing dep; if it's absent, fail loudly with
        # a config-style error rather than a raw ImportError. The kipy
        # proto import below is safe once pynng is available (both come
        # in via the same extra).
        try:
            import pynng  # noqa: F401 - ensures the extra is installed
        except ImportError as exc:
            raise IpcError(
                "KiMCP installed without the `[ipc]` extra — run "
                "`pip install 'kimcp[ipc]'` (or equivalent) to enable "
                "IPC calls. See ADR-0015."
            ) from exc

        from kipy.proto.common.envelope_pb2 import (
            ApiRequest,
            ApiResponse,
            ApiStatusCode,
        )

        # Retry loop owns envelope construction so a reconnect-driven
        # retry rebuilds the envelope from scratch with the (now-cleared)
        # token. Rebuilding on retry matters: a reconnect means "new
        # session, fresh handshake", and the KiCAD server issues a new
        # token in that flow. Sending stale token bytes on the retry
        # would get AS_TOKEN_MISMATCH from a real KiCAD.
        max_attempts = 2  # initial + one reconnect
        reply_bytes: bytes | None = None
        for attempt in range(1, max_attempts + 1):
            envelope = ApiRequest()
            envelope.message.Pack(command)
            envelope.header.kicad_token = self._kicad_token
            envelope.header.client_name = self._client_name
            try:
                reply_bytes = await self._send_recv(envelope.SerializeToString())
                break
            except IpcSocketUnreachableError:
                if attempt >= max_attempts:
                    raise
                # Transport wedged — clear the token so the retry
                # handshakes as a brand-new session.
                self._kicad_token = ""
                log.info(
                    "ipc backend: transport failed — reconnecting once "
                    "(attempt %d of %d)",
                    attempt + 1,
                    max_attempts,
                )
        assert reply_bytes is not None  # loop either breaks on success or re-raises

        reply = ApiResponse()
        reply.ParseFromString(reply_bytes)

        if reply.status.status != ApiStatusCode.AS_OK:
            # Server-side rejection — keep the typed error so the diagnostic
            # layer can distinguish AS_BAD_REQUEST from AS_BUSY / AS_NOT_READY.
            raise IpcCallError(
                f"KiCAD returned non-OK ({reply.status.status}) for "
                f"{type(command).__name__}: "
                f"{reply.status.error_message or '<no error message>'}",
                status_code=reply.status.status,
                error_message=reply.status.error_message,
            )

        response = response_type()
        if not reply.message.Unpack(response):
            # AS_OK with an unpackable payload — protocol mismatch. Raising
            # IpcCallError (rather than IpcError) keeps the "call failed at
            # the RPC layer" signal unified; the type_url in the message
            # helps the user spot mismatches against their kicad-python
            # version.
            raise IpcCallError(
                f"could not unpack {response_type.__name__} from "
                f"ApiResponse.message (type_url={reply.message.type_url!r}) "
                f"— likely a kicad-python version skew against this KiCAD",
                status_code=reply.status.status,
                error_message="",
            )

        # Capture session token from the first successful reply. KiCAD
        # uses it to tie subsequent requests to the same client session;
        # an empty-string token on the first request is the documented
        # handshake.
        if not self._kicad_token and reply.header.kicad_token:
            self._kicad_token = reply.header.kicad_token

        # Cast: `response_type()` is typed `type[R]` → `R`, but
        # `google.protobuf` has `ignore_missing_imports = true` in our
        # mypy config so the whole chain degrades to `Any`. The runtime
        # type is whatever the caller asked for (Unpack above guarantees
        # the type match via protobuf `type_url`) — we just need to
        # convince the type checker. See ADR-0015 on why we don't pull
        # in `types-protobuf`.
        return cast(R, response)

    async def aclose(self) -> None:
        """Close the IPC socket cleanly and forget the session token.

        Call at shutdown, or when the caller explicitly wants to drop the
        connection (e.g. the user toggled the `[ipc]` preference off). A
        subsequent ``call()`` reconnects from scratch — new socket, fresh
        token handshake. Idempotent: safe to call multiple times and on
        backends that never opened a socket.
        """
        await self._close_sock()
        self._kicad_token = ""

    # -- internals -----------------------------------------------------------

    async def _ensure_connected(self) -> None:
        """Resolve the socket path (if needed) and dial a ``pynng.Req0``.

        No-op when already connected. Raises
        :class:`IpcSocketUnreachableError` if resolution or dial fails —
        those are the "KiCAD is not answering" cases and the caller
        should not silently retry.
        """
        if self._sock is not None:
            return

        import pynng
        from pynng.exceptions import NNGException

        if self._socket_path is None:
            resolved = resolve_ipc_socket(self._configured_path)
            if resolved is None:
                raise IpcSocketUnreachableError(
                    "no IPC socket discoverable — is KiCAD 9+ running, "
                    "or is `kicad.ipc_socket` set?",
                    socket_path="<unresolved>",
                )
            self._socket_path = resolved

        timeout_ms = int(self._call_timeout * 1000)
        sock = pynng.Req0(send_timeout=timeout_ms, recv_timeout=timeout_ms)
        # kicad-python / KiCAD expect the nng URI form; our `_socket_path`
        # is the bare filesystem path (resolver strips `ipc://`). Prepend
        # the scheme here so every pynng.dial call speaks the same shape.
        url = f"ipc://{self._socket_path}"
        try:
            # pynng.dial is synchronous — the AF_UNIX connect is typically
            # sub-millisecond but we offload to a worker thread so the
            # event loop stays pure regardless.
            await asyncio.to_thread(sock.dial, url, block=True)
        except NNGException as exc:
            sock.close()
            raise IpcSocketUnreachableError(
                f"IPC dial failed for {url!r}: {exc}",
                socket_path=self._socket_path,
            ) from exc
        self._sock = sock

    async def _send_recv(self, data: bytes) -> bytes:
        """Send a serialized envelope and return the reply bytes.

        One shot — on ``NNGException`` we drop the (now-stale) socket so
        the caller's retry loop re-dials on the next iteration, and
        surface :class:`IpcSocketUnreachableError`. The retry decision
        belongs to :meth:`call`, not here: the retry must rebuild the
        envelope so the handshake token refreshes, and only ``call()``
        has the command object needed to do that.
        """
        from pynng.exceptions import NNGException

        await self._ensure_connected()
        sock = self._sock
        assert sock is not None  # _ensure_connected either sets this or raises

        try:
            await sock.asend(data)  # type: ignore[attr-defined]
            reply_msg = await sock.arecv_msg()  # type: ignore[attr-defined]
            # `.bytes` on a pynng Msg is already a `bytes` object; wrap
            # defensively in case a future pynng version returns a view.
            return bytes(reply_msg.bytes)
        except NNGException as exc:
            await self._close_sock()
            raise IpcSocketUnreachableError(
                f"IPC send/recv failed for socket {self._socket_path!r}: {exc}",
                socket_path=self._socket_path or "<unresolved>",
            ) from exc

    async def _close_sock(self) -> None:
        """Close the cached socket if any. Idempotent, exception-swallowing.

        Teardown is best-effort: if ``close()`` raises (rare — typically
        only on a double-close race) we log and move on. The caller is
        already handling a failure path; we don't want teardown noise to
        mask the original error.
        """
        sock = self._sock
        self._sock = None
        if sock is None:
            return
        try:
            # pynng.Socket.close is synchronous and bounded — no await.
            sock.close()  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover — teardown races
            log.debug("ipc backend: close raised %r (ignored)", exc)


__all__ = ["DEFAULT_CALL_TIMEOUT_SEC", "IpcBackend"]
