"""Filesystem watcher that invalidates ``ParseCache`` entries on change.

Why this exists
---------------

The parse cache's primary invalidation path is conservative: every
``get()`` re-stats the file and compares ``(mtime_ns, size)``. That's
correct but leaves two gaps the watcher closes:

* **Eager eviction on delete/rename.** Without a watcher, a deleted file's
  cache entry sits in the LRU until it ages out — burning bytes we could
  have reclaimed the instant the file left disk.
* **Same-mtime edits.** A tool that writes a file with matching mtime
  (possible on filesystems with coarse mtime granularity, or when a user
  deliberately ``touch``-sets a timestamp) would otherwise be invisible
  to the cache. The watcher evicts on ``on_modified`` regardless of stat.

ADR-0012 + `performance.md` explicitly mandate ``watchdog`` as the
invalidation mechanism; this module is where that mandate lives.

Lifecycle
---------

* Construct once per ``SexprBackend`` (in practice: once per ``Server``).
* ``schedule(path)`` is idempotent — repeat calls for the same absolute
  path are a no-op.
* ``start()`` spawns the observer thread (lazy; callers that never
  schedule won't pay for a thread).
* ``stop()`` halts the thread and unblocks ``join()``. Safe to call
  multiple times, safe on a never-started instance.

Thread-safety
-------------

Watchdog fires callbacks on the observer thread. ``ParseCache.invalidate``
takes the cache's internal lock, so callbacks don't race tool code calling
``cache.get()``. We do NOT synchronise with the cache during eviction —
if a tool's ``get()`` races an invalidation, worst case the tool re-parses
(correct behaviour), never returns stale data.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

    from kimcp.sexpr.cache import ParseCache

log = logging.getLogger(__name__)

# KiCAD source-file suffixes we actually care about. The resources
# layer surfaces more (``.kicad_pro``, ``.kicad_mod``, ``.kicad_wks``,
# ``.kicad_dru``) but only these end up in the sexpr parse cache —
# other suffixes never appear as keys, so watching them is wasted work.
_TRACKED_SUFFIXES: frozenset[str] = frozenset(
    {".kicad_sch", ".kicad_pcb", ".kicad_sym", ".kicad_mod", ".kicad_wks", ".kicad_dru"}
)


class _CacheEvictHandler(FileSystemEventHandler):
    """Dispatch filesystem events to ``ParseCache.invalidate``.

    Filters to tracked KiCAD suffixes so we don't spam the cache with
    invalidation calls for irrelevant files (``.git/index``, editor
    swapfiles, etc.). Filtering cheap — ``Path.suffix`` on a str — and
    keeps debug-level logging readable.
    """

    def __init__(self, cache: ParseCache) -> None:
        super().__init__()
        self._cache = cache

    # Helper — ``event.src_path`` and ``event.dest_path`` are documented
    # as ``str | bytes`` across watchdog versions. Coerce + filter here
    # so the individual handlers stay narrow.
    def _invalidate_if_tracked(self, raw: str | bytes) -> None:
        path_str = raw.decode() if isinstance(raw, bytes) else raw
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix not in _TRACKED_SUFFIXES:
            return
        dropped = self._cache.invalidate(path)
        if dropped:
            log.debug("watcher evicted %s from parse cache", path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._invalidate_if_tracked(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        # A file appearing under a previously-cached path (rare but
        # possible after a delete+recreate race) must drop the stale
        # entry before anyone reads it.
        if event.is_directory:
            return
        self._invalidate_if_tracked(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._invalidate_if_tracked(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Atomic-save pattern: editor writes ``foo.tmp``, renames over
        # ``foo.kicad_sch``. Both the source (losing its identity) and
        # the destination (replacing previous content) need eviction.
        if event.is_directory:
            return
        self._invalidate_if_tracked(event.src_path)
        self._invalidate_if_tracked(event.dest_path)


class CacheInvalidator:
    """Watchdog observer + event handler bound to a ``ParseCache``.

    One instance per server. Scheduling is idempotent; lifecycle
    (``start``/``stop``) is safe across repeat calls and on instances
    that were never started.
    """

    def __init__(self, cache: ParseCache) -> None:
        self._cache = cache
        self._handler = _CacheEvictHandler(cache)
        self._observer: BaseObserver = Observer()
        self._watched: set[Path] = set()
        self._started = False
        self._stopped = False
        # Serialises schedule/start/stop against concurrent callers —
        # the observer thread doesn't touch this set, so only other
        # caller threads (tests, main thread) contend here.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def schedule(self, directory: Path | str, *, recursive: bool = True) -> bool:
        """Register ``directory`` for watching. Returns True if newly added.

        Idempotent: repeat calls for the same absolute path are a no-op.
        Silently returns False when the directory doesn't exist (a config
        that points at a non-existent project must not crash the server —
        tools calling into paths outside the watched area still work, they
        just don't benefit from eager eviction).
        """
        path = Path(directory).expanduser().resolve()
        with self._lock:
            if self._stopped:
                log.debug("CacheInvalidator.schedule after stop ignored: %s", path)
                return False
            if path in self._watched:
                return False
            if not path.is_dir():
                log.debug("CacheInvalidator.schedule skipped non-dir: %s", path)
                return False
            try:
                self._observer.schedule(self._handler, str(path), recursive=recursive)
            except OSError as exc:
                # e.g. fs-level limits (inotify watches exhausted on Linux).
                # Log + continue — the cache's stat-based fallback remains
                # correct, just less proactive.
                log.warning("CacheInvalidator.schedule failed for %s: %s", path, exc)
                return False
            self._watched.add(path)
            return True

    def start(self) -> None:
        """Spawn the observer thread. No-op if already started or stopped."""
        with self._lock:
            if self._started or self._stopped:
                return
            self._observer.start()
            self._started = True

    def stop(self, *, timeout: float | None = 2.0) -> None:
        """Stop the observer thread and wait up to ``timeout`` seconds to join.

        Idempotent: safe to call multiple times, safe on an instance that
        was never started. ``timeout=None`` waits forever — the default
        covers a tidy shutdown without letting a buggy handler wedge the
        server's aclose path.
        """
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if not self._started:
                return
            try:
                self._observer.stop()
                self._observer.join(timeout=timeout)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("CacheInvalidator.stop error: %s", exc)

    # ------------------------------------------------------------------
    # Introspection (tests + admin)
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._started and not self._stopped

    def watched_paths(self) -> frozenset[Path]:
        with self._lock:
            return frozenset(self._watched)


__all__ = ["CacheInvalidator"]
