"""Unit tests for ``resolve_system_symbol_lib``.

Mirrors the pattern of the existing ``resolve_cli_path`` tests: stub
out ``sys.platform`` + the module-level candidate tuples so behavior
is deterministic regardless of which OS the tests run on, then pin the
three cases that matter — found, not-found, and path-traversal-input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp.cli import paths as paths_module
from kimcp.cli.paths import resolve_system_symbol_lib


def test_returns_path_when_bundled_lib_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drop a file at a fake candidate dir, point the platform walker at
    it, confirm the resolver returns the absolute path."""
    fake_symbols_dir = tmp_path / "symbols"
    fake_symbols_dir.mkdir()
    fake_power = fake_symbols_dir / "power.kicad_sym"
    fake_power.write_text("(kicad_symbol_lib)", encoding="utf-8")

    monkeypatch.setattr(
        paths_module,
        "_platform_symbol_dirs",
        lambda: (str(fake_symbols_dir),),
    )

    result = resolve_system_symbol_lib("power")
    assert result is not None
    assert result == fake_power.resolve()


def test_returns_none_when_no_candidate_has_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty candidate dir → None (not a raise). Callers own the fallback
    decision — no exceptional control flow."""
    empty_dir = tmp_path / "empty_symbols"
    empty_dir.mkdir()
    monkeypatch.setattr(
        paths_module,
        "_platform_symbol_dirs",
        lambda: (str(empty_dir),),
    )
    assert resolve_system_symbol_lib("power") is None


def test_first_hit_wins_across_multiple_candidate_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When two candidate dirs both have ``power.kicad_sym``, the first
    one wins — matching KiCAD's own $KICAD_SYMBOL_DIR precedence."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "power.kicad_sym").write_text("(kicad_symbol_lib ; first)", encoding="utf-8")
    (second / "power.kicad_sym").write_text("(kicad_symbol_lib ; second)", encoding="utf-8")

    monkeypatch.setattr(
        paths_module,
        "_platform_symbol_dirs",
        lambda: (str(first), str(second)),
    )

    result = resolve_system_symbol_lib("power")
    assert result is not None
    assert result.parent == first.resolve()


@pytest.mark.parametrize("traversal", ["", "..", "../etc/passwd", "foo/bar", "foo\\bar"])
def test_rejects_path_separator_in_lib_name(
    traversal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lib_name`` is an unqualified stem. Anything containing a path
    separator — or the empty string — is a caller bug that must not be
    silently resolved against an unintended directory."""
    # Make the resolver succeed if it didn't reject early.
    monkeypatch.setattr(paths_module, "_platform_symbol_dirs", lambda: ("/",))
    assert resolve_system_symbol_lib(traversal) is None


def test_rejects_directory_entries_with_matching_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``power.kicad_sym`` exists but as a directory, not a file. The
    resolver's ``is_file`` check must reject it — otherwise downstream
    ``SexprDocument.from_path`` would blow up with a less-readable
    error deep in the parser."""
    weird = tmp_path / "symbols"
    weird.mkdir()
    (weird / "power.kicad_sym").mkdir()  # directory, not file
    monkeypatch.setattr(
        paths_module,
        "_platform_symbol_dirs",
        lambda: (str(weird),),
    )
    assert resolve_system_symbol_lib("power") is None
