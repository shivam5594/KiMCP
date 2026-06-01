"""Unit tests for `kicad-cli` path discovery.

Heavily monkeypatched — the point is that discovery order is correct, not
that we actually have kicad installed.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from kimcp.cli.paths import resolve_cli_path


def _make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\necho stub\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# -- explicit configured path ----------------------------------------------


def test_explicit_path_honored_when_executable(tmp_path: Path) -> None:
    stub = _make_executable(tmp_path / "my-kicad-cli")
    resolved = resolve_cli_path(str(stub))
    assert resolved == stub.resolve()


def test_explicit_path_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Set $HOME so `~` expands inside the test sandbox.
    monkeypatch.setenv("HOME", str(tmp_path))
    stub = _make_executable(tmp_path / "kicad-cli")
    resolved = resolve_cli_path("~/kicad-cli")
    assert resolved == stub.resolve()


def test_explicit_path_returns_none_when_missing(tmp_path: Path) -> None:
    # Non-auto explicit values must not silently fall back.
    assert resolve_cli_path(str(tmp_path / "not-there")) is None


def test_explicit_path_returns_none_when_not_executable(tmp_path: Path) -> None:
    p = tmp_path / "not-exec"
    p.write_text("not executable")
    # No +x bit.
    assert resolve_cli_path(str(p)) is None


# -- auto path: PATH lookup -------------------------------------------------


def test_auto_prefers_path_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _make_executable(tmp_path / "kicad-cli")
    monkeypatch.setattr("kimcp.cli.paths.shutil.which", lambda name: str(stub))

    resolved = resolve_cli_path("auto")
    assert resolved == stub.resolve()


def test_auto_empty_configured_also_triggers_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty string is treated like "auto" (current behavior).
    stub = _make_executable(tmp_path / "kicad-cli")
    monkeypatch.setattr("kimcp.cli.paths.shutil.which", lambda name: str(stub))

    resolved = resolve_cli_path("")
    assert resolved == stub.resolve()


# -- auto path: platform defaults ------------------------------------------


def test_auto_falls_back_to_platform_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _make_executable(tmp_path / "kicad-cli")
    monkeypatch.setattr("kimcp.cli.paths.shutil.which", lambda name: None)
    monkeypatch.setattr("kimcp.cli.paths._platform_candidates", lambda: (str(stub),))

    resolved = resolve_cli_path("auto")
    assert resolved == stub.resolve()


def test_auto_returns_none_when_nothing_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kimcp.cli.paths.shutil.which", lambda name: None)
    monkeypatch.setattr("kimcp.cli.paths._platform_candidates", lambda: ())
    assert resolve_cli_path("auto") is None


def test_auto_skips_nonexistent_platform_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kimcp.cli.paths.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "kimcp.cli.paths._platform_candidates",
        lambda: ("/nowhere/1", "/nowhere/2"),
    )
    assert resolve_cli_path("auto") is None


def test_explicit_dangling_symlink_returns_none(tmp_path: Path) -> None:
    # A symlink pointing at a nonexistent target: `is_file()` returns False
    # (follows the link and finds nothing) so we correctly return None rather
    # than claim a valid binary. Protects against weird macOS KiCAD-upgrade
    # states where `/Applications/KiCad/…` is a stale symlink.
    real_target = tmp_path / "real-target-that-does-not-exist"
    link = tmp_path / "link"
    link.symlink_to(real_target)
    assert resolve_cli_path(str(link)) is None
