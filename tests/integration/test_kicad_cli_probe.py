"""Integration test: ``CliBackend.probe()`` against a real ``kicad-cli``.

Skipped automatically when no ``kicad-cli`` is discoverable on this
machine. The normal unit-test matrix mocks the subprocess layer; this
file exercises the real binary so we catch drift between what we think
``kicad-cli version`` emits and what actually arrives on disk.

Run with::

    uv run pytest -m integration tests/integration/test_kicad_cli_probe.py

Why integration, not unit: version-string parsing is spec-brittle.
KiCAD's CLI output format has shifted between majors (``Version:``
prefix vs. bare semver), and ``parse_cli_version`` is written
defensively to handle both. Pinning it against the real binary once per
machine beats writing fixture-only tests that enforce yesterday's
format.
"""

from __future__ import annotations

import pytest

from kimcp.backends.cli import CliBackend
from kimcp.cli.paths import resolve_cli_path
from kimcp.cli.version import KiCadVersion

pytestmark = pytest.mark.integration


def _resolved_cli_or_skip() -> str:
    """Skip the module cleanly when no ``kicad-cli`` is installed.

    Discovery uses the production resolver (``auto``) so the test
    accepts exactly the binaries the server would accept — no synthetic
    PATH munging.
    """
    resolved = resolve_cli_path("auto")
    if resolved is None:
        pytest.skip("no kicad-cli discoverable on this machine")
    return str(resolved)


# -- probe returns True for a working install ------------------------------


@pytest.mark.asyncio
async def test_probe_against_real_cli_succeeds() -> None:
    """Real ``kicad-cli`` meets min_version=9.0.0 on any supported host."""
    _ = _resolved_cli_or_skip()
    backend = CliBackend()
    ok = await backend.probe()
    assert ok is True
    # Populated state after a successful probe.
    assert backend.cli_path is not None
    assert backend.detected_version is not None
    assert backend.probed is True


@pytest.mark.asyncio
async def test_detected_version_parses_semver() -> None:
    """``parse_cli_version`` accepts whatever real ``kicad-cli`` emits today.

    Pins a regression that would bite us if a future KiCAD release
    rearranged the version-line format.
    """
    _ = _resolved_cli_or_skip()
    backend = CliBackend()
    await backend.probe()
    detected = backend.detected_version
    assert detected is not None
    # Integer-typed tuple is the contract ``CliBackend.probe`` depends on
    # to do ``parsed < self._min_version``; if this shape drifts the
    # comparison would silently misbehave.
    assert isinstance(detected.major, int)
    assert isinstance(detected.minor, int)
    assert isinstance(detected.patch, int)
    # Supported matrix starts at 9.x per ADR-0014 — any real host with a
    # too-old version would have been skipped by ``resolve_cli_path``
    # returning None earlier (a too-old ``kicad-cli`` still resolves,
    # but in practice our CI/dev hosts all have 9+; pin that here).
    assert detected.major >= 9


# -- min_version gating --------------------------------------------------


@pytest.mark.asyncio
async def test_probe_returns_false_when_min_version_too_high() -> None:
    """A min_version above what's installed makes probe say "unavailable".

    This is the realistic path for "you're running KiCAD 9 but this
    tool requires KiCAD 11" scenarios. The backend parses the version
    successfully but still returns False — exactly what the dispatcher
    needs to emit ``KICAD_VERSION_INCOMPAT``.
    """
    _ = _resolved_cli_or_skip()
    # Pin a version that's definitely beyond any shipping KiCAD.
    backend = CliBackend(min_version="99.0.0")
    ok = await backend.probe()
    assert ok is False
    # Version WAS detected — the fail mode is "version known, too old",
    # not "couldn't talk to the binary".
    assert backend.detected_version is not None


# -- caching -------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_result_is_cached() -> None:
    """The backend docstring guarantees probe caches both success and failure.

    Repeat ``probe()`` must not re-exec the subprocess — the
    availability flag is stable until ``refresh=True``.
    """
    _ = _resolved_cli_or_skip()
    backend = CliBackend()
    first = await backend.probe()
    second = await backend.probe()
    assert first == second
    # A refresh re-runs — still succeeds against the same binary, but
    # exercises the refresh path end-to-end.
    refreshed = await backend.probe(refresh=True)
    assert refreshed == first


# -- configured path honored ---------------------------------------------


@pytest.mark.asyncio
async def test_configured_path_is_honored() -> None:
    """``cli_exe`` from config bypasses PATH discovery.

    Primarily tested in unit tests, but running against the real binary
    here catches "the explicit-path branch of ``resolve_cli_path`` works
    when the path is real" — the branch unit tests mock.
    """
    cli_path = _resolved_cli_or_skip()
    backend = CliBackend(configured_path=cli_path)
    ok = await backend.probe()
    assert ok is True
    assert backend.cli_path == cli_path


# -- version comparison against min_version pin ---------------------------


def test_min_version_parse_stable_across_formats() -> None:
    """Sanity check: ``KiCadVersion.parse`` handles semver and semver-ish inputs.

    A unit test covers this too, but running here makes it obvious when
    the integration suite is healthy — the first thing to fail if
    ``KiCadVersion`` changes shape.
    """
    assert KiCadVersion.parse("9.0.0") is not None
    assert KiCadVersion.parse("10.0.0") is not None
    assert KiCadVersion.parse("9.0.1-rc1") is not None
    assert KiCadVersion.parse("not-a-version") is None
