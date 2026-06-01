"""Unit tests for SAtom / SList behavior — dirty tracking, walking, search."""

from __future__ import annotations

from kimcp.sexpr.nodes import SAtom, SList
from kimcp.sexpr.parser import parse


def test_freshly_parsed_tree_is_clean() -> None:
    root = parse(b"(a (b c) (d e))")
    assert root.is_dirty() is False
    for node in root.walk():
        assert node.is_dirty() is False


def test_mutating_atom_bubbles_dirty_via_walk() -> None:
    root = parse(b"(a (b c) (d e))")
    deep = root.find("b")
    assert deep is not None
    atom = deep.items[1]
    assert isinstance(atom, SAtom)
    atom.set_text("X")

    assert atom.is_dirty() is True
    # Walking the tree marks any ancestor as dirty via its recursive check.
    assert root.is_dirty() is True


def test_sibling_of_dirty_node_stays_clean() -> None:
    root = parse(b"(a (b c) (d e))")
    b = root.find("b")
    assert b is not None
    b.append(SAtom(text="extra"))

    d = root.find("d")
    assert d is not None
    # d itself hasn't been touched — it's individually clean.
    assert d.is_dirty() is False
    assert b.is_dirty() is True
    assert root.is_dirty() is True  # via descendant


def test_list_mutations_flip_dirty() -> None:
    lst = SList(items=[SAtom(text="head", start=0, end=4)], start=0, end=6)
    lst._dirty = False  # pretend freshly-parsed
    assert lst.is_dirty() is False

    lst.append(SAtom(text="x"))
    assert lst.is_dirty() is True


def test_find_and_find_all() -> None:
    root = parse(b"(root (kv 1) (kv 2) (other))")
    first = root.find("kv")
    assert first is not None and isinstance(first.items[1], SAtom) and first.items[1].text == "1"
    everything = root.find_all("kv")
    assert len(everything) == 2


def test_walk_is_depth_first() -> None:
    root = parse(b"(a (b (c 1)) (d 2))")
    heads = [n.text if isinstance(n, SAtom) else n.head for n in root.walk()]
    # Expect: root, 'a', b-list, 'b', c-list, 'c', '1', d-list, 'd', '2'
    assert heads[0] == "a"  # root list: head property
    assert "c" in heads
    assert heads.index("b") < heads.index("c")  # depth-first ordering


def test_explicit_mark_dirty() -> None:
    root = parse(b"(a b)")
    # Simulate a caller that mutates items in place (not going through the API).
    root.items.append(SAtom(text="sneaky"))
    # is_dirty still detects the new node because it has no span (synthesized).
    assert root.is_dirty() is True

    # And mark_dirty is idempotent + explicit.
    root.mark_dirty()
    assert root.is_dirty() is True
