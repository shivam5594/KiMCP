"""kicad_version — reports the detected `kicad-cli` version + install path.

Thin wrapper around `CliBackend`. The server injects the backend via
`set_cli_backend` post-construction so the tool reflects the live probe
result instead of re-shelling. If the backend hasn't been probed yet (or
the tool runs before the server had a chance to inject), we probe on
demand so CLI callers like `kimcp-cli tool run kicad_version` still
work.

Output distinguishes three states:

* **found + compatible** — CLI resolved, version ≥ min_version.
* **found + too_old**    — CLI resolved but below min_version.
* **not_found**          — no binary on PATH / configured location.

Each state carries enough detail for the user to act (install,
upgrade, or set `kicad.cli_exe`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kimcp._types import ToolClass
from kimcp.backends.cli import CliBackend
from kimcp.schemas.envelope import ToolOutput
from kimcp.tools.base import Tool


class KiCadVersionInput(BaseModel):
    pass


class KiCadVersionOutput(ToolOutput):
    status: Literal["found", "too_old", "not_found"]
    cli_path: str | None = Field(
        default=None,
        description="Absolute path to `kicad-cli`; null when status=='not_found'.",
    )
    detected_version: str | None = Field(
        default=None,
        description="Parsed major.minor.patch; null when we couldn't extract one.",
    )
    detected_version_raw: str | None = Field(
        default=None,
        description="Raw `Version:` string from `kicad-cli version` for diagnostics.",
    )
    min_version: str = Field(
        ...,
        description="Minimum version required by this server (config.kicad.min_version).",
    )


class KiCadVersionTool(Tool[KiCadVersionInput, KiCadVersionOutput]):
    """Report the detected KiCAD CLI version and whether it meets min_version."""

    name = "kicad_version"
    version = "0.1.0"
    description = "Return the detected kicad-cli version, install path, and compatibility status."
    input_model = KiCadVersionInput
    output_model = KiCadVersionOutput
    classification = ToolClass.READ
    # Deliberately empty. This tool *reports on* CliBackend state rather than
    # *using* CLI to service a request; a dispatcher gate on `(Backend.CLI,)`
    # would raise BACKEND_UNAVAILABLE when CLI is missing, which is the exact
    # case where a graceful `status="not_found"` envelope is most useful.
    # The backend-subject relationship is expressed by the typed setter
    # injection (`set_cli_backend`) and the tool's own `status` field.
    preferred_backends = ()

    def __init__(self, cli_backend: CliBackend | None = None) -> None:
        self._cli_backend = cli_backend

    def set_cli_backend(self, backend: CliBackend) -> None:
        self._cli_backend = backend

    async def run(self, input: KiCadVersionInput) -> KiCadVersionOutput:
        backend = self._cli_backend
        if backend is None:
            # Bare entry-point load without server-side wiring — build a
            # disposable backend with default config values. Good enough
            # for `kimcp-cli tool run kicad_version` diagnostic use.
            backend = CliBackend()

        # Ensure we have a probe result. probe() is idempotent and caches.
        available = await backend.probe()

        min_version = str(backend.min_version.raw or backend.min_version.as_tuple())
        cli_path = backend.cli_path
        detected = backend.detected_version

        if cli_path is None:
            return KiCadVersionOutput(
                status="not_found",
                cli_path=None,
                detected_version=None,
                detected_version_raw=None,
                min_version=min_version,
            )

        detected_str = (
            f"{detected.major}.{detected.minor}.{detected.patch}" if detected is not None else None
        )
        detected_raw = detected.raw if detected is not None else None

        status: Literal["found", "too_old", "not_found"] = "found" if available else "too_old"
        return KiCadVersionOutput(
            status=status,
            cli_path=cli_path,
            detected_version=detected_str,
            detected_version_raw=detected_raw,
            min_version=min_version,
        )


__all__ = ["KiCadVersionInput", "KiCadVersionOutput", "KiCadVersionTool"]
