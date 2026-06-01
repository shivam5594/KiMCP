"""Token types for the S-expression lexer.

We deliberately conflate numbers and symbols into a single ATOM kind — KiCAD
doesn't distinguish them semantically at the grammar level; consumers
interpret as needed. QUOTED is separate so the writer knows to re-quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TokenKind(Enum):
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    ATOM = "ATOM"  # bare symbol / number / identifier
    QUOTED = "QUOTED"  # double-quoted string (text already unescaped)


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    # For ATOM / QUOTED this is the *logical* text (quotes stripped,
    # escapes decoded). For LPAREN / RPAREN this is "(" / ")".
    text: str
    # Byte span in the original source. End is exclusive.
    start: int
    end: int


__all__ = ["Token", "TokenKind"]
