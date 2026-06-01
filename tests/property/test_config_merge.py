"""Property-based tests for ``_deep_merge`` + ``load_config``.

The config loader layers three sources in order: user-global →
project-local → ``session_overrides``. Each layer is a dict merged via
``_deep_merge``. Those merges are load-bearing for every runtime knob —
if a future refactor breaks "session overrides always win" or
"right-hand value overwrites on scalar keys", a dev's IDE config would
silently invert their CLI flags.

These tests encode the merge's algebraic guarantees. They're
property-based (not fixture-based) because the merge surface is
wide: arbitrary nested dicts of arbitrary shape. Hand-picked cases
miss edge cases (mixed scalar/dict on the same key, deeply nested
leaves); hypothesis shrinks them to minimal repros.

One axis we deliberately *don't* test: full associativity across
arbitrary dict shapes. ``_deep_merge`` is not associative when a key
switches types between scalar and dict across merges (this is by
design — a scalar wholly replaces any prior dict). The associativity
property only holds when the tree shape stays consistent at each key,
which is the production invariant: TOML schemas don't flip a section
between a table and a scalar across configs.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kimcp.config import _deep_merge

pytestmark = pytest.mark.property


# Two-level "section dict" mirroring the real config schema:
# outer keys always map to inner dicts, inner keys always map to
# scalars. Mirrors ``[observability]``, ``[kicad]``, etc. — the keys
# KiMCP actually merges.
#
# Why not arbitrary nested dicts: ``_deep_merge`` treats scalar-vs-dict
# asymmetrically (a scalar wholly replaces any prior dict, and vice
# versa). Associativity holds only when a given key path has a
# consistent type across all three operands, which is the production
# invariant — TOML schemas don't let a section flip between a table
# and a scalar. Generating dicts where keys COULD have mixed types
# would turn the associativity test into a test of the asymmetric
# override semantics, which is covered by the "rightmost wins" test.
_inner_keys = st.sampled_from(["a", "b", "c", "d"])
_outer_keys = st.sampled_from(["x", "y", "z"])
_leaves = st.integers(min_value=-1000, max_value=1000)


def _inner_dict_strategy() -> st.SearchStrategy[dict[str, int]]:
    return st.dictionaries(_inner_keys, _leaves, max_size=4)


def _nested_dict_strategy() -> st.SearchStrategy[dict[str, Any]]:
    # Outer values are always inner dicts — never scalars. This is
    # exactly the config's shape.
    return st.dictionaries(_outer_keys, _inner_dict_strategy(), max_size=3)


# -- algebraic properties --------------------------------------------------


@given(a=_nested_dict_strategy())
def test_merge_with_empty_is_identity_on_right(a: dict[str, Any]) -> None:
    """``merge(a, {}) == a`` — the right operand adds nothing."""
    assert _deep_merge(a, {}) == a


@given(a=_nested_dict_strategy())
def test_merge_with_empty_is_identity_on_left(a: dict[str, Any]) -> None:
    """``merge({}, a) == a`` — the left operand contributes nothing."""
    assert _deep_merge({}, a) == a


@given(a=_nested_dict_strategy())
def test_merge_is_idempotent_with_self(a: dict[str, Any]) -> None:
    """``merge(a, a) == a`` — merging a config with itself changes nothing."""
    assert _deep_merge(a, a) == a


@given(a=_nested_dict_strategy(), b=_nested_dict_strategy(), c=_nested_dict_strategy())
def test_merge_is_associative_on_consistent_shapes(
    a: dict[str, Any], b: dict[str, Any], c: dict[str, Any]
) -> None:
    """``merge(merge(a, b), c) == merge(a, merge(b, c))`` given consistent leaf types.

    Associativity is what lets the loader build ``merged`` via three
    independent passes (user → project → session) without caring
    whether a future refactor reorders the fold. Since our strategy
    generates only int leaves + dicts, a key never flips between
    scalar and dict — the pathological case the merge deliberately
    handles asymmetrically.
    """
    left = _deep_merge(_deep_merge(a, b), c)
    right = _deep_merge(a, _deep_merge(b, c))
    assert left == right


# -- "rightmost wins" contract --------------------------------------------


@given(
    a=_inner_dict_strategy(),
    b=_inner_dict_strategy(),
)
def test_rightmost_scalar_wins_at_leaf_level(
    a: dict[str, int], b: dict[str, int]
) -> None:
    """For any leaf key in ``b``, the merged result carries ``b``'s value.

    This is the "CLI override wins over file setting" contract at its
    most fundamental. At the leaf level — where both sides are scalars
    — the right operand always wins.
    """
    merged = _deep_merge(a, b)
    for key, value in b.items():
        assert merged[key] == value, (
            f"key {key!r}: expected b's scalar {value!r} to win, got {merged[key]!r}"
        )


@given(a=_nested_dict_strategy(), b=_nested_dict_strategy())
def test_merge_preserves_all_keys(a: dict[str, Any], b: dict[str, Any]) -> None:
    """Every key present in either operand appears in the merged output.

    A merge that dropped keys would silently lose user configuration —
    the kind of bug that's only visible when a dev wonders why their
    setting "isn't taking effect".
    """
    merged = _deep_merge(a, b)
    for key in a:
        assert key in merged
    for key in b:
        assert key in merged


@given(a=_nested_dict_strategy(), b=_nested_dict_strategy())
def test_merge_does_not_mutate_inputs(a: dict[str, Any], b: dict[str, Any]) -> None:
    """``_deep_merge`` is pure — neither operand is mutated in place.

    The loader reuses the ``merged`` accumulator across passes; if the
    function mutated ``base``, the second pass would corrupt the first
    pass's output.
    """
    a_snapshot = _deep_copy(a)
    b_snapshot = _deep_copy(b)
    _deep_merge(a, b)
    assert a == a_snapshot
    assert b == b_snapshot


def _deep_copy(obj: Any) -> Any:
    """Hand-rolled copy (no ``copy`` import) to keep the test self-contained.

    The strategy only produces dict | int, so this recurses cleanly.
    """
    if isinstance(obj, dict):
        return {k: _deep_copy(v) for k, v in obj.items()}
    return obj
