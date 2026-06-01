"""Property-based tests for the dispatcher.

Per `testing.md`: "for a fixed backend-availability set, selection is
deterministic". We encode that here.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kimcp._types import Backend
from kimcp.backends.dispatcher import BackendAvailability, Dispatcher
from kimcp.errors import RpcError

pytestmark = pytest.mark.property


backend_strategy = st.sampled_from(list(Backend))


@given(
    preference=st.lists(backend_strategy, min_size=1, max_size=8, unique=False),
    availability=st.dictionaries(
        backend_strategy, st.booleans(), min_size=0, max_size=len(Backend)
    ),
)
def test_selection_is_deterministic(preference, availability) -> None:
    av1 = BackendAvailability()
    av2 = BackendAvailability()
    for b, v in availability.items():
        av1.mark(b, v)
        av2.mark(b, v)

    d1 = Dispatcher(av1)
    d2 = Dispatcher(av2)

    try:
        r1 = d1.pick(preferred=preference)
    except RpcError:
        r1 = None
    try:
        r2 = d2.pick(preferred=preference)
    except RpcError:
        r2 = None

    assert r1 == r2


@given(preference=st.lists(backend_strategy, min_size=1, max_size=8))
def test_pick_returns_first_available(preference) -> None:
    av = BackendAvailability()
    for b in Backend:
        av.mark(b, True)

    disp = Dispatcher(av)
    assert disp.pick(preferred=preference) == preference[0]
