"""Unit tests for pcb_drc_violations.

This tool wraps ``pcb_drc``. The kicad-cli stub machinery is tested
already in ``test_tool_pcb_drc.py`` — here we inject a fake drc tool
and focus on the filter + aggregation logic that's unique to this
layer.

Coverage:

* **flattening** — three buckets → one list with bucket tags preserved.
* **filters** — rule_ids, severity_floor (including 'info'),
  description_contains, buckets. Each alone + composed.
* **aggregates** — counts by rule_id / severity / bucket match the
  filtered row list.
* **include_items=False** — items arrays drop; counts unchanged.
* **sort order** — severity rank asc (error first), then rule_id alpha.
* **status propagation** — upstream ``pcb_not_found`` / ``cli_failed``
  / ``parse_failed`` pass through with note.
* **severity_floor='error' narrows the upstream call** — we don't ask
  pcb_drc for warnings if the caller only wanted errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp._types import Backend, ToolClass
from kimcp.tools.builtin.pcb_drc import (
    DrcItem,
    DrcViolation,
    PcbDrcInput,
    PcbDrcOutput,
    PcbDrcTool,
)
from kimcp.tools.builtin.pcb_drc_violations import (
    PcbDrcViolationsInput,
    PcbDrcViolationsTool,
)

# -- fake drc tool ---------------------------------------------------------


class _FakeDrcTool(PcbDrcTool):
    """Test double: records input, returns a pre-baked output.

    Inherits from ``PcbDrcTool`` so the ``_drc_tool`` type slot
    accepts it without casts. ``set_cli_backend`` is a no-op — the
    fake never touches the CLI.
    """

    def __init__(self, output: PcbDrcOutput) -> None:
        super().__init__()
        self._output = output
        self.calls: list[PcbDrcInput] = []

    async def run(self, input: PcbDrcInput) -> PcbDrcOutput:
        self.calls.append(input)
        return self._output


def _sample_drc_output() -> PcbDrcOutput:
    """A mixed DRC result with all three buckets + all three severities.

    Useful as a shared fixture for filter / aggregate tests. Layout:

    - ``violations``: clearance (error), silk_over_copper (warning),
      text_height (info).
    - ``unconnected_items``: unconnected_items (warning).
    - ``schematic_parity_issues``: extra_footprint (error).
    """
    return PcbDrcOutput(
        status="violations",
        pcb_path="/tmp/board.kicad_pcb",
        violations=[
            DrcViolation(
                rule_id="clearance",
                severity="error",
                description="Clearance violation between U1 pad 3 and track on F.Cu",
                items=[DrcItem(description="track", uuid="aaaa")],
            ),
            DrcViolation(
                rule_id="silk_over_copper",
                severity="warning",
                description="Silkscreen text overlaps pad on R7",
                items=[DrcItem(description="silk", uuid="bbbb")],
            ),
            DrcViolation(
                rule_id="text_height",
                severity="info",
                description="Text height below 0.8 mm on C12",
                items=[DrcItem(description="text", uuid="cccc")],
            ),
        ],
        unconnected_items=[
            DrcViolation(
                rule_id="unconnected_items",
                severity="warning",
                description="Net 'CLK' has unconnected pin U3.4",
                items=[DrcItem(description="pin", uuid="dddd")],
            ),
        ],
        schematic_parity_issues=[
            DrcViolation(
                rule_id="extra_footprint",
                severity="error",
                description="Footprint R99 on PCB has no matching symbol in schematic",
                items=[DrcItem(description="footprint", uuid="eeee")],
            ),
        ],
        total_count=5,
        coordinate_units="mm",
        kicad_version="9.0.1",
    )


def _make_tool(output: PcbDrcOutput | None = None) -> tuple[
    PcbDrcViolationsTool, _FakeDrcTool
]:
    fake = _FakeDrcTool(output or _sample_drc_output())
    tool = PcbDrcViolationsTool(drc_tool=fake)
    return tool, fake


def _touch_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (version 20240108) (generator test))\n", encoding="utf-8"
    )
    return pcb


# -- metadata --------------------------------------------------------------


def test_metadata() -> None:
    tool = PcbDrcViolationsTool()
    assert tool.name == "pcb_drc_violations"
    assert tool.classification == ToolClass.READ
    assert tool.preferred_backends == (Backend.CLI,)
    assert tool.required_backends == frozenset({Backend.CLI})


# -- happy paths: flattening + aggregation --------------------------------


@pytest.mark.asyncio
async def test_flattens_all_three_buckets(tmp_path: Path) -> None:
    """5 findings across 3 buckets collapse to a single 5-row list
    with bucket tags preserved. Default severity_floor='warning'
    keeps the info-level text_height out."""
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(PcbDrcViolationsInput(pcb_path=pcb))
    assert out.status == "violations"
    # warning floor drops the info-severity text_height → 4 rows.
    assert out.total_count == 4
    buckets = {row.bucket for row in out.rows}
    assert buckets == {"violations", "unconnected", "parity"}


@pytest.mark.asyncio
async def test_aggregates_match_filtered_rows(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(PcbDrcViolationsInput(pcb_path=pcb))
    # With default warning floor: 2 errors + 2 warnings.
    assert out.count_by_severity == {"error": 2, "warning": 2}
    assert out.count_by_bucket == {
        "violations": 2,
        "unconnected": 1,
        "parity": 1,
    }
    # Rule-id split: clearance, silk_over_copper, unconnected_items,
    # extra_footprint each appear once.
    assert out.count_by_rule_id == {
        "clearance": 1,
        "silk_over_copper": 1,
        "unconnected_items": 1,
        "extra_footprint": 1,
    }


@pytest.mark.asyncio
async def test_sort_order_is_severity_then_rule(tmp_path: Path) -> None:
    """Errors come first, then warnings; within each severity the
    rule_id is alphabetical. Makes the list predictable for the
    "top of the pile" triage view."""
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(PcbDrcViolationsInput(pcb_path=pcb))
    keys = [(row.severity, row.rule_id) for row in out.rows]
    assert keys == [
        ("error", "clearance"),
        ("error", "extra_footprint"),
        ("warning", "silk_over_copper"),
        ("warning", "unconnected_items"),
    ]


# -- filter: severity_floor -----------------------------------------------


@pytest.mark.asyncio
async def test_severity_floor_info_keeps_everything(tmp_path: Path) -> None:
    """Default floor is 'warning' — drops text_height (info). Flipping
    to 'info' surfaces it."""
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(pcb_path=pcb, severity_floor="info")
    )
    assert out.total_count == 5
    assert "info" in out.count_by_severity


@pytest.mark.asyncio
async def test_severity_floor_error_narrows_upstream_call(tmp_path: Path) -> None:
    """When the caller only wants errors, we should pass 'error' to
    pcb_drc too — no point asking the CLI for warnings we'll discard.
    Pin this so a future refactor doesn't over-request."""
    pcb = _touch_pcb(tmp_path)
    tool, fake = _make_tool()
    await tool.run(
        PcbDrcViolationsInput(pcb_path=pcb, severity_floor="error")
    )
    assert len(fake.calls) == 1
    assert fake.calls[0].severity_floor == "error"


@pytest.mark.asyncio
async def test_severity_floor_warning_asks_drc_for_warnings(tmp_path: Path) -> None:
    """Inverse of the narrowing check: 'warning' / 'info' inputs both
    forward 'warning' upstream so info findings survive the CLI's
    own filter and reach us for the local 'info' floor."""
    pcb = _touch_pcb(tmp_path)
    for local_floor in ("warning", "info"):
        tool, fake = _make_tool()
        await tool.run(
            PcbDrcViolationsInput(pcb_path=pcb, severity_floor=local_floor)  # type: ignore[arg-type]
        )
        assert fake.calls[0].severity_floor == "warning", (
            f"local_floor={local_floor!r} should forward 'warning' to pcb_drc"
        )


# -- filter: rule_ids -----------------------------------------------------


@pytest.mark.asyncio
async def test_rule_ids_filter(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(pcb_path=pcb, rule_ids=["clearance"])
    )
    assert out.total_count == 1
    assert out.rows[0].rule_id == "clearance"


@pytest.mark.asyncio
async def test_rule_ids_filter_multi(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(
            pcb_path=pcb, rule_ids=["clearance", "extra_footprint"]
        )
    )
    rule_ids = sorted(row.rule_id for row in out.rows)
    assert rule_ids == ["clearance", "extra_footprint"]


# -- filter: buckets ------------------------------------------------------


@pytest.mark.asyncio
async def test_buckets_filter(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(pcb_path=pcb, buckets=["parity"])
    )
    assert out.total_count == 1
    assert out.rows[0].bucket == "parity"
    assert out.rows[0].rule_id == "extra_footprint"


# -- filter: description_contains ----------------------------------------


@pytest.mark.asyncio
async def test_description_contains_filter(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(pcb_path=pcb, description_contains="F.Cu")
    )
    # Only the clearance finding mentions F.Cu.
    assert [row.rule_id for row in out.rows] == ["clearance"]


@pytest.mark.asyncio
async def test_description_contains_case_sensitive(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(pcb_path=pcb, description_contains="f.cu")
    )
    assert out.rows == []


# -- filter: compose ------------------------------------------------------


@pytest.mark.asyncio
async def test_all_filters_compose_with_and(tmp_path: Path) -> None:
    """severity='error' + bucket='violations' + rule_id='clearance' →
    one row. Proves the filter pipeline composes rather than
    short-circuiting on the first match."""
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(
            pcb_path=pcb,
            severity_floor="error",
            buckets=["violations"],
            rule_ids=["clearance"],
        )
    )
    assert out.total_count == 1
    assert out.rows[0].rule_id == "clearance"
    assert out.rows[0].bucket == "violations"


# -- include_items --------------------------------------------------------


@pytest.mark.asyncio
async def test_include_items_false_drops_item_arrays(tmp_path: Path) -> None:
    """Counts-only mode: rows present, items empty. Shrinks the
    payload on boards with large item arrays."""
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(pcb_path=pcb, include_items=False)
    )
    assert out.total_count == 4
    for row in out.rows:
        assert row.items == []
    # But the rule_id / severity / bucket tags still populate.
    assert out.count_by_rule_id  # non-empty


@pytest.mark.asyncio
async def test_include_items_true_keeps_items(tmp_path: Path) -> None:
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(pcb_path=pcb, include_items=True)
    )
    clearance = next(r for r in out.rows if r.rule_id == "clearance")
    assert len(clearance.items) == 1
    assert clearance.items[0].uuid == "aaaa"


# -- status propagation ---------------------------------------------------


@pytest.mark.asyncio
async def test_ok_when_filtered_empty(tmp_path: Path) -> None:
    """Filters empty out the findings → status flips to 'ok'. Callers
    reading the status alone would see 'clean' even though the board
    had findings — the counts tell the honest story, and filtering-
    to-ok is the caller's explicit intent."""
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool()
    out = await tool.run(
        PcbDrcViolationsInput(pcb_path=pcb, rule_ids=["no_such_rule"])
    )
    assert out.status == "ok"
    assert out.total_count == 0


@pytest.mark.asyncio
async def test_upstream_pcb_not_found_propagates(tmp_path: Path) -> None:
    fake_out = PcbDrcOutput(
        status="pcb_not_found",
        pcb_path=None,
        note="no such file: /bogus",
    )
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool(fake_out)
    out = await tool.run(PcbDrcViolationsInput(pcb_path=pcb))
    assert out.status == "pcb_not_found"
    assert out.note == "no such file: /bogus"
    assert out.rows == []


@pytest.mark.asyncio
async def test_upstream_cli_failed_propagates(tmp_path: Path) -> None:
    fake_out = PcbDrcOutput(
        status="cli_failed",
        pcb_path="/tmp/board.kicad_pcb",
        note="kicad-cli timed out after 120s",
    )
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool(fake_out)
    out = await tool.run(PcbDrcViolationsInput(pcb_path=pcb))
    assert out.status == "cli_failed"
    assert "timed out" in (out.note or "")


@pytest.mark.asyncio
async def test_upstream_parse_failed_propagates(tmp_path: Path) -> None:
    fake_out = PcbDrcOutput(
        status="parse_failed",
        pcb_path="/tmp/board.kicad_pcb",
        note="DRC JSON was not parseable: ...",
    )
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool(fake_out)
    out = await tool.run(PcbDrcViolationsInput(pcb_path=pcb))
    assert out.status == "parse_failed"
    assert out.rows == []


# -- forwarded options ----------------------------------------------------


@pytest.mark.asyncio
async def test_schematic_parity_and_units_forward(tmp_path: Path) -> None:
    """schematic_parity and units pass straight through to pcb_drc."""
    pcb = _touch_pcb(tmp_path)
    tool, fake = _make_tool()
    await tool.run(
        PcbDrcViolationsInput(
            pcb_path=pcb, schematic_parity=True, units="in"
        )
    )
    call = fake.calls[0]
    assert call.schematic_parity is True
    assert call.units == "in"


# -- empty-board happy path -----------------------------------------------


@pytest.mark.asyncio
async def test_clean_board_returns_ok_with_empty_aggregates(tmp_path: Path) -> None:
    """No findings at all → status='ok', aggregates are empty dicts
    (not missing / not None). Keeps the JSON shape predictable for
    downstream consumers."""
    clean = PcbDrcOutput(
        status="ok",
        pcb_path="/tmp/board.kicad_pcb",
        violations=[],
        unconnected_items=[],
        schematic_parity_issues=[],
        total_count=0,
        coordinate_units="mm",
        kicad_version="9.0.1",
    )
    pcb = _touch_pcb(tmp_path)
    tool, _ = _make_tool(clean)
    out = await tool.run(PcbDrcViolationsInput(pcb_path=pcb))
    assert out.status == "ok"
    assert out.rows == []
    assert out.count_by_rule_id == {}
    assert out.count_by_severity == {}
    assert out.count_by_bucket == {}
