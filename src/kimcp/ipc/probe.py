"""Async reachability probe for the KiCAD IPC socket.

This layer answers a single narrow question: **can we open a connection
to the IPC socket right now?** It does *not* perform the nng handshake,
exchange protobuf envelopes, or verify the server identity. Those concerns
arrive alongside the first real RPC call in a later milestone (see
ADR-0015 for the protobuf-over-nng transport); until then, "a process is
listening" is the signal the dispatcher needs. nng sits on AF_UNIX under
the hood so a plain ``asyncio.open_unix_connection`` is sufficient for
the liveness question — we don't have to speak nng framing to observe
that something is accepting connects.

The probe distinguishes three outcomes:

* ``True``  — we opened the connection and closed it cleanly. Something is
  listening and accepts connections.
* ``False`` — the connection was refused, the socket does not exist, or
  the peer is not speaking the expected transport (e.g. regular file).
* raise ``IpcProbeTimeoutError`` — the connect attempt exceeded the
  timeout. Usually means a wedged KiCAD; signalling with an exception
  lets the dispatcher surface "found but not responsive" distinctly from
  "not found".
"""

from __future__ import annotations

import asyncio
import logging
import sys

from kimcp.ipc.errors import IpcProbeTimeoutError

log = logging.getLogger(__name__)

DEFAULT_PROBE_TIMEOUT_SEC = 2.0


async def probe_socket(socket_path: str, *, timeout: float = DEFAULT_PROBE_TIMEOUT_SEC) -> bool:
    """Return True iff we can open — and immediately close — a connection.

    On POSIX, uses ``asyncio.open_unix_connection`` against ``socket_path``.
    On Windows, uses a pipe-open via the ProactorEventLoop primitive.
    Any connection refusal / missing path / wrong-type peer returns False.
    Timeout raises :class:`IpcProbeTimeoutError` — "wedged" is a different
    signal than "missing", and the tool surface wants to show both.
    """
    try:
        if sys.platform == "win32":
            ok = await asyncio.wait_for(_probe_windows_pipe(socket_path), timeout=timeout)
        else:
            ok = await asyncio.wait_for(_probe_unix_socket(socket_path), timeout=timeout)
    except TimeoutError as exc:
        raise IpcProbeTimeoutError(
            f"IPC probe exceeded {timeout}s for {socket_path!r}",
            socket_path=socket_path,
            timeout=timeout,
        ) from exc
    return ok


async def _probe_unix_socket(socket_path: str) -> bool:
    """Open and close a Unix socket connection. Returns False on any OSError."""
    try:
        reader, writer = await asyncio.open_unix_connection(path=socket_path)
    except (ConnectionRefusedError, FileNotFoundError, PermissionError, OSError) as exc:
        # ConnectionRefusedError: nothing listening.
        # FileNotFoundError:     socket path missing (stale config / not started).
        # PermissionError:       filesystem ACL prevents connect (e.g. foreign UID).
        # OSError (other):       platform-specific sockopt / ENOTSOCK on regular files.
        log.debug("ipc probe: unix connect failed for %s: %r", socket_path, exc)
        return False

    # Clean close — we only wanted to know someone was listening.
    writer.close()
    try:
        await writer.wait_closed()
    except OSError as exc:  # pragma: no cover — close-path races are platform-specific
        log.debug("ipc probe: wait_closed raised %r (ignored)", exc)
    # `reader` is owned by the StreamWriter transport and closed with it.
    del reader
    return True


async def _probe_windows_pipe(pipe_name: str) -> bool:  # pragma: no cover — platform-gated
    """Open and close a Windows named pipe.

    Windows' asyncio supports named-pipe clients via the Proactor loop
    (default on Python 3.8+). Failure modes mirror the Unix path:
    FileNotFoundError / PermissionError / generic OSError all collapse
    to False; timeout surfaces through the outer ``wait_for`` in
    :func:`probe_socket`.

    Implementation note: we import ``asyncio.windows_utils`` lazily so
    cross-platform mypy/ruff don't choke on the attribute when running
    on POSIX. Not covered by CI today (no Windows runner yet) — the
    cross-platform test just asserts the function exists and the
    dispatch is correct.
    """
    try:
        loop = asyncio.get_running_loop()
        # `connect_pipe` is present on Windows' ProactorEventLoop. On
        # other loops this raises AttributeError, which we treat as
        # "cannot probe on this platform".
        connect_pipe = getattr(loop, "create_pipe_connection", None)
        if connect_pipe is None:
            log.debug("ipc probe: windows pipe probe not supported on this event loop")
            return False
        # The exact API signature varies by Python version; using a dict
        # kwarg path keeps this forward-compat. If this path needs
        # hardening we'll add an integration test on a Windows runner.
        _transport, _protocol = await connect_pipe(asyncio.Protocol, pipe_name)
        _transport.close()
    except (FileNotFoundError, PermissionError, OSError) as exc:
        log.debug("ipc probe: windows pipe connect failed for %s: %r", pipe_name, exc)
        return False
    return True


__all__ = ["DEFAULT_PROBE_TIMEOUT_SEC", "probe_socket"]
