"""Recursive-descent parser for KiCAD S-expressions.

Input: raw `bytes` of a `.kicad_*` file.
Output: an `SList` root node with `(start, end)` spans populated on every
list and atom so the writer can splice untouched subtrees back in
verbatim.

A KiCAD file always wraps its top-level payload in a single list (e.g.
`(kicad_sym ... )`). We accept exactly one top-level list, with optional
surrounding whitespace; any trailing non-whitespace is an error.
"""

from __future__ import annotations

from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.lexer import tokenize
from kimcp.sexpr.nodes import SAtom, SList, SNode
from kimcp.sexpr.tokens import Token, TokenKind


def parse(source: bytes, *, filename: str | None = None) -> SList:
    """Parse `source` bytes and return the top-level SList.

    `filename` is carried purely for error messages (not stored on the
    tree; SexprDocument owns that).
    """
    _ = filename  # reserved for richer error shaping later
    tokens = list(tokenize(source))
    parser = _Parser(tokens, source)
    root = parser.parse_top_level()
    parser.expect_exhausted()
    return root


class _Parser:
    __slots__ = ("_i", "_source", "_tokens")

    def __init__(self, tokens: list[Token], source: bytes) -> None:
        self._tokens = tokens
        self._i = 0
        self._source = source

    # ------------------------------------------------------------------
    # Entrypoints
    # ------------------------------------------------------------------

    def parse_top_level(self) -> SList:
        if not self._tokens:
            raise SexprParseError(
                "source is empty",
                offset=0,
                source=self._source,
            )

        first = self._peek()
        if first.kind is not TokenKind.LPAREN:
            raise SexprParseError(
                f"expected top-level '(' but got {first.kind.value} {first.text!r}",
                offset=first.start,
                source=self._source,
            )
        return self._parse_list()

    def expect_exhausted(self) -> None:
        if self._i < len(self._tokens):
            extra = self._tokens[self._i]
            raise SexprParseError(
                f"unexpected trailing token {extra.kind.value} {extra.text!r} after top-level list",
                offset=extra.start,
                source=self._source,
            )

    # ------------------------------------------------------------------
    # Core recursion
    # ------------------------------------------------------------------

    def _parse_list(self) -> SList:
        lparen = self._consume()  # '('
        assert lparen.kind is TokenKind.LPAREN

        items: list[SNode] = []
        while True:
            if self._i >= len(self._tokens):
                raise SexprParseError(
                    "unterminated list (EOF before ')')",
                    offset=lparen.start,
                    source=self._source,
                )
            tok = self._peek()
            if tok.kind is TokenKind.RPAREN:
                rparen = self._consume()
                return SList(items=items, start=lparen.start, end=rparen.end)
            if tok.kind is TokenKind.LPAREN:
                items.append(self._parse_list())
                continue
            if tok.kind in (TokenKind.ATOM, TokenKind.QUOTED):
                self._consume()
                items.append(
                    SAtom(
                        text=tok.text,
                        quoted=(tok.kind is TokenKind.QUOTED),
                        start=tok.start,
                        end=tok.end,
                    )
                )
                continue
            # Unreachable — all TokenKind values handled above.
            raise SexprParseError(  # pragma: no cover - defensive
                f"unexpected token kind {tok.kind.value}",
                offset=tok.start,
                source=self._source,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _peek(self) -> Token:
        return self._tokens[self._i]

    def _consume(self) -> Token:
        tok = self._tokens[self._i]
        self._i += 1
        return tok


__all__ = ["parse"]
