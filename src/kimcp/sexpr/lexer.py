"""KiCAD S-expression lexer.

Grammar (simplified):

    file   := sexpr
    sexpr  := '(' atom sexpr* ')'
    atom   := symbol | quoted-string
    symbol := any run of non-whitespace, non-parenthesis, non-quote bytes
    string := '"' ('\\' any | [^"\\])* '"'

KiCAD is permissive on what an atom looks like (numbers, hex, identifiers,
punctuation like `/` are all plain atoms). We therefore treat any
non-paren, non-quote, non-whitespace run as a single ATOM token and defer
interpretation to consumers.

Quoted strings use C-style backslash escapes: `\\"` `\\\\` `\\n` `\\r` `\\t`.
Unknown escapes are preserved verbatim (per observed KiCAD behavior).

Span accounting: every token carries `(start, end)` byte offsets into the
original source. Whitespace between tokens is NOT emitted; the writer
reconstructs it when re-serializing dirty subtrees. For untouched
subtrees, the writer splices original bytes back in via the stored span
on the enclosing SList.
"""

from __future__ import annotations

from collections.abc import Iterator

from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.tokens import Token, TokenKind

# Bytes that terminate a bare-atom run.
_ATOM_TERMINATORS = frozenset(b'() \t\r\n"')


def tokenize(source: bytes) -> Iterator[Token]:
    """Yield tokens from `source`. Whitespace is consumed and not emitted."""
    if not isinstance(source, (bytes, bytearray)):
        raise TypeError(f"tokenize expects bytes, got {type(source).__name__}")

    i = 0
    n = len(source)

    while i < n:
        b = source[i]

        # Whitespace
        if b in (0x20, 0x09, 0x0A, 0x0D):  # space, tab, LF, CR
            i += 1
            continue

        # Parens
        if b == 0x28:  # '('
            yield Token(TokenKind.LPAREN, "(", i, i + 1)
            i += 1
            continue
        if b == 0x29:  # ')'
            yield Token(TokenKind.RPAREN, ")", i, i + 1)
            i += 1
            continue

        # Quoted string
        if b == 0x22:  # '"'
            text, end = _read_quoted(source, i)
            yield Token(TokenKind.QUOTED, text, i, end)
            i = end
            continue

        # Bare atom
        start = i
        while i < n and source[i] not in _ATOM_TERMINATORS:
            i += 1
        if i == start:
            raise SexprParseError(
                f"unexpected byte 0x{b:02x} in s-expression source",
                offset=start,
                source=bytes(source),
            )
        raw = bytes(source[start:i])
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SexprParseError(
                f"invalid UTF-8 in atom: {exc}",
                offset=start,
                source=bytes(source),
            ) from exc
        yield Token(TokenKind.ATOM, text, start, i)


def _read_quoted(source: bytes, start: int) -> tuple[str, int]:
    """Read a `"..."` string starting at `source[start]`. Returns (decoded, end)."""
    n = len(source)
    assert source[start] == 0x22

    out = bytearray()
    i = start + 1
    while i < n:
        b = source[i]
        if b == 0x22:  # closing quote
            try:
                return out.decode("utf-8"), i + 1
            except UnicodeDecodeError as exc:
                raise SexprParseError(
                    f"invalid UTF-8 in quoted string: {exc}",
                    offset=start,
                    source=bytes(source),
                ) from exc
        if b == 0x5C:  # backslash escape
            if i + 1 >= n:
                raise SexprParseError(
                    "unterminated escape in quoted string",
                    offset=i,
                    source=bytes(source),
                )
            nxt = source[i + 1]
            if nxt == 0x6E:  # \n
                out.append(0x0A)
            elif nxt == 0x72:  # \r
                out.append(0x0D)
            elif nxt == 0x74:  # \t
                out.append(0x09)
            elif nxt == 0x22:  # \"
                out.append(0x22)
            elif nxt == 0x5C:  # \\
                out.append(0x5C)
            else:
                # Preserve unknown escapes verbatim (per observed KiCAD files).
                out.append(b)
                out.append(nxt)
            i += 2
            continue
        out.append(b)
        i += 1

    raise SexprParseError(
        "unterminated quoted string",
        offset=start,
        source=bytes(source),
    )


__all__ = ["tokenize"]
