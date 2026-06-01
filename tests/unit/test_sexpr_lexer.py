"""Unit tests for the S-expression lexer."""

from __future__ import annotations

import pytest

from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.lexer import tokenize
from kimcp.sexpr.tokens import TokenKind


def _kinds(src: bytes) -> list[TokenKind]:
    return [t.kind for t in tokenize(src)]


def _texts(src: bytes) -> list[str]:
    return [t.text for t in tokenize(src)]


def test_empty_input_produces_no_tokens() -> None:
    assert list(tokenize(b"")) == []


def test_parens_and_atoms() -> None:
    assert _kinds(b"(foo bar)") == [
        TokenKind.LPAREN,
        TokenKind.ATOM,
        TokenKind.ATOM,
        TokenKind.RPAREN,
    ]
    assert _texts(b"(foo bar)") == ["(", "foo", "bar", ")"]


def test_quoted_string_with_escapes() -> None:
    toks = list(tokenize(b'("hello \\"world\\"")'))
    quoted = toks[1]
    assert quoted.kind is TokenKind.QUOTED
    assert quoted.text == 'hello "world"'


def test_backslash_and_newline_escapes() -> None:
    toks = list(tokenize(b'"a\\\\b\\nc\\tZ"'))
    assert len(toks) == 1
    assert toks[0].kind is TokenKind.QUOTED
    assert toks[0].text == "a\\b\nc\tZ"


def test_unknown_escape_passes_through_verbatim() -> None:
    # Unknown escapes kept byte-for-byte — matches observed KiCAD behavior.
    toks = list(tokenize(b'"\\q"'))
    assert toks[0].text == "\\q"


def test_numbers_are_atoms() -> None:
    assert _texts(b"(at 1.27 -2.54 90)") == ["(", "at", "1.27", "-2.54", "90", ")"]


def test_whitespace_is_not_emitted_but_spans_survive() -> None:
    toks = list(tokenize(b"( foo\tbar\n)"))
    assert [t.kind for t in toks] == [
        TokenKind.LPAREN,
        TokenKind.ATOM,
        TokenKind.ATOM,
        TokenKind.RPAREN,
    ]
    # Spans point at the original bytes.
    assert toks[1].start == 2 and toks[1].end == 5  # "foo"
    assert toks[2].start == 6 and toks[2].end == 9  # "bar"


def test_unterminated_quoted_string_raises() -> None:
    with pytest.raises(SexprParseError):
        list(tokenize(b'"no closing quote'))


def test_lone_escape_at_eof_raises() -> None:
    with pytest.raises(SexprParseError):
        list(tokenize(b'"abc\\'))


def test_non_bytes_input_rejected() -> None:
    with pytest.raises(TypeError):
        list(tokenize("I am a str, not bytes"))  # type: ignore[arg-type]


def test_utf8_atom() -> None:
    # Non-ASCII symbol — legal in KiCAD files (property values use it freely).
    toks = list(tokenize("µFarads".encode()))
    assert toks[0].kind is TokenKind.ATOM
    assert toks[0].text == "µFarads"
