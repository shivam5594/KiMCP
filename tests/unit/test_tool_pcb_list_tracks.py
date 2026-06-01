"""Unit tests for pcb_list_tracks.

Covers the three routing-kind branches (segment / via / arc) plus the
load-bearing mechanics:

* **net resolution** — the board stores net numbers on tracks and net
  names in separate ``(net N "<name>")`` declarations. A regression
  where the resolver map isn't built would leave ``net_name=""`` for
  every named net — verify explicitly.
* **layer filter for vias** — vias live on two (or more) layers; a
  ``layer="F.Cu"`` filter must include a via whose span contains
  F.Cu. Segments/arcs match on their single layer.
* **kind discriminator** — ``TrackItem.kind`` gates which geometry
  fields are meaningful. Pin that arcs get mid_x/mid_y, vias get
  at_x/at_y, segments get start/end.

Deliberately simple fixture: two segments (one top, one bottom),
one through-via, one arc, and one skipped graphic (gr_line) to prove
we don't mistake silk/geometry for routing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.pcb_list_tracks import (
    PcbListTracksInput,
    PcbListTracksTool,
)

# Minimal .kicad_pcb fixture. Three nets (0=noname, 1=VCC, 2=GND) and
# four routed copper items:
#   - segment on F.Cu, net 1 (VCC), wide track
#   - segment on B.Cu, net 2 (GND), narrow track
#   - through-via F.Cu↔B.Cu, net 1 (VCC)
#   - arc on F.Cu, net 2 (GND)
# Plus one (gr_line ...) to verify graphic items are NOT surfaced.
_PCB = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers)
\t(net 0 "")
\t(net 1 "VCC")
\t(net 2 "GND")
\t(segment
\t\t(start 100 50)
\t\t(end 110 50)
\t\t(width 0.5)
\t\t(layer "F.Cu")
\t\t(net 1)
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")
\t)
\t(segment
\t\t(start 120 60)
\t\t(end 120 80)
\t\t(width 0.2)
\t\t(layer "B.Cu")
\t\t(net 2)
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000002")
\t)
\t(via
\t\t(at 115 55)
\t\t(size 0.8)
\t\t(drill 0.4)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net 1)
\t\t(uuid "bbbbbbbb-0000-0000-0000-000000000001")
\t)
\t(arc
\t\t(start 130 70)
\t\t(mid 135 72)
\t\t(end 140 70)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net 2)
\t\t(uuid "cccccccc-0000-0000-0000-000000000001")
\t)
\t(gr_line
\t\t(start 0 0)
\t\t(end 50 0)
\t\t(layer "F.SilkS")
\t\t(width 0.15)
\t\t(uuid "dddddddd-0000-0000-0000-000000000001")
\t)
)
"""


def _write(tmp_path: Path, body: str = _PCB) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(body, encoding="utf-8")
    return pcb


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = PcbListTracksTool()
    assert tool.name == "pcb_list_tracks"
    assert tool.classification == ToolClass.READ
    assert tool.mutates is False
    assert tool.preferred_backends == (Backend.SEXPR,)
    assert tool.required_backends == frozenset({Backend.SEXPR})


# -- happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_all_routed_copper(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb))
    assert out.status == "ok"
    # 2 segments + 1 via + 1 arc = 4; gr_line is NOT counted.
    assert out.total == 4
    kinds = sorted(t.kind for t in out.tracks)
    assert kinds == ["arc", "segment", "segment", "via"]


@pytest.mark.asyncio
async def test_graphic_items_are_ignored(tmp_path: Path) -> None:
    """(gr_line ...) on F.SilkS is silk, not routing. Must not appear
    — if it does, the tool is conflating geometry with copper and a
    user's 'list VCC tracks' query would start returning silkscreen."""
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb))
    # gr_line uuid starts with "dddddddd" — pin the absence.
    uuids = {t.uuid for t in out.tracks}
    assert not any(u.startswith("dddddddd") for u in uuids)


@pytest.mark.asyncio
async def test_net_names_are_resolved(tmp_path: Path) -> None:
    """The load-bearing bit: tracks carry integer net numbers, names
    live in separate ``(net N "name")`` decls. A broken resolver
    leaves every ``net_name`` empty."""
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb))
    by_uuid = {t.uuid: t for t in out.tracks}
    # The F.Cu segment is on net 1 → "VCC".
    seg_vcc = by_uuid["aaaaaaaa-0000-0000-0000-000000000001"]
    assert seg_vcc.net_num == 1
    assert seg_vcc.net_name == "VCC"
    # The B.Cu segment is on net 2 → "GND".
    seg_gnd = by_uuid["aaaaaaaa-0000-0000-0000-000000000002"]
    assert seg_gnd.net_name == "GND"
    # The through-via is on VCC.
    via = by_uuid["bbbbbbbb-0000-0000-0000-000000000001"]
    assert via.net_name == "VCC"


@pytest.mark.asyncio
async def test_segment_fields_are_populated(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb))
    seg = next(
        t for t in out.tracks if t.uuid == "aaaaaaaa-0000-0000-0000-000000000001"
    )
    assert seg.kind == "segment"
    assert seg.start_x == 100.0
    assert seg.start_y == 50.0
    assert seg.end_x == 110.0
    assert seg.end_y == 50.0
    assert seg.layer == "F.Cu"
    assert seg.width == 0.5
    # Via / arc fields stay at defaults.
    assert seg.at_x == 0.0
    assert seg.mid_x == 0.0
    assert seg.drill == 0.0
    assert seg.layers == []


@pytest.mark.asyncio
async def test_via_fields_are_populated(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb))
    via = next(t for t in out.tracks if t.kind == "via")
    assert via.at_x == 115.0
    assert via.at_y == 55.0
    assert via.layers == ["F.Cu", "B.Cu"]
    # width holds the pad size for vias by convention.
    assert via.width == 0.8
    assert via.drill == 0.4
    # segment/arc fields stay at defaults.
    assert via.start_x == 0.0
    assert via.end_x == 0.0
    assert via.layer == ""


@pytest.mark.asyncio
async def test_arc_fields_include_midpoint(tmp_path: Path) -> None:
    """An arc's midpoint is what distinguishes it from a straight
    segment — without it the geometry is ambiguous. Pin all three
    points."""
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb))
    arc = next(t for t in out.tracks if t.kind == "arc")
    assert arc.start_x == 130.0
    assert arc.start_y == 70.0
    assert arc.mid_x == 135.0
    assert arc.mid_y == 72.0
    assert arc.end_x == 140.0
    assert arc.end_y == 70.0
    assert arc.layer == "F.Cu"
    assert arc.width == 0.25


# -- filters: kinds -------------------------------------------------------


@pytest.mark.asyncio
async def test_kinds_filter_segment_only(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb, kinds=["segment"]))
    assert out.status == "ok"
    assert {t.kind for t in out.tracks} == {"segment"}
    assert out.total == 2


@pytest.mark.asyncio
async def test_kinds_filter_via_and_arc(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb, kinds=["via", "arc"]))
    assert {t.kind for t in out.tracks} == {"via", "arc"}
    assert out.total == 2


# -- filters: layer -------------------------------------------------------


@pytest.mark.asyncio
async def test_layer_filter_top_matches_segment_arc_and_via(tmp_path: Path) -> None:
    """A through-via lives on both F.Cu and B.Cu — a layer="top" filter
    must include it. That's the whole point of the layer-span match
    for vias."""
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb, layer="top"))
    # F.Cu segment + F.Cu arc + through-via (spans F.Cu).
    kinds = sorted(t.kind for t in out.tracks)
    assert kinds == ["arc", "segment", "via"]


@pytest.mark.asyncio
async def test_layer_filter_bottom_matches_segment_and_via(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb, layer="bottom"))
    # B.Cu segment + through-via. Arc is on F.Cu only.
    kinds = sorted(t.kind for t in out.tracks)
    assert kinds == ["segment", "via"]


@pytest.mark.asyncio
async def test_layer_filter_inner_copper_returns_empty(tmp_path: Path) -> None:
    """A filter for a copper layer that no track sits on yields empty —
    not an error."""
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb, layer="In1.Cu"))
    assert out.status == "ok"
    assert out.tracks == []


# -- filters: net ---------------------------------------------------------


@pytest.mark.asyncio
async def test_net_contains_filter(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb, net_contains="VCC"))
    # Both the F.Cu segment and the through-via are on VCC.
    assert out.total == 2
    kinds = sorted(t.kind for t in out.tracks)
    assert kinds == ["segment", "via"]


@pytest.mark.asyncio
async def test_net_contains_filter_case_sensitive(tmp_path: Path) -> None:
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb, net_contains="vcc"))
    assert out.tracks == []


@pytest.mark.asyncio
async def test_filters_compose_with_and(tmp_path: Path) -> None:
    """kinds=['segment'] + layer='top' + net_contains='VCC' should hit
    exactly the F.Cu VCC segment."""
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(
        PcbListTracksInput(
            pcb_path=pcb,
            kinds=["segment"],
            layer="top",
            net_contains="VCC",
        )
    )
    assert out.total == 1
    t = out.tracks[0]
    assert t.kind == "segment"
    assert t.layer == "F.Cu"
    assert t.net_name == "VCC"


# -- error paths ----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file(tmp_path: Path) -> None:
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=tmp_path / "nope.kicad_pcb"))
    assert out.status == "pcb_not_found"
    assert out.tracks == []


@pytest.mark.asyncio
async def test_wrong_suffix(tmp_path: Path) -> None:
    f = tmp_path / "wrong.txt"
    f.write_text(_PCB, encoding="utf-8")
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=f))
    assert out.status == "pcb_not_found"
    assert out.tracks == []


@pytest.mark.asyncio
async def test_parse_failure(tmp_path: Path) -> None:
    f = tmp_path / "broken.kicad_pcb"
    f.write_text("(kicad_pcb (unterminated ", encoding="utf-8")
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=f))
    assert out.status == "parse_failed"
    assert out.tracks == []


@pytest.mark.asyncio
async def test_wrong_top_head(tmp_path: Path) -> None:
    f = tmp_path / "sch_like.kicad_pcb"
    f.write_text(
        '(kicad_sch (version 20240108) (generator "eeschema"))', encoding="utf-8"
    )
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=f))
    assert out.status == "invalid_schema"
    assert out.tracks == []


# -- edge cases -----------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_board(tmp_path: Path) -> None:
    body = (
        '(kicad_pcb (version 20240108) (generator "pcbnew") (paper "A4") (layers))'
    )
    f = tmp_path / "empty.kicad_pcb"
    f.write_text(body, encoding="utf-8")
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=f))
    assert out.status == "ok"
    assert out.tracks == []


@pytest.mark.asyncio
async def test_track_without_uuid_is_skipped(tmp_path: Path) -> None:
    """Degenerate fixture: a segment missing its uuid is silently
    dropped rather than aborting the whole listing. Hand-edited
    boards sometimes have these; we don't want a single malformed
    node to nuke the query."""
    body = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers)
\t(net 0 "")
\t(segment
\t\t(start 100 50)
\t\t(end 110 50)
\t\t(width 0.5)
\t\t(layer "F.Cu")
\t\t(net 0)
\t)
)
"""
    f = tmp_path / "no_uuid.kicad_pcb"
    f.write_text(body, encoding="utf-8")
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=f))
    assert out.status == "ok"
    assert out.tracks == []


@pytest.mark.asyncio
async def test_net_zero_surfaces_empty_name(tmp_path: Path) -> None:
    """A track on net 0 is the 'no net assigned' sentinel. ``net_name``
    must be the empty string, not ``"<unnamed>"`` or similar invented
    placeholder."""
    body = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers)
\t(net 0 "")
\t(segment
\t\t(start 100 50)
\t\t(end 110 50)
\t\t(width 0.5)
\t\t(layer "F.Cu")
\t\t(net 0)
\t\t(uuid "ffffffff-0000-0000-0000-000000000001")
\t)
)
"""
    f = tmp_path / "net0.kicad_pcb"
    f.write_text(body, encoding="utf-8")
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=f))
    assert out.status == "ok"
    assert len(out.tracks) == 1
    assert out.tracks[0].net_num == 0
    assert out.tracks[0].net_name == ""


@pytest.mark.asyncio
async def test_sort_order_is_kind_then_uuid(tmp_path: Path) -> None:
    """Deterministic ordering: arcs first, then segments, then vias.
    Within each kind, sort by uuid."""
    pcb = _write(tmp_path)
    tool = PcbListTracksTool()
    out = await tool.run(PcbListTracksInput(pcb_path=pcb))
    kinds = [t.kind for t in out.tracks]
    assert kinds == ["arc", "segment", "segment", "via"]
    # Within segments, uuids must be ascending.
    seg_uuids = [t.uuid for t in out.tracks if t.kind == "segment"]
    assert seg_uuids == sorted(seg_uuids)
