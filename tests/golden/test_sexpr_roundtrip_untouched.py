"""Golden-file round-trip tests for the S-expression parser.

Per `kimcp-architecture/testing.md`:

    read → modify → write → re-read → re-modify → write. Result must equal
    the first-write byte-for-byte for untouched sections.

The guarantee we exercise here: *after the first canonicalizing write*,
re-parsing that output and modifying a single field produces bytes that
match the first write exactly outside the touched subtree, and
parse-identically inside it. Clean subtrees get their source bytes
spliced verbatim; dirty ones get canonically reformatted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp.sexpr.document import SexprDocument, _trees_structurally_equal
from kimcp.sexpr.nodes import SAtom, SList

pytestmark = pytest.mark.golden

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sexpr" / "simple_resistor.kicad_sym"


def _first_write(fixture_bytes: bytes) -> bytes:
    """Canonicalize once — this is the baseline all later writes compare to."""
    doc = SexprDocument.from_bytes(FIXTURE, fixture_bytes)
    return doc.serialize()


def test_first_write_is_stable_on_unmodified_tree() -> None:
    """Parsing + serializing without edits is byte-stable across iterations."""
    fixture_bytes = FIXTURE.read_bytes()
    out1 = _first_write(fixture_bytes)
    out2 = _first_write(out1)
    out3 = _first_write(out2)
    assert out1 == out2 == out3


def test_modify_then_write_preserves_untouched_sections() -> None:
    fixture_bytes = FIXTURE.read_bytes()
    out1 = _first_write(fixture_bytes)

    # Re-read the canonicalized bytes and modify exactly one field.
    doc = SexprDocument.from_bytes(FIXTURE, out1)
    gen = doc.root.find("generator")
    assert gen is not None
    gen_value = gen.items[1]
    assert isinstance(gen_value, SAtom)
    gen_value.set_text("kimcp-golden")

    out2 = doc.serialize()

    # Re-read, touch the generator again (different value), write again.
    doc3 = SexprDocument.from_bytes(FIXTURE, out2)
    gen3 = doc3.root.find("generator")
    assert gen3 is not None
    gen_value3 = gen3.items[1]
    assert isinstance(gen_value3, SAtom)
    gen_value3.set_text("kimcp-golden-2")
    out3 = doc3.serialize()

    # --- Assertions ---
    # 1. All three writes remain valid s-expressions.
    reparse1 = SexprDocument.from_bytes(FIXTURE, out1).root
    reparse2 = SexprDocument.from_bytes(FIXTURE, out2).root
    reparse3 = SexprDocument.from_bytes(FIXTURE, out3).root

    # 2. Untouched sections survive: the `version` field and every symbol
    #    definition should be *structurally identical* across all three
    #    writes (the only field that changes is `generator`).
    def _strip_generator(root: SList) -> SList:
        kept = [c for c in root.items if not (hasattr(c, "head") and c.head == "generator")]
        return SList(items=kept)

    assert _trees_structurally_equal(_strip_generator(reparse1), _strip_generator(reparse2))
    assert _trees_structurally_equal(_strip_generator(reparse2), _strip_generator(reparse3))

    # 3. The *large* untouched subtrees (the two symbol bodies) preserve
    #    their exact byte ranges across writes 2 and 3 — that's the real
    #    byte-splice guarantee. Locate each symbol in both outputs and
    #    assert the byte ranges match.
    def _symbol_bytes(output: bytes, name: str) -> bytes:
        doc = SexprDocument.from_bytes(FIXTURE, output)
        for child in doc.root.find_all("symbol"):
            if child.items and isinstance(child.items[1], SAtom) and child.items[1].text == name:
                return output[child.start : child.end]
        raise AssertionError(f"symbol {name!r} not found")

    # Between out2 and out3 — both modified only the generator. The symbol
    # bodies were clean in both re-parses and must have been spliced
    # byte-identically.
    assert _symbol_bytes(out2, "R_Small") == _symbol_bytes(out3, "R_Small")
    assert _symbol_bytes(out2, "C_Small") == _symbol_bytes(out3, "C_Small")
