"""Parse errors for the S-expression layer.

Carries a source offset so callers can format a helpful message. Line /
column translation is done lazily to avoid scanning the whole file for
every error.
"""

from __future__ import annotations


class SexprParseError(ValueError):
    """Raised when an S-expression source cannot be tokenized or parsed."""

    def __init__(self, message: str, *, offset: int, source: bytes | None = None) -> None:
        self.offset = offset
        self.source = source
        super().__init__(self._format(message))

    def _format(self, message: str) -> str:
        if self.source is None:
            return f"{message} (at byte {self.offset})"
        line, col = _line_col(self.source, self.offset)
        return f"{message} (line {line}, col {col})"


def _line_col(source: bytes, offset: int) -> tuple[int, int]:
    if offset < 0:
        return (1, 1)
    clamped = min(offset, len(source))
    prefix = source[:clamped]
    line = prefix.count(b"\n") + 1
    last_nl = prefix.rfind(b"\n")
    col = clamped - last_nl if last_nl >= 0 else clamped + 1
    return (line, col)


__all__ = ["SexprParseError"]
