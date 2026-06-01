"""Shared pytest fixtures + in-memory transport for tests."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest


class MemoryTransport:
    """In-memory Transport implementation for unit/e2e tests.

    `incoming` is drained in order; each `write_message` is appended to `sent`.
    Returning None from `read_message` after `incoming` exhausts lets the
    dispatch loop exit cleanly.
    """

    def __init__(self, incoming: Iterable[dict[str, Any]]) -> None:
        self._incoming = list(incoming)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def read_message(self) -> dict[str, Any] | None:
        if not self._incoming:
            return None
        return self._incoming.pop(0)

    async def write_message(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def memory_transport_factory():
    def _factory(messages: Iterable[dict[str, Any]]) -> MemoryTransport:
        return MemoryTransport(messages)

    return _factory


@pytest.fixture
def socket_tmp_path() -> Iterator[Path]:
    """Short-path tmpdir safe for AF_UNIX socket binding.

    pytest's built-in ``tmp_path`` lives under ``/private/var/folders/...``
    on macOS, which blows past the ~104-char AF_UNIX ``sun_path`` limit as
    soon as a test prepends a filename (``bind(2)`` raises ``OSError: AF_UNIX
    path too long``). This fixture hands back a ``/tmp/kimcp-XXXXXX/``
    directory — short, still unique per test, still inside the user-private
    ``/tmp`` on modern systems — and removes it at teardown.

    Use this (and only this) in tests that call ``socket.bind`` or
    ``asyncio.start_unix_server``. Regular file-IO tests should keep using
    ``tmp_path`` for pytest's nicer per-test subdir semantics.
    """
    root = Path(tempfile.mkdtemp(prefix="kimcp-"))
    try:
        yield root
    finally:
        # rm -rf the whole dir — sockets, any fixture files, the dir itself.
        # `ignore_errors=True` keeps teardown robust against sockets that were
        # unlinked mid-test or permission oddities on CI runners.
        shutil.rmtree(root, ignore_errors=True)
