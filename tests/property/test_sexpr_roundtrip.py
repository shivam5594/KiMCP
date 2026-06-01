"""Property-based round-trip tests for the S-expression parser.

Strategy: build a random synthetic tree, serialize, parse the output,
and assert structural equality. This catches a wide class of
quoting / escaping / whitespace bugs that unit tests don't.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from kimcp.sexpr.document import _trees_structurally_equal
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.sexpr.parser import parse
from kimcp.sexpr.writer import serialize

pytestmark = pytest.mark.property


# Symbol-like atom text: always non-empty, no whitespace, no quotes, no parens.
_symbol_chars = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="_-./:+",
    ),
    min_size=1,
    max_size=10,
)

# Quoted-string text: any printable/unicode, including characters that *require*
# the quoted form — the writer handles escaping for us. hypothesis moved from
# `blacklist_categories` to `exclude_categories`; its stub insists on a literal
# tuple type but our plain ("Cs",) tuple is indistinguishable at runtime.
_quoted_text = st.text(
    alphabet=st.characters(exclude_categories=("Cs",)),  # type: ignore[arg-type]
    min_size=0,
    max_size=20,
)


def _atom_strategy() -> SearchStrategy[SAtom]:
    return st.one_of(
        _symbol_chars.map(lambda s: SAtom(text=s, quoted=False)),
        _quoted_text.map(lambda s: SAtom(text=s, quoted=True)),
    )


def _list_strategy(max_depth: int) -> SearchStrategy[SList]:
    if max_depth <= 0:
        # Leaf list: head + 0..3 atom children.
        return st.builds(
            lambda head, children: SList(items=[head, *children]),
            head=_symbol_chars.map(lambda s: SAtom(text=s)),
            children=st.lists(_atom_strategy(), max_size=3),
        )
    child_strategy = st.one_of(_atom_strategy(), _list_strategy(max_depth - 1))
    return st.builds(
        lambda head, children: SList(items=[head, *children]),
        head=_symbol_chars.map(lambda s: SAtom(text=s)),
        children=st.lists(child_strategy, max_size=4),
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(tree=_list_strategy(max_depth=3))
def test_serialize_then_parse_preserves_structure(tree: SList) -> None:
    data = serialize(tree, source=None)
    reparsed = parse(data)
    assert _trees_structurally_equal(tree, reparsed)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(tree=_list_strategy(max_depth=2))
def test_serialize_is_idempotent(tree: SList) -> None:
    first = serialize(tree, source=None)
    reparsed = parse(first)
    second = serialize(reparsed, source=first)
    # After the first canonicalization, subsequent round-trips must be stable.
    assert first == second
