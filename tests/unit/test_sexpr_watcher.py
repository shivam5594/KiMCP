"""Unit tests for ``kimcp.sexpr.watcher.CacheInvalidator``.

These tests are **timing-sensitive** — watchdog dispatches events on a
background thread, and different filesystems debounce at different speeds
(inotify ~ms, FSEvents ~100ms, polling fallback ~1s). Each test that
modifies the filesystem uses ``_wait_for`` with a generous timeout rather
than a fixed ``sleep``, so slow CI runners don't flake and fast dev
machines don't idle.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from kimcp.sexpr.cache import ParseCache
from kimcp.sexpr.watcher import CacheInvalidator

# A minimal valid-ish KiCAD schematic — just enough for SexprDocument to
# parse without raising. Real files have much more, but the cache only
# cares about "it parsed" / "it's cached", not the tree shape.
_MINIMAL_SCH = b"(kicad_sch (version 20240101) (generator test))"


def _wait_for(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.02
) -> bool:
    """Poll ``predicate`` until truthy or ``timeout`` elapses.

    Watchdog events arrive asynchronously; polling + backoff is more
    robust than a fixed ``time.sleep`` across the filesystems we care
    about (inotify / FSEvents / polling). Returns whether the predicate
    ever went truthy — callers assert on that.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def cache() -> ParseCache:
    # Generous cap so entries don't get evicted for size-pressure reasons
    # during the test and confuse the "watcher did / didn't evict" assertion.
    return ParseCache(max_bytes=16 * 1024 * 1024)


@pytest.fixture
def invalidator(cache: ParseCache) -> CacheInvalidator:
    inv = CacheInvalidator(cache)
    yield inv
    # Tear down the observer thread even if the test forgot / errored.
    inv.stop()


def _prime_after_start(cache: ParseCache, path: Path) -> None:
    """Create the file, let watchdog settle, then populate the cache.

    On macOS FSEvents replays historical events for a few hundred ms after
    ``Observer.start()``. If we prime the cache too early, a replayed
    ``modified``/``created`` event evicts our fresh entry before the real
    test action fires. Creating the file AFTER start + sleeping briefly
    drains that replay window so test assertions pin the ACTUAL watch
    behaviour, not FSEvents bootstrap noise.
    """
    path.write_bytes(_MINIMAL_SCH)
    # 300 ms is empirically enough for FSEvents to drain on macOS; inotify
    # on Linux settles within tens of ms so this is "wasted" but still fast.
    time.sleep(0.3)
    cache.get(path)


# -- construction + scheduling --------------------------------------------


def test_schedule_on_existing_dir_returns_true(
    invalidator: CacheInvalidator, tmp_path: Path
) -> None:
    assert invalidator.schedule(tmp_path) is True
    assert tmp_path.resolve() in invalidator.watched_paths()


def test_schedule_is_idempotent_for_same_path(
    invalidator: CacheInvalidator, tmp_path: Path
) -> None:
    """Second schedule for same dir is a no-op — no duplicate watches."""
    assert invalidator.schedule(tmp_path) is True
    assert invalidator.schedule(tmp_path) is False
    assert len(invalidator.watched_paths()) == 1


def test_schedule_missing_dir_returns_false(
    invalidator: CacheInvalidator, tmp_path: Path
) -> None:
    """Non-existent dir must not crash boot — log + skip is the contract."""
    missing = tmp_path / "nope"
    assert invalidator.schedule(missing) is False
    assert invalidator.watched_paths() == frozenset()


def test_schedule_expands_tilde(
    invalidator: CacheInvalidator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``~/project`` resolves against $HOME — matches log_path semantics."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "proj").mkdir()
    assert invalidator.schedule("~/proj") is True
    assert (tmp_path / "proj").resolve() in invalidator.watched_paths()


# -- lifecycle: start / stop ----------------------------------------------


def test_is_running_false_before_start(invalidator: CacheInvalidator) -> None:
    assert invalidator.is_running is False


def test_start_flips_is_running(
    invalidator: CacheInvalidator, tmp_path: Path
) -> None:
    invalidator.schedule(tmp_path)
    invalidator.start()
    assert invalidator.is_running is True


def test_stop_without_start_is_noop(invalidator: CacheInvalidator) -> None:
    """A server that never called start (e.g. aborted bootstrap) must stop cleanly."""
    invalidator.stop()
    assert invalidator.is_running is False


def test_stop_is_idempotent(
    invalidator: CacheInvalidator, tmp_path: Path
) -> None:
    invalidator.schedule(tmp_path)
    invalidator.start()
    invalidator.stop()
    invalidator.stop()  # must not raise
    assert invalidator.is_running is False


def test_schedule_after_stop_is_noop(
    invalidator: CacheInvalidator, tmp_path: Path
) -> None:
    """Late scheduling against a torn-down invalidator is a no-op, not a crash."""
    invalidator.start()
    invalidator.stop()
    assert invalidator.schedule(tmp_path) is False


def test_start_after_stop_is_noop(
    invalidator: CacheInvalidator, tmp_path: Path
) -> None:
    """Don't let a resurrect-after-shutdown bug silently spawn a zombie thread."""
    invalidator.schedule(tmp_path)
    invalidator.start()
    invalidator.stop()
    invalidator.start()  # must not spawn a thread
    assert invalidator.is_running is False


# -- invalidation behaviour -----------------------------------------------


def test_modifying_kicad_sch_invalidates_cache(
    invalidator: CacheInvalidator,
    cache: ParseCache,
    tmp_path: Path,
) -> None:
    """End-to-end: edit a .kicad_sch under the watched tree → cache entry vanishes."""
    invalidator.schedule(tmp_path)
    invalidator.start()

    sch = tmp_path / "sample.kicad_sch"
    _prime_after_start(cache, sch)
    assert len(cache) == 1

    # Overwrite with different content; the watcher should fire.
    sch.write_bytes(_MINIMAL_SCH + b" ; edit")

    assert _wait_for(lambda: len(cache) == 0), (
        "expected cache entry to be evicted by watcher within timeout"
    )


def test_deleting_kicad_sch_invalidates_cache(
    invalidator: CacheInvalidator,
    cache: ParseCache,
    tmp_path: Path,
) -> None:
    invalidator.schedule(tmp_path)
    invalidator.start()

    sch = tmp_path / "sample.kicad_sch"
    _prime_after_start(cache, sch)
    assert len(cache) == 1

    sch.unlink()

    assert _wait_for(lambda: len(cache) == 0), (
        "expected delete event to evict cache entry"
    )


def test_non_kicad_file_change_does_not_invalidate(
    invalidator: CacheInvalidator,
    cache: ParseCache,
    tmp_path: Path,
) -> None:
    """Event filter guards against wasted invalidation on ``.git/index`` etc.

    Prime the cache, then rapid-fire a ``.txt`` change; the cache entry
    must still be there after the watcher's had a chance to dispatch.
    """
    invalidator.schedule(tmp_path)
    invalidator.start()

    sch = tmp_path / "sample.kicad_sch"
    _prime_after_start(cache, sch)
    assert len(cache) == 1

    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("hello", encoding="utf-8")

    # Give watchdog a healthy window to dispatch (longer than the
    # positive-case poll would take to notice a real eviction).
    time.sleep(0.4)
    assert len(cache) == 1, (
        "non-KiCAD file edit must not have evicted the cached .kicad_sch"
    )


def test_atomic_save_pattern_invalidates_destination(
    invalidator: CacheInvalidator,
    cache: ParseCache,
    tmp_path: Path,
) -> None:
    """Editor writes ``foo.tmp`` then renames over ``foo.kicad_sch`` — both
    source and dest need invalidation so a subsequent ``get()`` re-parses.
    """
    invalidator.schedule(tmp_path)
    invalidator.start()

    sch = tmp_path / "sample.kicad_sch"
    _prime_after_start(cache, sch)
    assert len(cache) == 1

    tmp_file = tmp_path / "sample.new.kicad_sch"
    tmp_file.write_bytes(_MINIMAL_SCH + b" ; atomic")
    tmp_file.rename(sch)

    assert _wait_for(lambda: len(cache) == 0), (
        "rename-over-existing should have evicted the replaced cache entry"
    )


def test_nested_subdir_changes_fire_when_recursive(
    invalidator: CacheInvalidator,
    cache: ParseCache,
    tmp_path: Path,
) -> None:
    """Default ``recursive=True`` picks up changes under subdirectories."""
    sub = tmp_path / "sub"
    sub.mkdir()

    invalidator.schedule(tmp_path, recursive=True)
    invalidator.start()

    sch = sub / "nested.kicad_sch"
    _prime_after_start(cache, sch)
    assert len(cache) == 1

    sch.write_bytes(_MINIMAL_SCH + b" ; nested edit")

    assert _wait_for(lambda: len(cache) == 0), (
        "recursive watch must fire for nested .kicad_sch edits"
    )
