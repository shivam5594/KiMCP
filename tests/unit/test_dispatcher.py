"""Unit tests for the backend dispatcher."""

from __future__ import annotations

import pytest

from kimcp._types import Backend
from kimcp.backends.dispatcher import BackendAvailability, Dispatcher
from kimcp.errors import BACKEND_UNAVAILABLE, RpcError


def test_picks_first_preferred_when_available() -> None:
    avail = BackendAvailability()
    avail.mark(Backend.IPC, True)
    avail.mark(Backend.SEXPR, True)

    disp = Dispatcher(avail)
    chosen = disp.pick(preferred=[Backend.IPC, Backend.SEXPR])
    assert chosen == Backend.IPC


def test_falls_back_to_next_when_primary_unavailable() -> None:
    avail = BackendAvailability()
    avail.mark(Backend.IPC, False)
    avail.mark(Backend.SEXPR, True)

    disp = Dispatcher(avail)
    chosen = disp.pick(preferred=[Backend.IPC, Backend.SEXPR])
    assert chosen == Backend.SEXPR


def test_raises_when_nothing_available() -> None:
    avail = BackendAvailability()
    for b in Backend:
        avail.mark(b, False)

    disp = Dispatcher(avail)
    with pytest.raises(RpcError) as exc:
        disp.pick(preferred=[Backend.IPC, Backend.SEXPR])
    assert exc.value.code == BACKEND_UNAVAILABLE
    assert "preferred" in (exc.value.data or {})


def test_respects_required_filter() -> None:
    avail = BackendAvailability()
    avail.mark(Backend.IPC, True)
    avail.mark(Backend.SEXPR, True)

    disp = Dispatcher(avail)
    # Even though IPC is first preferred, required={SEXPR} filters it out.
    chosen = disp.pick(preferred=[Backend.IPC, Backend.SEXPR], required={Backend.SEXPR})
    assert chosen == Backend.SEXPR


def test_required_set_with_no_match_raises() -> None:
    avail = BackendAvailability()
    avail.mark(Backend.IPC, True)

    disp = Dispatcher(avail)
    with pytest.raises(RpcError) as exc:
        disp.pick(preferred=[Backend.IPC], required={Backend.SEXPR})
    assert exc.value.code == BACKEND_UNAVAILABLE
