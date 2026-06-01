"""File-level facade — load a `.kicad_*` file, expose the parsed tree,
write back safely.

Responsibilities:

* Hold the original source bytes (needed by the writer for clean-splice).
* Surface convenience properties used across many tools (`version`,
  `generator`, `top_head`).
* Round-trip validate on save per `safety.md`: serialize → reparse →
  assert structural equality, then atomic write.
* Deliberately narrow — this is *not* a semantic KiCAD model. Semantic
  accessors (components, nets, etc.) live in the per-file helpers added
  alongside their consuming tools.

Behavior intentionally omitted at M1:

* File-watching / cache invalidation (wired in M4 when resources land).
* Schema-level validation of KiCAD file-format versions (per-format
  modules will own that; this layer just exposes the raw value).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kimcp.sexpr.nodes import SAtom, SList
from kimcp.sexpr.parser import parse
from kimcp.sexpr.writer import serialize

log = logging.getLogger(__name__)


@dataclass
class SexprDocument:
    """A parsed KiCAD S-expression file."""

    path: Path
    source: bytes
    root: SList

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> SexprDocument:
        p = Path(path)
        data = p.read_bytes()
        return cls.from_bytes(p, data)

    @classmethod
    def from_bytes(cls, path: str | os.PathLike[str], data: bytes) -> SexprDocument:
        p = Path(path)
        root = parse(data, filename=str(p))
        return cls(path=p, source=data, root=root)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def top_head(self) -> str | None:
        """E.g. 'kicad_sym', 'kicad_sch', 'kicad_pcb'."""
        return self.root.head

    @property
    def version(self) -> str | None:
        """The `(version NNN)` value if present — KiCAD format version."""
        node = self.root.find("version")
        if node is None or len(node.items) < 2:
            return None
        value = node.items[1]
        return value.text if isinstance(value, SAtom) else None

    @property
    def generator(self) -> str | None:
        """The `(generator ...)` value if present."""
        node = self.root.find("generator")
        if node is None or len(node.items) < 2:
            return None
        value = node.items[1]
        return value.text if isinstance(value, SAtom) else None

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def serialize(self) -> bytes:
        """Render the (possibly modified) tree to bytes.

        Clean subtrees get their original bytes spliced back; dirty ones
        are canonically formatted. See `writer.serialize`.
        """
        return serialize(self.root, self.source)

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Atomically write the current tree to disk.

        Per `safety.md` §Backend-specific safety: the S-expression
        backend must never write without round-trip-validating its own
        output. We serialize, re-parse, and assert structural equality
        before swapping the file into place. An atomic rename (within
        the target directory so it stays on the same filesystem) keeps
        readers from seeing a half-written file.
        """
        target = Path(path) if path is not None else self.path
        rendered = self.serialize()

        # Round-trip check — reject writes that don't parse back cleanly.
        try:
            reparsed = parse(rendered, filename=str(target))
        except Exception as exc:
            raise RuntimeError(
                f"round-trip validation failed while saving {target}: {exc}"
            ) from exc
        if not _trees_structurally_equal(self.root, reparsed):
            raise RuntimeError(
                f"round-trip validation detected structural drift when saving {target}"
            )

        # Atomic write: temp file in the same directory, then rename.
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".kimcp-tmp",
            dir=str(target.parent),
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(rendered)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
        except Exception:
            # Best-effort cleanup; swallow errors so the original exception propagates.
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

        # After a successful save, the on-disk bytes are the new source
        # baseline; update internal state so later edits splice from the
        # just-written bytes.
        self.path = target
        self.source = rendered
        self.root = reparsed
        return target


def _trees_structurally_equal(a: SList | SAtom, b: SList | SAtom) -> bool:
    """Deep structural equality on (SList | SAtom) trees.

    We compare *logical* content: atom text + quoted flag, and the
    recursive child list for SList. Spans and dirty flags are ignored —
    they're purely bookkeeping for the writer.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, SAtom):
        assert isinstance(b, SAtom)
        return a.text == b.text and a.quoted == b.quoted
    assert isinstance(a, SList) and isinstance(b, SList)
    if len(a.items) != len(b.items):
        return False
    return all(_trees_structurally_equal(x, y) for x, y in zip(a.items, b.items, strict=True))


__all__ = ["SexprDocument"]
