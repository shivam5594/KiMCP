"""SWIG ``pcbnew`` backend — dormant adapter slot.

**Status: dormant.** The ADR-0014 decision was IPC-first; by the time the
rest of the platform landed (M32) the IPC + CLI + SEXPR trio covered every
operation a tool needed. The SWIG ``pcbnew`` Python bindings shipped with
KiCAD proper would be a viable fallback — they exist on disk next to any
installed KiCAD — but plumbing them in would require:

1. Discovering the bundled ``pcbnew.py`` outside ``sys.path`` (it lives
   under ``/Applications/KiCad/KiCad.app/Contents/Frameworks`` on macOS
   and ``/usr/lib/kicad/...`` on Linux).
2. Subprocess isolation — the KiCAD Python is a different interpreter
   (often a different minor version), and mixing it with the kimcp
   process crashes.
3. API-shape guards — KiCAD 9.0 and 10.0 have divergent ``pcbnew``
   signatures for the same operations.

None of that is worth building while IPC covers the gap. We keep
``SwigBackend`` on disk as a **reserved backend slot** rather than
deleting it: the ``Backend.SWIG`` enum member is referenced in config
defaults (``preferred_backend_order``) and the dispatcher's selection
matrix, and deleting the class would force an ADR and a config-schema
bump for no immediate gain.

``probe()`` deliberately returns ``False``, which is the signal the
dispatcher uses to *never* route through this backend. So in practice
this is a null backend — present, validly enumerated, always unavailable.

If a future milestone needs a SWIG fallback (e.g. for a pre-9.0 KiCAD
install where IPC isn't in the build), resurrect this file by
implementing the three items above and flipping ``probe()`` to do the
real version check.
"""

from __future__ import annotations

import logging

from kimcp._types import Backend

log = logging.getLogger(__name__)


class SwigBackend:
    """Dormant adapter for KiCAD's SWIG ``pcbnew`` bindings.

    ``probe()`` is hardcoded to ``False`` — this backend never services
    a tool call. See module docstring for the rationale (and the
    conditions under which a future milestone might revive it).
    """

    kind = Backend.SWIG

    async def probe(self) -> bool:
        # Intentional: this backend is dormant. Returning False keeps
        # the dispatcher from ever selecting SWIG even if a tool's
        # ``preferred_backends`` list includes it. If you're here
        # planning to wire real SWIG support, see the module docstring
        # for the three discovery + isolation problems that need
        # solving first.
        return False


__all__ = ["SwigBackend"]
