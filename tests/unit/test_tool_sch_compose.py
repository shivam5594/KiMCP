"""Focused unit tests for sch_compose — the batched mutation primitive.

Exercises the three batch-control modes (default abort, continue_on_error,
dry_run), the empty-batch short-circuit, and the per-step dispatch for
each supported op. Detailed per-builder semantics already live in the
underlying tool tests (test_tool_sch_add_wire, …) so we only verify the
compose-specific glue: ordering, single-snapshot, abort semantics.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kimcp.config import Config, SafetyCfg
from kimcp.tools.builtin.sch_compose import (
    AddJunctionStep,
    AddLabelStep,
    AddNoConnectStep,
    AddWireStep,
    SchComposeInput,
    SchComposeTool,
)

_MIN_SCH = textwrap.dedent(
    """\
    (kicad_sch (version 20231120) (generator eeschema)
      (uuid "00000000-0000-0000-0000-000000000001")
      (lib_symbols)
    )
    """
)


def _make_sch(tmp_path: Path) -> Path:
    p = tmp_path / "test.kicad_sch"
    p.write_text(_MIN_SCH)
    return p


def _cfg() -> Config:
    """Config with snapshots disabled — tmp dirs may contain system files
    the copy-mode snapshot can't read."""
    return Config(safety=SafetyCfg(snapshot_mode="off"))


async def test_empty_batch_short_circuits(tmp_path: Path) -> None:
    p = _make_sch(tmp_path)
    tool = SchComposeTool(config=_cfg())
    out = await tool.run(SchComposeInput(sch_path=p, steps=[]))
    assert out.status == "empty_batch"
    assert out.applied == 0
    assert out.results == []


async def test_mixed_ops_apply_in_order(tmp_path: Path) -> None:
    p = _make_sch(tmp_path)
    tool = SchComposeTool(config=_cfg())
    out = await tool.run(
        SchComposeInput(
            sch_path=p,
            steps=[
                AddWireStep(op="add_wire", start_x=0, start_y=0, end_x=2.54, end_y=0),
                AddJunctionStep(op="add_junction", at_x=2.54, at_y=0),
                AddLabelStep(op="add_label", text="MOSI", at_x=2.54, at_y=0),
                AddNoConnectStep(op="add_no_connect", at_x=5.08, at_y=0),
            ],
        )
    )
    assert out.status == "ok"
    assert out.applied == 4
    assert [r.op for r in out.results] == [
        "add_wire",
        "add_junction",
        "add_label",
        "add_no_connect",
    ]
    assert all(r.status == "ok" for r in out.results)
    assert all(r.uuid is not None for r in out.results)

    # File reflects all four mutations. Writer canonical form puts the
    # text atom on its own line, so match the head only.
    text = p.read_text()
    assert "(wire" in text
    assert "(junction" in text
    assert "(label" in text and '"MOSI"' in text
    assert "(no_connect" in text


async def test_abort_on_error_does_not_save(tmp_path: Path) -> None:
    p = _make_sch(tmp_path)
    pre = p.read_text()
    tool = SchComposeTool(config=_cfg())
    out = await tool.run(
        SchComposeInput(
            sch_path=p,
            steps=[
                AddWireStep(op="add_wire", start_x=0, start_y=0, end_x=2.54, end_y=0),
                # Zero-length after snap — fails.
                AddWireStep(op="add_wire", start_x=5.08, start_y=0, end_x=5.08, end_y=0),
                AddWireStep(op="add_wire", start_x=10, start_y=0, end_x=15, end_y=0),
            ],
        )
    )
    assert out.status == "aborted"
    assert out.applied == 0
    # Only the first two were attempted; the third never ran.
    assert len(out.results) == 2
    assert out.results[0].status == "ok"
    assert out.results[1].status == "failed"
    # On-disk schematic unchanged.
    assert p.read_text() == pre


async def test_continue_on_error_applies_survivors(tmp_path: Path) -> None:
    p = _make_sch(tmp_path)
    tool = SchComposeTool(config=_cfg())
    out = await tool.run(
        SchComposeInput(
            sch_path=p,
            continue_on_error=True,
            steps=[
                AddWireStep(op="add_wire", start_x=0, start_y=0, end_x=2.54, end_y=0),
                AddWireStep(op="add_wire", start_x=5.08, start_y=0, end_x=5.08, end_y=0),
                AddWireStep(op="add_wire", start_x=10, start_y=0, end_x=15, end_y=0),
            ],
        )
    )
    assert out.status == "partial"
    assert out.applied == 2
    assert len(out.results) == 3
    assert [r.status for r in out.results] == ["ok", "failed", "ok"]
    # File has the two surviving wires.
    text = p.read_text()
    assert text.count("(wire") == 2


async def test_dry_run_does_not_save(tmp_path: Path) -> None:
    p = _make_sch(tmp_path)
    pre = p.read_text()
    tool = SchComposeTool(config=_cfg())
    out = await tool.run(
        SchComposeInput(
            sch_path=p,
            dry_run=True,
            steps=[
                AddWireStep(op="add_wire", start_x=0, start_y=0, end_x=2.54, end_y=0),
                AddJunctionStep(op="add_junction", at_x=2.54, at_y=0),
            ],
        )
    )
    assert out.status == "dry_run"
    assert out.applied == 0
    assert all(r.status == "dry_run" for r in out.results)
    assert all(r.uuid is None for r in out.results)
    # On-disk unchanged.
    assert p.read_text() == pre


async def test_grid_snap_warning_per_step(tmp_path: Path) -> None:
    p = _make_sch(tmp_path)
    tool = SchComposeTool(config=_cfg())
    out = await tool.run(
        SchComposeInput(
            sch_path=p,
            steps=[
                # Off-grid coordinates — should snap and emit warning.
                AddWireStep(op="add_wire", start_x=1.0, start_y=1.0,
                            end_x=3.0, end_y=1.0),
            ],
        )
    )
    assert out.status == "ok"
    assert out.results[0].note is not None
    assert "snapped" in out.results[0].note.lower() or "grid" in out.results[0].note.lower()


async def test_sch_not_found(tmp_path: Path) -> None:
    p = tmp_path / "missing.kicad_sch"
    tool = SchComposeTool(config=_cfg())
    out = await tool.run(
        SchComposeInput(
            sch_path=p,
            steps=[AddWireStep(op="add_wire", start_x=0, start_y=0, end_x=2.54, end_y=0)],
        )
    )
    assert out.status == "sch_not_found"
    assert out.applied == 0


async def test_compose_classified_as_mutate() -> None:
    """Compose mutates the schematic; the classification matters for
    audit logging and dry-run defaults per ADR-0008."""
    from kimcp._types import ToolClass
    assert SchComposeTool.classification is ToolClass.MUTATE
    assert SchComposeTool.mutates is True
