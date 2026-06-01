"""Pure-Python KiCAD S-expression parser.

Scope (per `kimcp-architecture/backends.md`): reads and writes
`.kicad_sch`, `.kicad_pcb`, `.kicad_sym`, `.kicad_mod`, `.kicad_dru`,
`.kicad_wks`. (`.kicad_pro` is JSON, handled elsewhere.)

Design goals:

- **Byte-preserving round-trip** for untouched subtrees (per `testing.md`).
  The writer copies original source bytes for clean nodes; only nodes that
  were modified get canonically reformatted.
- **Lazy-friendly tree** — children are plain lists; callers can descend or
  ignore at will. Span metadata is retained so partial re-serialization
  stays cheap.
- **Small, dependency-free core** — the hot paths are profile-justified
  (see ADR-0002); until then, pure Python.

Public surface kept minimal; consumers import `SexprDocument` for the
file-level facade and `SAtom` / `SList` when they manipulate the tree.
"""

from __future__ import annotations

from kimcp.sexpr.cache import ParseCache
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.errors import SexprParseError
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.sexpr.parser import parse
from kimcp.sexpr.writer import serialize

__all__ = [
    "ParseCache",
    "SAtom",
    "SList",
    "SexprDocument",
    "SexprParseError",
    "parse",
    "serialize",
]
