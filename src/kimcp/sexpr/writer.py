"""S-expression serializer.

Two-mode render:

1. **Clean subtrees** — splice the original source bytes back in verbatim.
   This is the whole point of carrying `(start, end)` on every node: if
   nothing inside the subtree was modified, we reproduce the input
   byte-for-byte without touching whitespace, newlines, or quoting.

2. **Dirty subtrees** — emit a canonical text form.
   * Atoms: quote iff `quoted=True`, escape only the characters that must
     be escaped (`"` `\\`).
   * Lists: indent-2 across multiple lines when any child is itself a
     list; inline otherwise. Deterministic output — running the formatter
     twice produces the same bytes.

The whole operation runs in O(n) over the output size.
"""

from __future__ import annotations

from kimcp.sexpr.nodes import SAtom, SList, SNode


def serialize(root: SList, source: bytes | None = None) -> bytes:
    """Render `root` to bytes.

    `source` is the original source (as parsed) — required whenever any
    node in the tree is clean with a valid span, since the writer will
    splice from it. For fully-synthetic trees (no spans), pass `None`.
    """
    buf = bytearray()
    _write_node(root, source, buf, indent=0)
    if not buf.endswith(b"\n"):
        buf.append(0x0A)
    return bytes(buf)


def _write_node(node: SNode, source: bytes | None, buf: bytearray, *, indent: int) -> None:
    # Splice path: clean + has span + original bytes available.
    if source is not None and node.has_source_span() and not node.is_dirty():
        buf.extend(source[node.start : node.end])
        return

    if isinstance(node, SAtom):
        buf.extend(_render_atom(node))
        return

    assert isinstance(node, SList)
    _write_list_canonical(node, source, buf, indent=indent)


def _write_list_canonical(
    node: SList, source: bytes | None, buf: bytearray, *, indent: int
) -> None:
    buf.append(0x28)  # '('

    items = node.items
    if not items:
        buf.append(0x29)  # ')'
        return

    # Head + inline payload decision.
    # If any non-head child is a list, break onto multiple lines — the KiCAD
    # convention and also what keeps large files readable.
    multiline = any(isinstance(c, SList) for c in items[1:])

    # Head always immediately follows '(' with no preceding space.
    _write_node(items[0], source, buf, indent=indent + 1)

    if multiline:
        child_indent = b"\n" + b"\t" * (indent + 1)
        for child in items[1:]:
            buf.extend(child_indent)
            _write_node(child, source, buf, indent=indent + 1)
        # Closing paren hugs the last child — matches KiCAD's own style.
        buf.append(0x29)
    else:
        for child in items[1:]:
            buf.append(0x20)  # ' '
            _write_node(child, source, buf, indent=indent + 1)
        buf.append(0x29)  # ')'


# ---------------------------------------------------------------------------
# Atom rendering
# ---------------------------------------------------------------------------

# Escape only the minimal set required by the lexer's string-decoder.
# KiCAD itself escapes `"` and `\\`; everything else rides through.
_ESCAPE_CHARS = {
    0x22: b'\\"',
    0x5C: b"\\\\",
    0x0A: b"\\n",
    0x0D: b"\\r",
    0x09: b"\\t",
}


def _render_atom(atom: SAtom) -> bytes:
    if atom.quoted:
        return _render_quoted(atom.text)
    # Bare atoms need quoting if they contain any terminator character
    # (otherwise they'd tokenize back differently). Empty bare atoms are
    # illegal; promote to quoted.
    if not atom.text or _needs_quoting(atom.text):
        return _render_quoted(atom.text)
    return atom.text.encode("utf-8")


def _needs_quoting(s: str) -> bool:
    for ch in s:
        b = ord(ch)
        if b in (0x28, 0x29, 0x22, 0x20, 0x09, 0x0A, 0x0D):
            return True
    return False


def _render_quoted(text: str) -> bytes:
    out = bytearray(b'"')
    for ch in text:
        b = ord(ch)
        if b in _ESCAPE_CHARS:
            out.extend(_ESCAPE_CHARS[b])
        else:
            out.extend(ch.encode("utf-8"))
    out.append(0x22)
    return bytes(out)


__all__ = ["serialize"]
