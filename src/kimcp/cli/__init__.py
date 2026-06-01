"""`kicad-cli` infrastructure layer.

Splits cleanly from the backend adapter so the adapter focuses on
dispatcher concerns (probe caching, per-session state) while this
package owns the mechanics of finding, invoking, and interpreting
`kicad-cli` output. Future tools that need to shell out (DRC, ERC,
exports) import from `kimcp.cli.runner` directly; they do not go through
the backend adapter's internals.

Public surface:

* `resolve_cli_path` — path-discovery chain (config → PATH → platform
  defaults). Returns None when nothing resolves (not an exception —
  "cli not installed" is a probe-result bool, not a failure mode).
* `run_cli` — async subprocess runner with timeout, capture, and
  structured `CliResult`.
* `CliError`, `CliTimeoutError`, `CliNonZeroError` — typed errors for
  post-invocation failures (timeout, non-zero exit).
* `KiCadVersion`, `parse_cli_version` — version comparison used by the
  backend's probe gate (per ADR-0014's min-version contract).
"""

from __future__ import annotations

from kimcp.cli.errors import CliError, CliNonZeroError, CliTimeoutError
from kimcp.cli.paths import resolve_cli_path
from kimcp.cli.runner import CliResult, run_cli
from kimcp.cli.version import KiCadVersion, parse_cli_version

__all__ = [
    "CliError",
    "CliNonZeroError",
    "CliResult",
    "CliTimeoutError",
    "KiCadVersion",
    "parse_cli_version",
    "resolve_cli_path",
    "run_cli",
]
