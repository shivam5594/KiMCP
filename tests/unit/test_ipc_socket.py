"""Unit tests for IPC socket path discovery.

Heavily monkeypatched — the point is that resolution *order* is correct,
not that KiCAD is actually running. Real reachability is a separate
concern and lives in `test_ipc_probe.py`.

Note on paths: tests that bind a real ``AF_UNIX`` socket use the
``socket_tmp_path`` fixture (short ``/tmp/kimcp-XXXXXX`` path) instead of
pytest's ``tmp_path`` — the latter lives under ``/private/var/folders/…``
on macOS and overflows the ~104-char ``sun_path`` limit.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from kimcp.ipc.socket import resolve_ipc_socket


def _make_socket_file(path: Path) -> Path:
    """Create a real Unix socket file at ``path`` and return it.

    We bind a fresh ``AF_UNIX`` socket to ``path`` so ``exists()`` returns
    True. We don't listen — reachability is the probe layer's problem.
    The caller's fixture (``socket_tmp_path``) handles rm-rf teardown.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        # Close unconditionally — if bind failed, still release the fd; if it
        # succeeded, the filesystem entry remains for the resolver to find.
        sock.close()
    return path


# -- explicit configured path ----------------------------------------------


def test_explicit_path_honored_when_socket_exists(socket_tmp_path: Path) -> None:
    sock_path = _make_socket_file(socket_tmp_path / "explicit.sock")
    resolved = resolve_ipc_socket(str(sock_path))
    assert resolved == str(sock_path.resolve())


def test_explicit_path_expands_user(socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(socket_tmp_path))
    sock_path = _make_socket_file(socket_tmp_path / "kicad-api.sock")
    resolved = resolve_ipc_socket("~/kicad-api.sock")
    assert resolved == str(sock_path.resolve())


def test_explicit_path_returns_none_when_missing(tmp_path: Path) -> None:
    # Non-auto explicit values must not silently fall back to discovery.
    # No socket is bound here so regular `tmp_path` is fine.
    assert resolve_ipc_socket(str(tmp_path / "not-there.sock")) is None


def test_explicit_path_accepts_ipc_scheme(socket_tmp_path: Path) -> None:
    """Config values may carry the ``ipc://`` prefix (the nng URI form used by
    ``kicad-python`` and the ``KICAD_API_SOCKET`` env var). The resolver must
    strip the scheme before the existence check — ADR-0015.
    """
    sock_path = _make_socket_file(socket_tmp_path / "schemed.sock")
    resolved = resolve_ipc_socket(f"ipc://{sock_path}")
    assert resolved == str(sock_path.resolve())


# -- auto: env var fallback ------------------------------------------------


def test_auto_uses_env_var_when_set(socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = _make_socket_file(socket_tmp_path / "env.sock")
    monkeypatch.setenv("KICAD_API_SOCKET", str(sock_path))
    resolved = resolve_ipc_socket("auto")
    assert resolved == str(sock_path.resolve())


def test_auto_env_var_accepts_ipc_scheme(
    socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``KICAD_API_SOCKET`` carries the nng URI form (``ipc:///tmp/...``) per
    upstream. The resolver must strip the scheme, else users copying the env
    var from ``kicad-python`` docs hit a false 'not discoverable' even when
    the underlying socket is right there — ADR-0015.
    """
    sock_path = _make_socket_file(socket_tmp_path / "env-schemed.sock")
    monkeypatch.setenv("KICAD_API_SOCKET", f"ipc://{sock_path}")
    resolved = resolve_ipc_socket("auto")
    assert resolved == str(sock_path.resolve())


def test_auto_env_var_missing_path_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit env-var misconfigurations surface as None, not as a
    platform-default fallback — symmetric with the explicit-config branch."""
    monkeypatch.setenv("KICAD_API_SOCKET", str(tmp_path / "env-missing.sock"))
    # Also neuter platform defaults so we know env is the only knob tested.
    monkeypatch.setattr("kimcp.ipc.socket._platform_candidates", lambda: ())
    assert resolve_ipc_socket("auto") is None


def test_auto_empty_configured_triggers_discovery(
    socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock_path = _make_socket_file(socket_tmp_path / "env.sock")
    monkeypatch.setenv("KICAD_API_SOCKET", str(sock_path))
    resolved = resolve_ipc_socket("")
    assert resolved == str(sock_path.resolve())


# -- auto: platform defaults -----------------------------------------------


def test_auto_falls_back_to_platform_candidates(
    socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock_path = _make_socket_file(socket_tmp_path / "platform.sock")
    # No env var.
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setattr(
        "kimcp.ipc.socket._platform_candidates",
        lambda: (str(sock_path),),
    )
    assert resolve_ipc_socket("auto") == str(sock_path.resolve())


def test_auto_returns_none_when_nothing_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setattr("kimcp.ipc.socket._platform_candidates", lambda: ())
    assert resolve_ipc_socket("auto") is None


def test_auto_skips_nonexistent_platform_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setattr(
        "kimcp.ipc.socket._platform_candidates",
        lambda: ("/nowhere/1.sock", "/nowhere/2.sock"),
    )
    assert resolve_ipc_socket("auto") is None


def test_auto_picks_first_existing_candidate(
    socket_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When multiple candidates exist, the first-in-order wins. Pins the
    discovery order — a regression that reverses the tuple would flip
    XDG vs /tmp precedence on Linux."""
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    first = _make_socket_file(socket_tmp_path / "first.sock")
    second = _make_socket_file(socket_tmp_path / "second.sock")
    monkeypatch.setattr(
        "kimcp.ipc.socket._platform_candidates",
        lambda: (str(first), str(second)),
    )
    resolved = resolve_ipc_socket("auto")
    assert resolved == str(first.resolve())
