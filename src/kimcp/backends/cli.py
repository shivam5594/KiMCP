"""`kicad-cli` backend.

Thin dispatcher-facing adapter over `kimcp.cli.*`. Responsibilities:

* Resolve the `kicad-cli` path (config → PATH → platform defaults) —
  lazily, on first probe, so `__init__` stays pure.
* Shell out to `kicad-cli version` and parse the result.
* Gate availability on `config.kicad.min_version` (ADR-0014).
* Cache the probe outcome so repeated `probe()` calls don't re-exec the
  binary. The first call does the work; subsequent calls return the
  cached flag unless `probe(refresh=True)` is passed.

The subprocess mechanics, path discovery, version parsing, and typed
errors all live in `kimcp.cli.*`; this module just composes them.

Probe-result cache semantics (intentional):
    Both success AND failure are cached on the first `probe()` call.
    The dispatcher re-probes on demand by passing `refresh=True`; it
    does not silently retry on every call. If the user installs KiCAD
    mid-session, a refresh is required to pick it up. This matches
    how the IPC/SWIG backends will behave once they land.
"""

from __future__ import annotations

import logging

from kimcp._types import Backend
from kimcp.cli.errors import CliError
from kimcp.cli.paths import resolve_cli_path
from kimcp.cli.runner import run_cli
from kimcp.cli.version import KiCadVersion, parse_cli_version

log = logging.getLogger(__name__)

# Short timeout — `kicad-cli version` should return in well under a
# second on any healthy install. Anything longer means we're fighting
# a hung child and should surface that loudly rather than block probe.
_PROBE_TIMEOUT_SEC = 10.0


class CliBackend:
    kind = Backend.CLI

    def __init__(
        self,
        *,
        configured_path: str = "auto",
        min_version: str = "9.0.0",
    ) -> None:
        self._configured_path = configured_path

        parsed_min = KiCadVersion.parse(min_version)
        if parsed_min is None:
            # Defensive: config-level validation should already have caught this
            # (see KiCadCfg.min_version validator). The backend still refuses
            # ambiguous thresholds so nothing silently coerces to 0.0.0.
            raise ValueError(f"invalid min_version {min_version!r} — expected semver-like 'N.N.N'")
        self._min_version: KiCadVersion = parsed_min

        # Probe outputs — populated lazily on first `probe()` call so that
        # constructing a backend never touches the filesystem.
        self._probed: bool = False
        self._cli_path: str | None = None  # resolved path, absolute; None => not found
        self._detected_version: KiCadVersion | None = None
        self._available: bool = False

    # -- read-only accessors for tools / diagnostics --------------------------

    @property
    def cli_path(self) -> str | None:
        return self._cli_path

    @property
    def detected_version(self) -> KiCadVersion | None:
        return self._detected_version

    @property
    def min_version(self) -> KiCadVersion:
        return self._min_version

    @property
    def probed(self) -> bool:
        return self._probed

    # -- probe ---------------------------------------------------------------

    async def probe(self, *, refresh: bool = False) -> bool:
        """Return True iff `kicad-cli` is available AND meets min_version.

        First call performs the work; subsequent calls return the cached
        flag. Pass `refresh=True` to re-check (e.g., after a fresh install
        mid-session).
        """
        if self._probed and not refresh:
            return self._available

        # Resolve (or re-resolve) the path before consulting the CLI. This is
        # the only I/O in __init__-equivalent territory, and it belongs here,
        # not in the constructor.
        resolved = resolve_cli_path(self._configured_path)
        self._cli_path = str(resolved) if resolved is not None else None

        self._probed = True
        self._available = False
        self._detected_version = None

        if resolved is None:
            log.debug(
                "cli backend: no kicad-cli binary found (configured=%s)",
                self._configured_path,
            )
            return False

        try:
            result = await run_cli(
                ("version",),
                cli_path=resolved,
                timeout=_PROBE_TIMEOUT_SEC,
                check=True,
            )
        except CliError as exc:
            log.warning("cli backend probe failed: %s", exc)
            return False

        parsed = parse_cli_version(result.stdout) or parse_cli_version(result.stderr)
        if parsed is None:
            log.warning(
                "cli backend probe: could not parse version from output: %r",
                result.stdout[:200],
            )
            return False

        self._detected_version = parsed
        if parsed < self._min_version:
            log.info(
                "cli backend: detected %s < required %s — marking unavailable",
                parsed.raw or parsed.as_tuple(),
                self._min_version.raw or self._min_version.as_tuple(),
            )
            return False

        self._available = True
        return True


__all__ = ["CliBackend"]
