"""Discovery chains for KiCAD-installed resources.

Two related concerns live here:

1. **`resolve_cli_path`** — the ``kicad-cli`` binary. Used by every tool
   that shells out to KiCAD for load-validation, export, or ERC/DRC.
2. **`resolve_system_symbol_lib`** — the bundled ``.kicad_sym`` libraries
   KiCAD ships in ``SharedSupport/symbols`` (macOS) /
   ``/usr/share/kicad/symbols`` (Linux) / ``<install>\\symbols\\``
   (Windows). Used by ``sch_add_power`` to prefer the canonical
   ``power:GND``/``power:+3V3``/etc. lib_symbol over a minimal synthetic
   stand-in — canonical entries match what KiCAD's library browser
   places, eliminating the ``lib_symbol_mismatch`` ERC warning.

Both resolvers return an absolute, confirmed-existing Path, or None.

Resolution order for the CLI binary (first hit wins):

1. Explicit path from config (`kicad.cli_exe`) — if it's a concrete,
   executable file, use it directly. Non-`"auto"` values that don't
   resolve return None (we don't silently fall back — that would mask
   user misconfiguration).
2. `shutil.which("kicad-cli")` — honors $PATH. Works out-of-the-box on
   Linux distros and on macOS when the user has added the KiCAD.app
   binary to PATH.
3. Platform-specific well-known install paths — the defaults KiCAD
   ships under on macOS, common Linux package locations, and Windows
   installer defaults for KiCAD 9 / 10.

Resolution order for a system symbol library (first hit wins):

1. Platform-specific well-known ``symbols/`` directories — walked in
   the same platform order as the CLI binary. Only libraries bundled
   by the KiCAD installer itself land here; user-authored libraries
   live under the lib-table chain (not this resolver).

Returned paths are always absolute and confirmed to exist; callers can
pass them straight to ``SexprDocument.from_path``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Kept module-level so tests can monkeypatch without reaching into functions.
_MACOS_CANDIDATES = (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/Applications/KiCad/kicad-cli",
)

_LINUX_CANDIDATES = (
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/snap/bin/kicad-cli",
    "/var/lib/flatpak/exports/bin/org.kicad.KiCad.kicad-cli",
)


# Windows candidates — both `ProgramFiles` roots (x64 + legacy x86) and
# KiCAD 9 + 10 default install directories.
# Env var names are upper-case because on Windows `os.environ` is
# case-insensitive regardless (nt backend) and ruff SIM112 wants the
# canonical form.
def _windows_candidates() -> tuple[str, ...]:
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ]
    out: list[str] = []
    for root in roots:
        if not root:
            continue
        for kicad_ver in ("9.0", "10.0"):
            out.append(str(Path(root) / "KiCad" / kicad_ver / "bin" / "kicad-cli.exe"))
    return tuple(out)


def resolve_cli_path(configured: str) -> Path | None:
    """Return an absolute `Path` to `kicad-cli`, or None if not found.

    `configured` is whatever `config.kicad.cli_exe` holds — the sentinel
    `"auto"` enables the discovery chain; any other value is honored
    verbatim (and must resolve, else we return None).
    """
    if configured and configured != "auto":
        p = Path(configured).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p.resolve()
        return None

    # PATH lookup
    which = shutil.which("kicad-cli")
    if which:
        return Path(which).resolve()

    # Platform defaults
    for cand in _platform_candidates():
        p = Path(cand)
        if p.is_file() and os.access(p, os.X_OK):
            return p.resolve()

    return None


def _platform_candidates() -> tuple[str, ...]:
    # Bind to a str-typed local so mypy doesn't narrow `sys.platform` to the
    # host's literal and flag the other branches as unreachable. All three
    # branches are real across the support matrix.
    platform: str = sys.platform
    if platform == "darwin":
        return _MACOS_CANDIDATES
    if platform == "win32":
        return _windows_candidates()
    # Default to Linux/BSD locations for every other Unix-ish platform.
    return _LINUX_CANDIDATES


# ---------------------------------------------------------------------------
# System symbol library resolver
# ---------------------------------------------------------------------------
#
# These are the directories the KiCAD installer drops ``power.kicad_sym`` +
# ``Device.kicad_sym`` + the rest of the bundled libraries into. User-
# authored libraries live elsewhere (project-local + global lib-table) and
# are NOT resolved here — that's a different, richer lookup chain that
# would require walking ``sym-lib-table`` files.
_MACOS_SYMBOL_DIRS = (
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
)

_LINUX_SYMBOL_DIRS = (
    "/usr/share/kicad/symbols",
    "/usr/local/share/kicad/symbols",
    # Snap / Flatpak layouts — the bundled libraries ship alongside
    # the binary under the same sandbox root.
    "/snap/kicad/current/usr/share/kicad/symbols",
    "/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad/symbols",
)


def _windows_symbol_dirs() -> tuple[str, ...]:
    """Windows-install ``symbols/`` directory candidates.

    KiCAD's Windows installer drops libraries under
    ``<ProgramFiles>\\KiCad\\<ver>\\share\\kicad\\symbols``. We walk
    both 64-bit and 32-bit Program Files roots for KiCAD 9 + 10.
    """
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ]
    out: list[str] = []
    for root in roots:
        if not root:
            continue
        for kicad_ver in ("9.0", "10.0"):
            out.append(
                str(
                    Path(root)
                    / "KiCad"
                    / kicad_ver
                    / "share"
                    / "kicad"
                    / "symbols"
                )
            )
    return tuple(out)


def _platform_symbol_dirs() -> tuple[str, ...]:
    platform: str = sys.platform
    if platform == "darwin":
        return _MACOS_SYMBOL_DIRS
    if platform == "win32":
        return _windows_symbol_dirs()
    return _LINUX_SYMBOL_DIRS


def resolve_system_symbol_lib(lib_name: str) -> Path | None:
    """Return an absolute ``Path`` to ``<lib_name>.kicad_sym`` in the
    KiCAD-installed bundled-library set, or None if not found.

    ``lib_name`` is the *unqualified* library name: ``"power"`` resolves
    to ``power.kicad_sym`` in the installer-dropped symbols directory.
    Callers pass the stem only — never with a ``.kicad_sym`` suffix —
    so this function can be composed with ``lib_prefix`` arguments on
    downstream tools that synthesize ``<prefix>:<symbol>`` qualified
    ids.

    Returns None rather than raising. Callers decide whether a missing
    bundled library is fatal (e.g. sch_add_power falls back to synthesis)
    or a hard error. The filename check rejects the empty string and any
    stem with path separators to protect against path-traversal via
    caller-controlled ``lib_name`` values.
    """
    if not lib_name or "/" in lib_name or "\\" in lib_name:
        return None

    filename = f"{lib_name}.kicad_sym"
    for candidate_dir in _platform_symbol_dirs():
        p = Path(candidate_dir) / filename
        if p.is_file():
            return p.resolve()

    return None


__all__ = ["resolve_cli_path", "resolve_system_symbol_lib"]
