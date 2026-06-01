"""Unit tests for SexprDocument — load, serialize, save, round-trip guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sexpr" / "simple_resistor.kicad_sym"


def test_from_path_parses_fixture() -> None:
    doc = SexprDocument.from_path(FIXTURE)
    assert doc.top_head == "kicad_symbol_lib"
    assert doc.version == "20231120"
    assert doc.generator == "kimcp-test"


def test_serialize_roundtrip_preserves_bytes() -> None:
    doc = SexprDocument.from_path(FIXTURE)
    data = doc.serialize()
    # Parser + writer on a clean tree must be a pure splice (plus at most
    # a single trailing newline).
    original = FIXTURE.read_bytes()
    if original.endswith(b"\n"):
        assert data == original
    else:
        assert data == original + b"\n"


def test_save_is_atomic_and_updates_state(tmp_path: Path) -> None:
    doc = SexprDocument.from_path(FIXTURE)
    target = tmp_path / "out.kicad_sym"
    doc.save(target)

    assert target.exists()
    # The document's path + source should now track the written bytes.
    assert doc.path == target
    assert target.read_bytes() == doc.source
    # And the tree is clean again (re-parsed during save).
    assert doc.root.is_dirty() is False


def test_save_blocks_on_round_trip_failure(tmp_path: Path, monkeypatch) -> None:
    doc = SexprDocument.from_path(FIXTURE)

    # Force the serializer to produce garbage to trigger the reparse guard.
    def _broken_serialize(_root, _source):
        return b"not an s-expression"

    monkeypatch.setattr("kimcp.sexpr.document.serialize", _broken_serialize)

    with pytest.raises(RuntimeError):
        doc.save(tmp_path / "out.kicad_sym")

    # And nothing was written.
    assert not (tmp_path / "out.kicad_sym").exists()


def test_modifying_generator_survives_save_roundtrip(tmp_path: Path) -> None:
    doc = SexprDocument.from_path(FIXTURE)

    gen = doc.root.find("generator")
    assert gen is not None
    gen_value = gen.items[1]
    assert isinstance(gen_value, SAtom)
    gen_value.set_text("kimcp-roundtrip")

    out_path = tmp_path / "modified.kicad_sym"
    doc.save(out_path)

    # Re-load and verify the value carried over.
    reloaded = SexprDocument.from_path(out_path)
    assert reloaded.generator == "kimcp-roundtrip"
    assert reloaded.version == "20231120"  # untouched field still present
