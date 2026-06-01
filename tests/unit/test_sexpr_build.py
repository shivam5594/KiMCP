"""Shared sexpr-builder helpers — format gotchas pinned once, reused by every
schematic-creation tool (M14+)."""

from __future__ import annotations

import pytest

from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin._sexpr_build import (
    DEFAULT_FONT_SIZE_MM,
    at_node,
    at_node_explicit,
    atom,
    effects_node,
    find_scalar_string,
    flag_node,
    fmt_mm,
    int_node,
    slist,
    stroke_default_node,
    uuid_node,
    yesno,
)

# -- fmt_mm: KiCAD's "integer-valued float → integer string" normalization ---


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.0, "0"),
        (1.0, "1"),
        (100.0, "100"),
        (-5.0, "-5"),
        (1.27, "1.27"),
        (2.54, "2.54"),
        (0.5, "0.5"),
        (-0.762, "-0.762"),
    ],
)
def test_fmt_mm_shapes(value: float, expected: str) -> None:
    """Integer-valued floats drop the decimal; fractionals keep plain decimal."""
    assert fmt_mm(value) == expected


# -- yesno ---------------------------------------------------------------


def test_yesno_true_is_yes() -> None:
    assert yesno(True) == "yes"


def test_yesno_false_is_no() -> None:
    assert yesno(False) == "no"


# -- atom / slist shorthands --------------------------------------------


def test_atom_unquoted_by_default() -> None:
    a = atom("hello")
    assert isinstance(a, SAtom)
    assert a.text == "hello"
    assert a.quoted is False


def test_atom_quoted_when_flag_set() -> None:
    a = atom("Device:R_Small", quoted=True)
    assert a.quoted is True
    assert a.text == "Device:R_Small"


def test_slist_wraps_items_in_order() -> None:
    lst = slist(atom("head"), atom("a"), atom("b"))
    assert isinstance(lst, SList)
    assert [i.text for i in lst.items if isinstance(i, SAtom)] == ["head", "a", "b"]


def test_slist_mixes_atoms_and_lists() -> None:
    inner = slist(atom("inner"), atom("1"))
    lst = slist(atom("outer"), inner, atom("trailing"))
    assert lst.items[0] == atom("outer")
    assert lst.items[1] is inner
    assert lst.items[2] == atom("trailing")


# -- flag_node / int_node ----------------------------------------------


def test_flag_node_emits_yes() -> None:
    node = flag_node("in_bom", True)
    assert node.head == "in_bom"
    flag = node.items[1]
    assert isinstance(flag, SAtom) and flag.text == "yes" and flag.quoted is False


def test_flag_node_emits_no() -> None:
    node = flag_node("dnp", False)
    flag = node.items[1]
    assert isinstance(flag, SAtom) and flag.text == "no"


def test_int_node_unquoted_integer() -> None:
    node = int_node("unit", 3)
    payload = node.items[1]
    assert isinstance(payload, SAtom) and payload.text == "3" and payload.quoted is False


# -- at_node: angle atom omitted when zero (load-bearing KiCAD gotcha) --


def test_at_node_omits_angle_when_zero() -> None:
    """Zero angle → only two coordinate atoms. KiCAD's canonical form."""
    node = at_node(10.0, 20.0)
    assert node.head == "at"
    # head + x + y = 3 items total
    assert len(node.items) == 3
    x_atom, y_atom = node.items[1], node.items[2]
    assert isinstance(x_atom, SAtom) and x_atom.text == "10"
    assert isinstance(y_atom, SAtom) and y_atom.text == "20"


def test_at_node_emits_angle_when_nonzero() -> None:
    node = at_node(10.0, 20.0, 90.0)
    assert len(node.items) == 4
    angle = node.items[3]
    assert isinstance(angle, SAtom) and angle.text == "90"


def test_at_node_fractional_coordinates() -> None:
    node = at_node(39.37, -2.54)
    x_atom, y_atom = node.items[1], node.items[2]
    assert isinstance(x_atom, SAtom) and x_atom.text == "39.37"
    assert isinstance(y_atom, SAtom) and y_atom.text == "-2.54"


# -- at_node_explicit: angle ALWAYS emitted, even at zero (load-bearing) --
#
# Regression guard for the "need a number for 'text angle'" KiCAD 10 load
# failure. Property nodes, symbol-instance positions, and labels all
# require the 3-atom ``(at X Y 0)`` form even when the angle is zero.
# Historically the MK-II Controller Board schematic tripped on
# ``(at 0 -3.81)`` at line 2226 — the power-symbol Reference property
# emitted via the 2-atom form.


def test_at_node_explicit_emits_three_atoms_at_zero() -> None:
    """Zero angle → still 3 coordinate atoms. The bug fix."""
    node = at_node_explicit(10.0, 20.0)
    assert node.head == "at"
    assert len(node.items) == 4
    x, y, a = node.items[1], node.items[2], node.items[3]
    assert isinstance(x, SAtom) and x.text == "10"
    assert isinstance(y, SAtom) and y.text == "20"
    assert isinstance(a, SAtom) and a.text == "0"


def test_at_node_explicit_emits_three_atoms_at_nonzero() -> None:
    node = at_node_explicit(10.0, 20.0, 90.0)
    assert len(node.items) == 4
    a = node.items[3]
    assert isinstance(a, SAtom) and a.text == "90"


def test_at_node_and_explicit_differ_only_at_zero() -> None:
    """The only divergence between the two helpers is the zero-angle case."""
    # Non-zero angles agree.
    same_90 = at_node(1.0, 2.0, 90.0)
    same_90_exp = at_node_explicit(1.0, 2.0, 90.0)
    assert len(same_90.items) == len(same_90_exp.items) == 4

    # Zero-angle diverges — this is the whole point of the split.
    zero = at_node(1.0, 2.0, 0.0)
    zero_exp = at_node_explicit(1.0, 2.0, 0.0)
    assert len(zero.items) == 3  # 2-atom form (KiCAD junction/sheet)
    assert len(zero_exp.items) == 4  # 3-atom form (KiCAD property/symbol/label)


# -- effects_node: (hide yes) child, not bare hide atom (load-bearing) --


def test_effects_node_visible_has_no_hide_child() -> None:
    node = effects_node()
    assert node.head == "effects"
    # head + font = 2 items. No hide, no justify.
    assert len(node.items) == 2
    # No child named "hide" anywhere.
    assert node.find("hide") is None


def test_effects_node_hidden_emits_hide_yes_list() -> None:
    """KiCAD writes (hide yes), not a bare 'hide' atom. Pin it."""
    node = effects_node(hidden=True)
    hide = node.find("hide")
    assert hide is not None
    # The hide child is a 2-atom list: (hide yes)
    assert len(hide.items) == 2
    flag = hide.items[1]
    assert isinstance(flag, SAtom) and flag.text == "yes" and flag.quoted is False


def test_effects_node_justify_left_bottom() -> None:
    """Labels carry a (justify ...) child; components don't. Tuple → child list."""
    node = effects_node(justify=("left", "bottom"))
    justify = node.find("justify")
    assert justify is not None
    texts = [i.text for i in justify.items if isinstance(i, SAtom)]
    assert texts == ["justify", "left", "bottom"]


def test_effects_node_font_size_default_matches_kicad() -> None:
    node = effects_node()
    font = node.find("font")
    assert font is not None
    size = font.find("size")
    assert size is not None
    w, h = size.items[1], size.items[2]
    assert isinstance(w, SAtom) and w.text == fmt_mm(DEFAULT_FONT_SIZE_MM)
    assert isinstance(h, SAtom) and h.text == fmt_mm(DEFAULT_FONT_SIZE_MM)


def test_effects_node_custom_font_size() -> None:
    node = effects_node(font_size_mm=1.0)
    font = node.find("font")
    assert font is not None
    size = font.find("size")
    assert size is not None
    w = size.items[1]
    assert isinstance(w, SAtom) and w.text == "1"


# -- uuid_node ---------------------------------------------------------


def test_uuid_node_quoted_payload() -> None:
    node = uuid_node("aaaa-bbbb-cccc")
    assert node.head == "uuid"
    payload = node.items[1]
    assert isinstance(payload, SAtom)
    assert payload.text == "aaaa-bbbb-cccc"
    assert payload.quoted is True


# -- stroke_default_node -----------------------------------------------


def test_stroke_default_node_shape() -> None:
    """Canonical wire/line stroke: (stroke (width 0) (type default))."""
    node = stroke_default_node()
    assert node.head == "stroke"
    width = node.find("width")
    assert width is not None and isinstance(width.items[1], SAtom)
    assert width.items[1].text == "0"
    type_node = node.find("type")
    assert type_node is not None and isinstance(type_node.items[1], SAtom)
    assert type_node.items[1].text == "default"


# -- find_scalar_string ------------------------------------------------


def test_find_scalar_string_returns_payload() -> None:
    parent = slist(
        atom("root"),
        slist(atom("uuid"), atom("deadbeef-dead-beef-dead-beefdeadbeef", quoted=True)),
    )
    assert find_scalar_string(parent, "uuid") == "deadbeef-dead-beef-dead-beefdeadbeef"


def test_find_scalar_string_missing_child() -> None:
    parent = slist(atom("root"))
    assert find_scalar_string(parent, "uuid") is None


def test_find_scalar_string_empty_child_list() -> None:
    parent = slist(atom("root"), slist(atom("uuid")))
    assert find_scalar_string(parent, "uuid") is None


def test_find_scalar_string_non_atom_payload() -> None:
    parent = slist(
        atom("root"),
        slist(atom("uuid"), slist(atom("nested"))),
    )
    assert find_scalar_string(parent, "uuid") is None
