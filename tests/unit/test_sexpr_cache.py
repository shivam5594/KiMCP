"""Unit tests for the S-expression parse cache."""

from __future__ import annotations

import os
import time
from pathlib import Path

from kimcp.sexpr.cache import ParseCache


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def test_first_get_parses_and_stores(tmp_path: Path) -> None:
    p = tmp_path / "doc.kicad_sym"
    _write(p, b"(kicad_symbol_lib (version 20231120))")
    cache = ParseCache()

    doc1 = cache.get(p)
    assert doc1.top_head == "kicad_symbol_lib"
    assert len(cache) == 1

    # Second get returns the same object (cache hit).
    doc2 = cache.get(p)
    assert doc2 is doc1


def test_mtime_change_invalidates(tmp_path: Path) -> None:
    p = tmp_path / "doc.kicad_sym"
    _write(p, b"(kicad_symbol_lib (version 1))")
    cache = ParseCache()
    first = cache.get(p)

    # Sleep long enough for a detectable mtime tick on all filesystems.
    time.sleep(0.01)
    _write(p, b"(kicad_symbol_lib (version 2))")
    # Belt-and-braces: bump mtime explicitly.
    now = time.time()
    os.utime(p, (now, now + 1))

    second = cache.get(p)
    assert second is not first
    assert second.version == "2"


def test_invalidate_drops_entry(tmp_path: Path) -> None:
    p = tmp_path / "doc.kicad_sym"
    _write(p, b"(kicad_symbol_lib)")
    cache = ParseCache()
    cache.get(p)
    assert len(cache) == 1

    assert cache.invalidate(p) is True
    assert len(cache) == 0
    assert cache.invalidate(p) is False  # already gone


def test_lru_eviction_respects_cap(tmp_path: Path) -> None:
    # Each file is 30 bytes; cap of 40 guarantees only one entry fits.
    cache = ParseCache(max_bytes=40)
    for i in range(3):
        p = tmp_path / f"{i}.kicad_sym"
        _write(p, b"(kicad_symbol_lib (version %d))" % i)
        cache.get(p)

    # Only the most-recently inserted entry should remain.
    assert len(cache) == 1
    assert cache.total_bytes() <= 40


def test_oversized_entry_is_not_cached(tmp_path: Path) -> None:
    cache = ParseCache(max_bytes=4)
    p = tmp_path / "big.kicad_sym"
    _write(p, b"(kicad_symbol_lib (version 1))")
    doc = cache.get(p)
    assert doc.top_head == "kicad_symbol_lib"
    # Bigger than the cap — must not be stored.
    assert len(cache) == 0


def test_invalidate_all(tmp_path: Path) -> None:
    cache = ParseCache()
    for i in range(3):
        p = tmp_path / f"{i}.kicad_sym"
        _write(p, b"(kicad_symbol_lib)")
        cache.get(p)
    assert len(cache) == 3
    cache.invalidate_all()
    assert len(cache) == 0
    assert cache.total_bytes() == 0


def test_verify_hash_detects_tampering_with_same_mtime(tmp_path: Path) -> None:
    p = tmp_path / "doc.kicad_sym"
    _write(p, b"(kicad_symbol_lib (version 1))")
    cache = ParseCache()

    first = cache.get(p, verify_hash=True)
    assert first.version == "1"

    # Tamper with content while preserving mtime + size exactly.
    stat = p.stat()
    _write(p, b"(kicad_symbol_lib (version 9))")
    os.utime(p, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    # Without verify_hash, we'd get a stale cached parse. With verify_hash,
    # the mismatch evicts and we get the new content.
    second = cache.get(p, verify_hash=True)
    assert second.version == "9"
