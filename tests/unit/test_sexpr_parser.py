"""Unit tests for the S-expression parser."""

from __future__ import annotations

import pytest

from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.sexpr.parser import parse


def test_minimal_list() -> None:
    root = parse(b"(foo)")
    assert isinstance(root, SList)
    assert root.head == "foo"
    assert len(root.items) == 1
    assert root.start == 0
    assert root.end == 5  # "(foo)" end-exclusive


def test_nested_structure() -> None:
    root = parse(b'(kicad_sym (version 20231120) (generator "eeschema"))')
    assert root.head == "kicad_sym"
    version = root.find("version")
    assert version is not None
    assert len(version.items) == 2
    assert isinstance(version.items[1], SAtom)
    assert version.items[1].text == "20231120"
    generator = root.find("generator")
    assert generator is not None
    assert isinstance(generator.items[1], SAtom)
    assert generator.items[1].quoted is True
    assert generator.items[1].text == "eeschema"


def test_spans_cover_original_bytes() -> None:
    src = b"(a (b c) d)"
    root = parse(src)
    assert src[root.start : root.end] == src
    inner = root.find("b")
    assert inner is not None
    assert src[inner.start : inner.end] == b"(b c)"


def test_leading_and_trailing_whitespace_allowed() -> None:
    root = parse(b"   \n(foo)\n\n")
    assert root.head == "foo"


def test_empty_source_raises() -> None:
    with pytest.raises(SexprParseError):
        parse(b"")


def test_top_level_must_be_list() -> None:
    with pytest.raises(SexprParseError):
        parse(b"foo")


def test_trailing_garbage_after_top_level_rejected() -> None:
    with pytest.raises(SexprParseError):
        parse(b"(foo) bar")


def test_unclosed_list_rejected() -> None:
    with pytest.raises(SexprParseError):
        parse(b"(foo (bar)")


def test_find_all_returns_all_matches() -> None:
    root = parse(b"(root (x 1) (y 2) (x 3) (x 4))")
    matches = root.find_all("x")
    assert len(matches) == 3
    values = [m.items[1].text for m in matches if isinstance(m.items[1], SAtom)]
    assert values == ["1", "3", "4"]
