"""Discovery chain for the KiCAD IPC socket.

Resolution order (first hit wins):

1. Explicit path from config (``kicad.ipc_socket``) — if it's a concrete
   socket path (or Windows named pipe), use it directly. Non-``"auto"``
   values that don't resolve return ``None`` rather than silently
   falling back; this surfaces user misconfiguration instead of hiding it.
2. ``KICAD_API_SOCKET`` environment variable — the documented upstream
   override (see ADR-0015; matches ``kicad-python``'s
   ``_default_socket_path`` and KiCAD's own contract).
3. Platform-specific well-known locations — where KiCAD 9+ places the
   socket by default on each supported platform.

The returned string is the path to pass to ``asyncio.open_unix_connection``
(POSIX) or whatever pipe-probe routine the caller chooses (Windows). We do
not attempt to open the socket here — that's the probe layer's job. This
module is pure path resolution so it stays synchronous and trivially
testable.

Scheme handling: KiCAD / ``kicad-python`` use the nng URI form
``ipc://<path>`` (e.g. ``ipc:///tmp/kicad/api.sock``) in the env var and
in ``pynng.Req0(dial=...)``. We deal in filesystem paths internally so
:func:`_strip_ipc_scheme` normalizes off the leading ``ipc://`` before
any existence check.

Windows note: KiCAD 9+ on Windows places the socket under
``%TEMP%\\kicad\\api.sock`` and pynng transparently maps that to AF_UNIX
(on 10+) or a named pipe (older). We also keep the ``\\\\.\\pipe\\*``
candidates as secondary fallbacks for older install paths. The probe
layer's Windows path is not yet verified against a live KiCAD 9+ install
— M5 targets POSIX first, Windows fit-and-finish lands later.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Platform defaults — kept module-level so tests can monkeypatch without
# reaching inside a function. Order matters: first hit wins.
#
# macOS: KiCAD 9+ and kicad-python default to `/tmp/kicad/api.sock`. We
# keep the Application Support path as a secondary fallback for
# non-upstream installs that may have diverged.
_MACOS_CANDIDATES = (
    # /tmp path is a well-known KiCAD default, not our choice. S108 ("probable
    # insecure /tmp usage") doesn't apply — we're matching the upstream
    # convention, not creating a tempfile.
    "/tmp/kicad/api.sock",
    "~/Library/Application Support/kicad/api.sock",
)

# Linux-candidate names used when probing XDG-relative and /tmp-relative roots
# as secondary fallbacks. The primary is the upstream-canonical
# `/tmp/kicad/api.sock` plus the Flatpak path; both are added first by
# `_linux_candidates` before the XDG fallbacks below.
_LINUX_CANDIDATE_NAMES = ("kicad/api.sock", "kicad-api.sock")

# Windows named-pipe fallbacks. The primary candidate (built dynamically from
# `tempfile.gettempdir()`) is prepended by `_platform_candidates` to match
# kicad-python's default.
_WINDOWS_PIPE_FALLBACKS = (
    r"\\.\pipe\kicad",
    r"\\.\pipe\kicad-api",
)

# Env override — matches the upstream `kicad-python` / KiCAD contract
# exactly. Prior versions of this file mistakenly used `KICAD_API_SOCK`
# (see ADR-0015).
_ENV_VAR = "KICAD_API_SOCKET"

# nng URI scheme prefix — stripped before any filesystem check.
_IPC_SCHEME = "ipc://"


def resolve_ipc_socket(configured: str) -> str | None:
    """Return an absolute socket path / pipe name, or ``None`` if not found.

    ``configured`` is whatever ``config.kicad.ipc_socket`` holds — the
    sentinel ``"auto"`` (and empty string, for symmetry with ``cli_exe``)
    enables the discovery chain; any other value is honored verbatim and
    must resolve, else we return ``None``.
    """
    # 1. Explicit configured value.
    if configured and configured != "auto":
        return _check_candidate(configured)

    # 2. Environment variable.
    env_value = os.environ.get(_ENV_VAR)
    if env_value:
        resolved = _check_candidate(env_value)
        if resolved is not None:
            return resolved
        # If the user explicitly set the env var, a missing path is a
        # configuration error — surface it (return None) rather than silently
        # masking it with a platform-default fallback. Symmetric with the
        # explicit-config branch above.
        return None

    # 3. Platform defaults.
    for cand in _platform_candidates():
        resolved = _check_candidate(cand)
        if resolved is not None:
            return resolved

    return None


def _strip_ipc_scheme(raw: str) -> str:
    """Return ``raw`` with any leading ``ipc://`` stripped.

    ``kicad-python``'s ``_default_socket_path`` emits URIs in the nng form
    (``ipc:///tmp/kicad/api.sock``) and the ``KICAD_API_SOCKET`` env var
    is documented in the same shape. Internally we operate on filesystem
    paths so the existence check in :func:`_check_candidate` works —
    normalizing the scheme here keeps every downstream layer scheme-free.
    """
    if raw.startswith(_IPC_SCHEME):
        return raw[len(_IPC_SCHEME) :]
    return raw


def _check_candidate(raw: str) -> str | None:
    """Validate a candidate path/pipe and return the absolute form or None.

    Existence checks differ by platform: on POSIX we want a socket file
    (or symlink to one); on Windows we accept anything in the named-pipe
    namespace since pipes don't show up in the filesystem.
    """
    raw = _strip_ipc_scheme(raw)

    if sys.platform == "win32" and raw.startswith(r"\\.\pipe"):
        # Pipe name; defer reachability to the probe layer.
        return raw

    expanded = Path(raw).expanduser()
    # `is_socket()` only works when the socket actually exists *and* is a
    # socket. We use `exists()` as a looser permit — symlinks, character
    # devices that a user has wired up, etc. — and leave the real "can we
    # talk to it?" decision to the probe.
    try:
        if expanded.exists():
            return str(expanded.resolve())
    except OSError:
        # Permission denied on a parent directory, or similar. Treat as
        # "not discoverable" rather than crash path resolution.
        return None
    return None


def _platform_candidates() -> tuple[str, ...]:
    # Bind to a str-typed local so mypy doesn't narrow `sys.platform` to the
    # host's literal and flag the other branches as unreachable. All three
    # branches are real across the support matrix. (Same trick as in
    # `kimcp.cli.paths`.)
    platform: str = sys.platform
    if platform == "darwin":
        return _MACOS_CANDIDATES
    if platform == "win32":
        # Primary: match kicad-python's default (`{gettempdir()}\kicad\api.sock`).
        # Fallbacks: legacy named-pipe paths, in case a user's install uses them.
        primary = str(Path(tempfile.gettempdir()) / "kicad" / "api.sock")
        return (primary, *_WINDOWS_PIPE_FALLBACKS)
    # Linux / BSD / other Unix-ish.
    return _linux_candidates()


def _linux_candidates() -> tuple[str, ...]:
    """Build the Linux candidate list.

    Order:

    1. Flatpak socket if the per-user Flathub path exists — KiCAD via Flatpak
       exposes its API socket under ``~/.var/app/org.kicad.KiCad/cache/tmp``
       (matches ``kicad-python``'s explicit check).
    2. Upstream canonical path ``/tmp/kicad/api.sock``.
    3. XDG-relative and ``/tmp/user-<uid>`` candidates as secondary fallbacks
       for non-upstream installs that may have diverged.
    """
    out: list[str] = []

    # 1. Flatpak: only add if the path actually exists — kipy does the same
    # (the Flatpak sandbox is user-specific; if it's not installed, we should
    # not consume the candidate slot with a path that will never exist).
    home = os.environ.get("HOME")
    if home:
        flatpak = f"{home}/.var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock"
        if os.path.exists(flatpak):
            out.append(flatpak)

    # 2. Upstream canonical /tmp path (always tried).
    out.append("/tmp/kicad/api.sock")

    # 3. Secondary fallbacks keyed on XDG_RUNTIME_DIR and /tmp/user-<uid>.
    # These cover source-build / legacy systemd-user configurations that
    # predate KiCAD's /tmp-default convention.
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    roots: list[str] = []
    if xdg:
        roots.append(xdg)
    # `os.getuid` exists on POSIX only. We're already inside the non-win32
    # platform branch (see `_platform_candidates`), so it's always safe here
    # — `getattr` with a default keeps the call graph robust against future
    # platforms that claim posix-ness without a uid concept.
    get_uid = getattr(os, "getuid", None)
    if get_uid is not None:
        roots.append(f"/tmp/user-{get_uid()}")

    for root in roots:
        for name in _LINUX_CANDIDATE_NAMES:
            out.append(str(Path(root) / name))

    return tuple(out)


__all__ = ["resolve_ipc_socket"]
