"""Unit tests for lib_attach_3d_model (M47)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.lib_add_footprint import (
    FootprintPad,
    LibAddFootprintInput,
    LibAddFootprintTool,
)
from kimcp.tools.builtin.lib_attach_3d_model import (
    LibAttach3dModelInput,
    LibAttach3dModelTool,
    Xyz,
)


def _cfg(tmp_path: Path):
    return load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
        session_overrides={"safety": {"snapshot_mode": "off"}},
    )


async def _seed_footprint(tmp_path: Path, name: str = "X") -> Path:
    """Write a valid .kicad_mod via the M45 tool and return its path."""
    lib = tmp_path / "seeded.pretty"
    tool = LibAddFootprintTool(_cfg(tmp_path))
    out = await tool.run(
        LibAddFootprintInput(
            lib_path=lib,
            footprint_name=name,
            pads=[
                FootprintPad(
                    number="1", pad_type="smd", shape="rect",
                    x=0, y=0, size_w=1, size_h=1,
                )
            ],
        )
    )
    assert out.status == "ok", f"seed failed: {out.note!r}"
    return lib / f"{name}.kicad_mod"


def _models(root: SList) -> list[SList]:
    return [c for c in root.items if isinstance(c, SList) and c.head == "model"]


def _xyz(node: SList, field: str) -> tuple[float, float, float] | None:
    """Return the (x, y, z) triple under ``(field (xyz X Y Z))``."""
    wrap = node.find(field)
    if wrap is None:
        return None
    xyz = wrap.find("xyz")
    if xyz is None or len(xyz.items) < 4:
        return None
    coords = []
    for i in range(1, 4):
        a = xyz.items[i]
        if not isinstance(a, SAtom):
            return None
        coords.append(float(a.text))
    return tuple(coords)  # type: ignore[return-value]


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = LibAttach3dModelTool()
    assert tool.name == "lib_attach_3d_model"
    assert tool.classification == ToolClass.MUTATE
    assert tool.mutates is True
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_attaches_step_model(tmp_path: Path) -> None:
    fp = await _seed_footprint(tmp_path)
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(
            footprint_path=fp,
            model_path="${KICAD6_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603.step",
        )
    )
    assert out.status == "ok", f"failed: {out.note!r}"
    assert out.replaced_existing is False
    assert out.model_count == 1

    doc = SexprDocument.from_path(fp)
    models = _models(doc.root)
    assert len(models) == 1
    # Path atom is the first child after the head.
    assert isinstance(models[0].items[1], SAtom)
    assert (
        models[0].items[1].text
        == "${KICAD6_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603.step"
    )


@pytest.mark.asyncio
async def test_default_transform_blocks_present(tmp_path: Path) -> None:
    """Even with all-default transforms, the three blocks must emit."""
    fp = await _seed_footprint(tmp_path)
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(footprint_path=fp, model_path="part.step")
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(fp)
    model = _models(doc.root)[0]
    assert _xyz(model, "offset") == (0.0, 0.0, 0.0)
    assert _xyz(model, "scale") == (1.0, 1.0, 1.0)
    assert _xyz(model, "rotate") == (0.0, 0.0, 0.0)


@pytest.mark.asyncio
async def test_custom_transforms_round_trip(tmp_path: Path) -> None:
    fp = await _seed_footprint(tmp_path)
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(
            footprint_path=fp,
            model_path="part.step",
            offset=Xyz(x=0.5, y=-0.25, z=0.1),
            scale=Xyz(x=1.1, y=1.0, z=0.9),
            rotate=Xyz(x=0.0, y=0.0, z=90.0),
        )
    )
    assert out.status == "ok"
    doc = SexprDocument.from_path(fp)
    model = _models(doc.root)[0]
    assert _xyz(model, "offset") == (0.5, -0.25, 0.1)
    assert _xyz(model, "scale") == (1.1, 1.0, 0.9)
    assert _xyz(model, "rotate") == (0.0, 0.0, 90.0)


@pytest.mark.asyncio
async def test_stp_normalized_to_step(tmp_path: Path) -> None:
    """.stp is an alias for .step; the tool normalizes on write."""
    fp = await _seed_footprint(tmp_path)
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(footprint_path=fp, model_path="part.stp")
    )
    assert out.status == "ok"
    assert out.model_path == "part.step"
    doc = SexprDocument.from_path(fp)
    model = _models(doc.root)[0]
    assert isinstance(model.items[1], SAtom)
    assert model.items[1].text == "part.step"


@pytest.mark.asyncio
async def test_wrl_accepted_as_is(tmp_path: Path) -> None:
    fp = await _seed_footprint(tmp_path)
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(footprint_path=fp, model_path="part.wrl")
    )
    assert out.status == "ok"
    assert out.model_path == "part.wrl"


@pytest.mark.asyncio
async def test_append_adds_second_model(tmp_path: Path) -> None:
    """Default behaviour: multiple attach calls accumulate (no replace)."""
    fp = await _seed_footprint(tmp_path)
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    await tool.run(
        LibAttach3dModelInput(footprint_path=fp, model_path="first.step")
    )
    out = await tool.run(
        LibAttach3dModelInput(footprint_path=fp, model_path="second.step")
    )
    assert out.status == "ok"
    assert out.replaced_existing is False
    assert out.model_count == 2

    doc = SexprDocument.from_path(fp)
    models = _models(doc.root)
    assert len(models) == 2
    texts = [m.items[1].text for m in models if isinstance(m.items[1], SAtom)]
    assert "first.step" in texts
    assert "second.step" in texts


@pytest.mark.asyncio
async def test_replace_swaps_first_model(tmp_path: Path) -> None:
    fp = await _seed_footprint(tmp_path)
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    await tool.run(
        LibAttach3dModelInput(footprint_path=fp, model_path="old.step")
    )
    out = await tool.run(
        LibAttach3dModelInput(
            footprint_path=fp, model_path="new.step", replace=True
        )
    )
    assert out.status == "ok"
    assert out.replaced_existing is True
    assert out.model_count == 1  # replace keeps total at 1

    doc = SexprDocument.from_path(fp)
    models = _models(doc.root)
    assert len(models) == 1
    assert isinstance(models[0].items[1], SAtom)
    assert models[0].items[1].text == "new.step"


@pytest.mark.asyncio
async def test_replace_without_existing_falls_back_to_append(tmp_path: Path) -> None:
    """replace=True on a bare footprint should still end up with one model."""
    fp = await _seed_footprint(tmp_path)
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(
            footprint_path=fp, model_path="only.step", replace=True
        )
    )
    assert out.status == "ok"
    # Nothing to replace → reports append, not replace.
    assert out.replaced_existing is False
    assert out.model_count == 1


@pytest.mark.asyncio
async def test_model_inserted_before_embedded_fonts(tmp_path: Path) -> None:
    """KiCAD convention: (model ...) sits before the trailing (embedded_fonts ...)."""
    fp = await _seed_footprint(tmp_path)
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    await tool.run(
        LibAttach3dModelInput(footprint_path=fp, model_path="part.step")
    )
    doc = SexprDocument.from_path(fp)
    # Last child should remain (embedded_fonts ...).
    last = doc.root.items[-1]
    assert isinstance(last, SList) and last.head == "embedded_fonts"
    # And the model block appears somewhere before it.
    model_indices = [
        i
        for i, c in enumerate(doc.root.items)
        if isinstance(c, SList) and c.head == "model"
    ]
    ef_indices = [
        i
        for i, c in enumerate(doc.root.items)
        if isinstance(c, SList) and c.head == "embedded_fonts"
    ]
    assert model_indices and ef_indices
    assert model_indices[0] < ef_indices[0]


@pytest.mark.asyncio
async def test_dry_run_does_not_mutate(tmp_path: Path) -> None:
    fp = await _seed_footprint(tmp_path)
    before = fp.read_bytes()
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(
            footprint_path=fp, model_path="part.step", dry_run=True
        )
    )
    assert out.status == "dry_run"
    # File contents untouched.
    assert fp.read_bytes() == before
    assert out.model_path == "part.step"


# -- error paths ----------------------------------------------------------


@pytest.mark.asyncio
async def test_footprint_not_found_missing(tmp_path: Path) -> None:
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(
            footprint_path=tmp_path / "ghost.kicad_mod",
            model_path="part.step",
        )
    )
    assert out.status == "footprint_not_found"


@pytest.mark.asyncio
async def test_footprint_not_found_wrong_suffix(tmp_path: Path) -> None:
    bad = tmp_path / "oops.txt"
    bad.write_text("not a footprint", encoding="utf-8")
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(footprint_path=bad, model_path="part.step")
    )
    assert out.status == "footprint_not_found"


@pytest.mark.asyncio
async def test_footprint_not_found_is_directory(tmp_path: Path) -> None:
    d = tmp_path / "dir.kicad_mod"
    d.mkdir()
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(footprint_path=d, model_path="part.step")
    )
    assert out.status == "footprint_not_found"


@pytest.mark.asyncio
async def test_invalid_schema_top_head(tmp_path: Path) -> None:
    bad = tmp_path / "wrong.kicad_mod"
    bad.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(footprint_path=bad, model_path="part.step")
    )
    assert out.status == "invalid_schema"


@pytest.mark.asyncio
async def test_parse_failed(tmp_path: Path) -> None:
    bad = tmp_path / "broken.kicad_mod"
    bad.write_text("(footprint (oops", encoding="utf-8")
    tool = LibAttach3dModelTool(_cfg(tmp_path))
    out = await tool.run(
        LibAttach3dModelInput(footprint_path=bad, model_path="part.step")
    )
    assert out.status == "parse_failed"


# -- Pydantic-level validation -------------------------------------------


def test_invalid_input_bad_suffix() -> None:
    with pytest.raises(ValueError):
        LibAttach3dModelInput(
            footprint_path=Path("/tmp/x.kicad_mod"),
            model_path="part.obj",  # not .step/.stp/.wrl
        )


def test_invalid_input_empty_model_path() -> None:
    with pytest.raises(ValueError):
        LibAttach3dModelInput(
            footprint_path=Path("/tmp/x.kicad_mod"),
            model_path="",
        )
