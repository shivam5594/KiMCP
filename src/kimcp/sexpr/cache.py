"""Parse cache for S-expression documents.

Keyed by `(absolute_path, mtime_ns, size)` (per ADR-0010's revisit note:
mtime+size is the default fast path; opt-in sha256 is available for
paranoid consistency checks). LRU-bounded by total source byte size with
a configurable cap (default 256 MiB, per `performance.md`).

File-watching / automatic invalidation is **not** wired in M1 — that
lands alongside the resources layer (M4) via `watchdog`. Callers can use
`invalidate(path)` or `invalidate_all()` in the meantime.

Thread-safety: the cache is not safe for concurrent mutation. Higher
layers serialize writes via per-project advisory locks (`safety.md`).
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from kimcp.sexpr.document import SexprDocument

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 256 * 1024 * 1024  # 256 MiB


@dataclass
class _Entry:
    key: tuple[str, int, int]
    document: SexprDocument
    size_bytes: int
    sha256: str | None  # populated only when verify_hash=True was used


class ParseCache:
    """LRU-bounded cache from absolute path to parsed SexprDocument."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self._max_bytes = max_bytes
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, path: str | Path, *, verify_hash: bool = False) -> SexprDocument:
        """Return a parsed document for `path`, reusing the cache where valid.

        A cache hit requires matching `(mtime_ns, size)`. When
        `verify_hash=True`, the sha256 of the on-disk bytes is also
        checked against the stored hash (or computed and stored on miss
        in that mode) — use when another process might have rewritten
        the file with matching mtime/size (very rare but possible).
        """
        p = Path(path).resolve()
        key_path = str(p)
        stat = p.stat()
        current_key = (key_path, stat.st_mtime_ns, stat.st_size)

        with self._lock:
            entry = self._entries.get(key_path)
            if entry is not None and entry.key == current_key:
                if verify_hash:
                    data_on_hit = p.read_bytes()
                    hit_digest = hashlib.sha256(data_on_hit).hexdigest()
                    if entry.sha256 != hit_digest:
                        log.debug("cache hit with hash mismatch; reparsing %s", p)
                        self._evict_locked(key_path)
                        entry = None
                    else:
                        self._entries.move_to_end(key_path)
                        return entry.document
                else:
                    self._entries.move_to_end(key_path)
                    return entry.document

        # Miss (or dropped above) — parse fresh outside the lock.
        data = p.read_bytes()
        digest: str | None = hashlib.sha256(data).hexdigest() if verify_hash else None
        doc = SexprDocument.from_bytes(p, data)
        new_entry = _Entry(
            key=current_key,
            document=doc,
            size_bytes=len(data),
            sha256=digest,
        )

        with self._lock:
            self._insert_locked(key_path, new_entry)
            return doc

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def invalidate(self, path: str | Path) -> bool:
        """Drop the entry for `path` if present. Returns True if dropped."""
        key_path = str(Path(path).resolve())
        with self._lock:
            return self._evict_locked(key_path)

    def invalidate_all(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0

    def __len__(self) -> int:
        return len(self._entries)

    def total_bytes(self) -> int:
        return self._total_bytes

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _insert_locked(self, key_path: str, entry: _Entry) -> None:
        if key_path in self._entries:
            self._evict_locked(key_path)

        # If a single entry exceeds the cap, skip caching rather than thrash.
        if self._max_bytes == 0 or entry.size_bytes > self._max_bytes:
            log.debug(
                "skipping cache insert: entry %d bytes vs cap %d",
                entry.size_bytes,
                self._max_bytes,
            )
            return

        self._entries[key_path] = entry
        self._total_bytes += entry.size_bytes

        while self._total_bytes > self._max_bytes and self._entries:
            oldest_key, oldest = self._entries.popitem(last=False)
            self._total_bytes -= oldest.size_bytes
            log.debug("evicted %s (%d bytes) from parse cache", oldest_key, oldest.size_bytes)

    def _evict_locked(self, key_path: str) -> bool:
        entry = self._entries.pop(key_path, None)
        if entry is None:
            return False
        self._total_bytes -= entry.size_bytes
        return True


__all__ = ["DEFAULT_MAX_BYTES", "ParseCache"]
