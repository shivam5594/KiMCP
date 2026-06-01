"""S-expression parser backend (M1).

Pure-Python reader/writer for `.kicad_sch`, `.kicad_pcb`, `.kicad_sym`,
`.kicad_mod`, `.kicad_dru`, `.kicad_wks`. No external dependencies — this
backend is always usable once `kimcp` is installed, so `probe()` is
unconditionally True.

The actual parsing / writing / caching lives in `kimcp.sexpr.*`; this
adapter exists to give the dispatcher a uniform handle plus a shared
parse cache owned per backend instance.
"""

from __future__ import annotations

from kimcp._types import Backend
from kimcp.sexpr.cache import ParseCache
from kimcp.sexpr.watcher import CacheInvalidator


class SexprBackend:
    kind = Backend.SEXPR

    def __init__(
        self,
        *,
        cache: ParseCache | None = None,
        watcher: CacheInvalidator | None = None,
    ) -> None:
        self.cache = cache or ParseCache()
        # Optional file-watcher — owned by the backend so tear-down via
        # ``aclose`` is uniform with IPC's socket lifecycle. ``None`` when
        # the operator set ``performance.file_watch=false`` (or we're in
        # a test that doesn't want a thread spun up). Cache correctness
        # does NOT depend on the watcher — ``ParseCache.get`` re-stats on
        # every call — the watcher is the "eager eviction" layer per
        # ADR-0012.
        self.watcher = watcher

    async def probe(self) -> bool:
        # Pure-Python — no KiCAD install required.
        return True

    async def aclose(self) -> None:
        """Tear down the file watcher if one was attached."""
        if self.watcher is not None:
            # ``stop`` is sync (watchdog's Observer is thread-based);
            # wrapping it in this async method keeps the adapter shape
            # symmetric with ``IpcBackend.aclose`` so ``Server.aclose``
            # can fan out uniformly.
            self.watcher.stop()


__all__ = ["SexprBackend"]
