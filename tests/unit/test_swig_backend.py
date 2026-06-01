"""Dormancy contract for ``SwigBackend``.

``SwigBackend`` is a reserved backend slot per ADR-0014 (IPC-first). It
stays on disk so ``Backend.SWIG`` remains a valid enum member, but its
``probe()`` is hardcoded to ``False`` — the dispatcher must never route
through it. Pin that here so a well-meaning future edit doesn't
accidentally flip it on without doing the work described in
``swig.py``'s module docstring (out-of-process isolation, API-shape
guards for 9.0 vs 10.0, bundled-interpreter discovery).
"""

from __future__ import annotations

import pytest

from kimcp._types import Backend
from kimcp.backends.swig import SwigBackend


def test_swig_kind_matches_backend_enum() -> None:
    """The adapter's kind classifier agrees with the Backend enum member."""
    assert SwigBackend.kind is Backend.SWIG


@pytest.mark.asyncio
async def test_swig_probe_is_dormant() -> None:
    """``probe()`` is hardcoded False — dispatcher never routes here.

    If this test fails, someone started wiring SWIG for real without
    addressing the three discovery/isolation problems in the module
    docstring. Back out and read the docstring first.
    """
    assert await SwigBackend().probe() is False


def test_swig_preserved_in_backend_enum() -> None:
    """``Backend.SWIG`` remains a valid enum member.

    Config defaults (``preferred_backend_order``) reference this name;
    removing it would silently break every existing config.toml that
    lists the backend.
    """
    assert Backend.SWIG.value == "swig"
    # And string-round-trip is stable (StrEnum contract).
    assert Backend("swig") is Backend.SWIG
