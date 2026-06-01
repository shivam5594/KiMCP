"""Backend dispatcher shell (per `backends.md` + ADR-0014).

Responsibilities:
  1. Cache per-session probe results for each backend.
  2. Given a tool's `preferred` list and optional `required` set, pick the
     first available backend in preference order.
  3. Emit a clear `BACKEND_UNAVAILABLE` error with context when nothing fits.

Non-responsibilities (filled in by later milestones):
  - Actually calling into backends.
  - `live_gui_visible` error shaping — done in the server layer where tool
    metadata is in scope.
  - Degrade-to-non-IPC warnings — injected by the server via the envelope's
    `meta.live_sync` + `meta.warnings`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from kimcp._types import Backend
from kimcp.errors import BACKEND_UNAVAILABLE, RpcError

log = logging.getLogger(__name__)


class BackendAvailability:
    """In-session cache of backend probe results."""

    def __init__(self) -> None:
        self._status: dict[Backend, bool] = {}

    def mark(self, backend: Backend, available: bool) -> None:
        self._status[backend] = available

    def is_available(self, backend: Backend) -> bool:
        return self._status.get(backend, False)

    def available(self) -> set[Backend]:
        return {b for b, ok in self._status.items() if ok}

    def as_dict(self) -> dict[str, bool]:
        return {b.value: ok for b, ok in self._status.items()}


class Dispatcher:
    """Selects a backend per call given a preference order and availability."""

    def __init__(self, availability: BackendAvailability | None = None) -> None:
        self.availability = availability or BackendAvailability()

    def pick(
        self,
        *,
        preferred: Iterable[Backend],
        required: Iterable[Backend] | None = None,
    ) -> Backend:
        """Return the first `preferred` backend that is available.

        Raises `RpcError(BACKEND_UNAVAILABLE)` if none qualify. If `required`
        is provided, only backends in that set are considered.
        """
        required_set: set[Backend] | None = set(required) if required is not None else None

        preferred_list = list(preferred)
        for backend in preferred_list:
            if required_set is not None and backend not in required_set:
                continue
            if self.availability.is_available(backend):
                return backend

        raise RpcError(
            code=BACKEND_UNAVAILABLE,
            message="no preferred backend is available for this operation",
            data={
                "preferred": [b.value for b in preferred_list],
                "required": [b.value for b in (required_set or ())] or None,
                "available": sorted(self.availability.as_dict().keys()),
            },
        )


__all__ = ["BackendAvailability", "Dispatcher"]
