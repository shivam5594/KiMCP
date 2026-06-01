"""Tree nodes for parsed S-expressions.

Two node types:

* `SAtom`  — a leaf: symbol, number, or quoted string.
* `SList`  — an `( ... )` expression with an ordered list of children.
  Children include the "head" symbol at index 0; no special slot for it
  so structure-preserving edits don't need special cases.

Every node tracks `(start, end)` byte offsets into the original source
and a `_dirty` flag. A node is **clean** iff it has not been modified
*and* all of its descendants are clean. The writer uses cleanliness to
decide: clean → splice original bytes, dirty → canonical reformat.

Modifying helpers on SList always mark `self._dirty = True`. Callers can
also flip `_dirty` manually if they mutate `items` in place, though the
provided methods are the preferred path.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Union

# A node is either a leaf atom or a list of nodes.
SNode = Union["SAtom", "SList"]


@dataclass(slots=True)
class SAtom:
    """Leaf node — a symbol, number, or quoted string."""

    text: str
    """Logical text (for QUOTED, the decoded contents without surrounding
    quotes; for ATOM, the raw symbol / number text)."""

    quoted: bool = False
    """True iff the atom was produced from a `"..."` token."""

    start: int = -1
    """Byte offset in the original source. -1 means 'synthesized'."""

    end: int = -1

    _dirty: bool = False

    # -- mutation ------------------------------------------------------

    def set_text(self, new_text: str, *, quoted: bool | None = None) -> None:
        """Replace the atom's text. Marks the node dirty so the writer
        emits its canonical form instead of splicing old bytes."""
        self.text = new_text
        if quoted is not None:
            self.quoted = quoted
        self._dirty = True

    # -- introspection -------------------------------------------------

    def is_dirty(self) -> bool:
        return self._dirty or self.start < 0

    def has_source_span(self) -> bool:
        return self.start >= 0 and self.end >= self.start


@dataclass(slots=True)
class SList:
    """Parenthesized list of nodes."""

    items: list[SNode] = field(default_factory=list)
    """Children including the head atom at index 0 (by convention)."""

    start: int = -1
    end: int = -1
    _dirty: bool = False

    # -- convenience ---------------------------------------------------

    @property
    def head(self) -> str | None:
        """The first child's text if it's an atom, else None.

        KiCAD convention is that every list starts with a symbol atom. We
        don't enforce it syntactically — some generated files include
        slightly looser shapes — but we surface it when present.
        """
        if not self.items:
            return None
        first = self.items[0]
        return first.text if isinstance(first, SAtom) else None

    @property
    def tail(self) -> list[SNode]:
        """Children after the head atom."""
        return self.items[1:] if self.items else []

    # -- search --------------------------------------------------------

    def find(self, head: str) -> SList | None:
        """Return the first child SList whose head atom equals `head`."""
        for child in self.items:
            if isinstance(child, SList) and child.head == head:
                return child
        return None

    def find_all(self, head: str) -> list[SList]:
        """Return every child SList whose head atom equals `head`."""
        return [c for c in self.items if isinstance(c, SList) and c.head == head]

    def walk(self) -> Iterator[SNode]:
        """Depth-first iterator over self + all descendants."""
        yield self
        for child in self.items:
            if isinstance(child, SList):
                yield from child.walk()
            else:
                yield child

    # -- mutation ------------------------------------------------------

    def set_items(self, items: list[SNode]) -> None:
        self.items = list(items)
        self._dirty = True

    def append(self, node: SNode) -> None:
        self.items.append(node)
        self._dirty = True

    def insert(self, index: int, node: SNode) -> None:
        self.items.insert(index, node)
        self._dirty = True

    def replace(self, index: int, node: SNode) -> None:
        self.items[index] = node
        self._dirty = True

    def remove_at(self, index: int) -> SNode:
        removed = self.items.pop(index)
        self._dirty = True
        return removed

    def mark_dirty(self) -> None:
        """Explicitly flag this list as modified.

        Needed only when callers mutate `items` in place without going
        through the methods above.
        """
        self._dirty = True

    # -- cleanliness ---------------------------------------------------

    def is_dirty(self) -> bool:
        if self._dirty or self.start < 0:
            return True
        for child in self.items:
            if isinstance(child, SList):
                if child.is_dirty():
                    return True
            elif child.is_dirty():
                return True
        return False

    def has_source_span(self) -> bool:
        return self.start >= 0 and self.end >= self.start


__all__ = ["SAtom", "SList", "SNode"]
