"""Property-based tests for ``_build_label_node`` across all kind/shape combos.

``sch_add_label`` dispatches on three ``LabelKind`` values and five
``LabelShape`` values — 15 combinations. Fixture-based tests cover the
headline paths (one of each kind), but 15 combos x the downstream
structural invariants (head atom, text position, Intersheetrefs
presence, fields_autoplaced flag on global only) produces a matrix
that's too tedious to hand-enumerate and too easy to get wrong.

Hypothesis walks every combo every run, so a future refactor that
breaks one cell (say: global labels lose their ``Intersheetrefs``
property) shrinks to a minimal repro instead of hiding behind a
fixture that didn't happen to exercise that arm.

Why this file, not a unit test: unit tests exercise behavior
("this exact label writes this exact bytes"); these tests exercise
invariants ("every valid kind/shape produces a structurally sound
node"). Complementary, not redundant.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_add_label import (
    _KIND_TO_HEAD,
    LabelKind,
    LabelShape,
    _build_label_node,
)

pytestmark = pytest.mark.property


_KINDS: tuple[LabelKind, ...] = ("local", "global", "hierarchical")
_SHAPES: tuple[LabelShape, ...] = (
    "input",
    "output",
    "bidirectional",
    "tri_state",
    "passive",
)

_kind_strategy = st.sampled_from(_KINDS)
_shape_strategy = st.sampled_from(_SHAPES)
# Text: keep non-empty and avoid characters that would fail Pydantic's
# stricter validation upstream; we're testing structure, not input
# sanitation.
_text_strategy = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=1,
    max_size=20,
)
_coord_strategy = st.floats(
    min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False
)
_angle_strategy = st.sampled_from([0.0, 90.0, 180.0, 270.0])


# -- head atom matches _KIND_TO_HEAD ---------------------------------------


@given(
    kind=_kind_strategy,
    shape=_shape_strategy,
    text=_text_strategy,
    at_x=_coord_strategy,
    at_y=_coord_strategy,
    angle=_angle_strategy,
)
def test_head_atom_matches_kind_mapping(
    kind: LabelKind,
    shape: LabelShape,
    text: str,
    at_x: float,
    at_y: float,
    angle: float,
) -> None:
    """The emitted list's head atom is exactly ``_KIND_TO_HEAD[kind]``.

    This is the one-bit difference eeschema uses to decide whether a
    label is sheet-local, project-global, or hierarchical-port. Any
    regression here would silently write working-looking S-expressions
    that KiCAD treats as the wrong kind of label.
    """
    node = _build_label_node(
        kind=kind,
        text=text,
        at_x=at_x,
        at_y=at_y,
        angle=angle,
        shape=shape,
        label_uuid="00000000-0000-0000-0000-000000000000",
    )
    assert isinstance(node, SList)
    assert node.head == _KIND_TO_HEAD[kind]


# -- text is always the second atom, always quoted ------------------------


@given(
    kind=_kind_strategy,
    shape=_shape_strategy,
    text=_text_strategy,
    at_x=_coord_strategy,
    at_y=_coord_strategy,
)
def test_label_text_is_quoted_second_atom(
    kind: LabelKind,
    shape: LabelShape,
    text: str,
    at_x: float,
    at_y: float,
) -> None:
    """Whatever the kind, ``items[1]`` is always the quoted label text.

    eeschema parses label positionally — head, then text, then the rest
    in any order. Moving the text would break the parse.
    """
    node = _build_label_node(
        kind=kind,
        text=text,
        at_x=at_x,
        at_y=at_y,
        angle=0.0,
        shape=shape,
        label_uuid="00000000-0000-0000-0000-000000000000",
    )
    assert len(node.items) >= 2
    text_atom = node.items[1]
    assert isinstance(text_atom, SAtom)
    assert text_atom.text == text
    assert text_atom.quoted is True


# -- global labels always carry the Intersheetrefs property ---------------


@given(
    shape=_shape_strategy,
    text=_text_strategy,
    at_x=_coord_strategy,
    at_y=_coord_strategy,
    angle=_angle_strategy,
)
def test_global_label_always_has_intersheetrefs(
    shape: LabelShape,
    text: str,
    at_x: float,
    at_y: float,
    angle: float,
) -> None:
    """A ``global_label`` without ``Intersheetrefs`` renders a blank
    cross-reference box in the plotted sheet.

    eeschema generates this property on every global label it writes;
    we match that so round-tripping through kimcp → eeschema doesn't
    silently strip it.
    """
    node = _build_label_node(
        kind="global",
        text=text,
        at_x=at_x,
        at_y=at_y,
        angle=angle,
        shape=shape,
        label_uuid="00000000-0000-0000-0000-000000000000",
    )
    # Find a child (property "Intersheetrefs" ...).
    isr = None
    for child in node.items:
        if not (isinstance(child, SList) and child.head == "property"):
            continue
        if len(child.items) < 2:
            continue
        name_atom = child.items[1]
        if isinstance(name_atom, SAtom) and name_atom.text == "Intersheetrefs":
            isr = child
            break
    assert isr is not None, "global_label missing (property Intersheetrefs ...)"


# -- hierarchical + global carry the shape sub-list; local does not -------


@given(
    kind=_kind_strategy,
    shape=_shape_strategy,
    text=_text_strategy,
    at_x=_coord_strategy,
    at_y=_coord_strategy,
)
def test_shape_subnode_present_iff_non_local(
    kind: LabelKind,
    shape: LabelShape,
    text: str,
    at_x: float,
    at_y: float,
) -> None:
    """``(shape <dir>)`` appears for global + hierarchical, never for local.

    Local labels have no direction hint — the schema would reject a
    ``(shape ...)`` child there. Global and hierarchical both require
    it. Pin both sides of the invariant so a future edit can't
    accidentally emit ``(shape ...)`` on a local label or drop it on
    the others.
    """
    node = _build_label_node(
        kind=kind,
        text=text,
        at_x=at_x,
        at_y=at_y,
        angle=0.0,
        shape=shape,
        label_uuid="00000000-0000-0000-0000-000000000000",
    )
    shape_child = node.find("shape")
    if kind == "local":
        assert shape_child is None, "local labels must not carry (shape ...)"
    else:
        assert shape_child is not None, f"{kind} label missing (shape ...)"
        # The shape value must be a bare atom carrying the requested direction.
        assert len(shape_child.items) >= 2
        value_atom = shape_child.items[1]
        assert isinstance(value_atom, SAtom)
        assert value_atom.text == shape


# -- every label carries a UUID -------------------------------------------


@given(
    kind=_kind_strategy,
    shape=_shape_strategy,
    text=_text_strategy,
)
def test_every_label_carries_uuid(
    kind: LabelKind, shape: LabelShape, text: str
) -> None:
    """Every emitted label has a ``(uuid ...)`` child — required by KiCAD
    for sheet-level identity tracking across edits."""
    label_uuid = "12345678-1234-1234-1234-123456789012"
    node = _build_label_node(
        kind=kind,
        text=text,
        at_x=0.0,
        at_y=0.0,
        angle=0.0,
        shape=shape,
        label_uuid=label_uuid,
    )
    uuid_child = node.find("uuid")
    assert uuid_child is not None
    assert len(uuid_child.items) >= 2
    uuid_atom = uuid_child.items[1]
    assert isinstance(uuid_atom, SAtom)
    assert uuid_atom.text == label_uuid
