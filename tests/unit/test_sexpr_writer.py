"""Unit tests for the S-expression writer.

Covers the clean-splice path, the canonical-format path for dirty
subtrees, atom quoting rules, and the splice-inside-dirty-parent case.
"""

from __future__ import annotations

from kimcp.sexpr.nodes import SAtom, SList
from kimcp.sexpr.parser import parse
from kimcp.sexpr.writer import serialize


def test_clean_tree_splices_source_verbatim() -> None:
    src = b'(foo  (bar "baz")\n   (qux 1))'
    root = parse(src)
    out = serialize(root, src)
    # Writer appends a trailing newline if none; fixture has no trailing \n,
    # so expect exactly src + b"\n".
    assert out == src + b"\n"


def test_trailing_newline_preserved_once_only() -> None:
    src = b"(foo)\n"
    out = serialize(parse(src), src)
    assert out == src  # already ends with \n, no double


def test_dirty_atom_canonicalizes_parent() -> None:
    src = b'(prop "old" (at 0 0))'
    root = parse(src)
    # Flip the quoted atom to a new value.
    first_child = root.items[1]
    assert isinstance(first_child, SAtom)
    first_child.set_text("new")

    out = serialize(root, src)
    # The parent list is now dirty; the writer canonical-formats it.
    # "new" retains quoted=True because set_text didn't change the flag.
    assert b'"new"' in out
    assert b'"old"' not in out
    # The (at 0 0) child was not modified, so its original bytes get spliced.
    assert b"(at 0 0)" in out


def test_replace_child_marks_parent_dirty() -> None:
    src = b"(a (b 1) (c 2))"
    root = parse(src)
    new_child = SList(items=[SAtom(text="b"), SAtom(text="99")])
    root.replace(1, new_child)

    out = serialize(root, src)
    assert b"b 99" in out  # dirty canonical form
    assert b"(c 2)" in out  # clean child spliced


def test_append_child_marks_parent_dirty() -> None:
    src = b"(a (b 1))"
    root = parse(src)
    root.append(SList(items=[SAtom(text="c"), SAtom(text="2")]))

    out = serialize(root, src)
    # Canonical form breaks to multiple lines when there are sub-lists.
    assert b"(b 1)" in out
    assert b"(c 2)" in out
    # Structure re-parses cleanly.
    reparsed = parse(out)
    assert reparsed.find("c") is not None


def test_atom_needs_quoting_when_containing_whitespace() -> None:
    # Synthetic atom (no source span) — must canonical-render.
    root = SList(
        items=[
            SAtom(text="prop"),
            SAtom(text="has space", quoted=False),
        ]
    )
    out = serialize(root, source=None)
    # Bare atom with space needs to become quoted for round-trip safety.
    assert b'"has space"' in out


def test_atom_preserves_quoted_flag_for_empty_string() -> None:
    root = SList(items=[SAtom(text="footprint"), SAtom(text="", quoted=True)])
    out = serialize(root, source=None)
    assert out.rstrip(b"\n") == b'(footprint "")'


def test_synthetic_tree_round_trips() -> None:
    # Built from scratch (no spans), must still render and re-parse.
    root = SList(
        items=[
            SAtom(text="kicad_sym"),
            SList(items=[SAtom(text="version"), SAtom(text="20231120")]),
            SList(items=[SAtom(text="generator"), SAtom(text="kimcp", quoted=True)]),
        ]
    )
    out = serialize(root, source=None)
    reparsed = parse(out)
    assert reparsed.head == "kicad_sym"
    assert reparsed.find("version") is not None
    assert reparsed.find("generator") is not None


def test_empty_list_renders() -> None:
    root = SList(items=[])
    out = serialize(root, source=None)
    assert out.rstrip(b"\n") == b"()"
