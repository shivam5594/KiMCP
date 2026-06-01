"""Regression tests for the grid-snap guardrail (``safety.grid_snap_mm``).

Two layers of coverage:

1. **Utility-level** — ``snap_coord``, ``snap_moved``, ``apply_grid_snap``
   in ``kimcp.tools.builtin._sexpr_build`` — the three helpers every
   mutating tool calls. Pin the corner cases (zero angle, negative
   coords, opt-out via None, NaN/inf pass-through, epsilon-tolerant
   no-op).

2. **Tool-level** — every ``sch_add_*`` tool that accepts coordinates
   snaps them when ``safety.grid_snap_mm`` is set and emits a single
   ``meta.warnings`` entry. This locks the wire-in across all seven
   tools — future regressions where someone adds a new mutating tool
   without plumbing the snap through will fail at least one of these
   cases.

Design notes:
* We use a small 2.54 mm fixture grid (KiCAD's native eeschema grid)
  and pick inputs slightly off it (``0.7`` mm, ``10.05`` mm) that snap
  to cleanly-readable values. This makes the assertion failures
  diagnosable at a glance.
* Each tool test uses minimal inline fixtures rather than shared
  helpers — the per-tool fixtures already live in each tool's own
  ``test_tool_*.py`` file; duplicating them here inflates the file
  for no benefit. We only pin the snap behavior here.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kimcp.config import load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom
from kimcp.tools.builtin._sexpr_build import (
    apply_grid_snap,
    snap_coord,
    snap_moved,
)
from kimcp.tools.builtin.sch_add_junction import (
    SchAddJunctionInput,
    SchAddJunctionTool,
)
from kimcp.tools.builtin.sch_add_label import (
    SchAddLabelInput,
    SchAddLabelTool,
)
from kimcp.tools.builtin.sch_add_no_connect import (
    SchAddNoConnectInput,
    SchAddNoConnectTool,
)
from kimcp.tools.builtin.sch_add_power import (
    SchAddPowerInput,
    SchAddPowerTool,
)
from kimcp.tools.builtin.sch_add_sheet import (
    SchAddSheetInput,
    SchAddSheetTool,
)
from kimcp.tools.builtin.sch_add_symbol import (
    SchAddSymbolInput,
    SchAddSymbolTool,
)
from kimcp.tools.builtin.sch_add_wire import SchAddWireInput, SchAddWireTool

_SCH_EMPTY = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols))
"""

_SCH_WITH_R_SMALL = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "deadbeef-dead-beef-dead-beefdeadbeef")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "Device:R_Small"
\t\t\t(exclude_from_sim no) (in_bom yes) (on_board yes)
\t\t\t(property "Reference" "R" (at 2.032 0 90) (effects (font (size 1.27 1.27))))
\t\t\t(property "Value" "R_Small" (at 0 0 90) (effects (font (size 1.27 1.27))))
\t\t\t(symbol "R_Small_1_1"
\t\t\t\t(pin passive line (at 0 2.54 270) (length 0.508)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))))))
"""


def _cfg_snap_on(tmp_path: Path, grid_mm: float = 2.54):
    """Config with grid snap enabled at ``grid_mm`` (default 2.54 mm)."""
    return load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
        session_overrides={
            "safety": {"snapshot_mode": "off", "grid_snap_mm": grid_mm}
        },
    )


def _write_sch(tmp_path: Path, body: str = _SCH_EMPTY) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(body, encoding="utf-8")
    return sch


# -- utility layer ---------------------------------------------------------


class TestSnapCoord:
    def test_already_on_grid_is_identity(self) -> None:
        """A coordinate that's already a multiple of grid must pass through
        bit-exact — otherwise ``round() * grid_mm`` FP noise would churn
        on-disk bytes every round-trip."""
        assert snap_coord(5.08, 2.54) == 5.08
        assert snap_coord(0.0, 2.54) == 0.0
        assert snap_coord(-12.7, 2.54) == -12.7

    def test_rounds_to_nearest_tick(self) -> None:
        # 10.05 is closer to 10.16 (= 4*2.54) than to 7.62 (= 3*2.54).
        assert snap_coord(10.05, 2.54) == pytest.approx(10.16)

    def test_negative_coord_rounds_symmetrically(self) -> None:
        assert snap_coord(-10.05, 2.54) == pytest.approx(-10.16)

    def test_none_opts_out(self) -> None:
        """None grid is the documented opt-out — return input verbatim,
        even for wildly off-grid values."""
        assert snap_coord(123.4567, None) == 123.4567
        assert snap_coord(-0.0001, None) == -0.0001

    def test_nan_and_inf_pass_through(self) -> None:
        """Degenerate FP inputs pass through so validation fires at a more
        specific layer (pydantic, KiCAD ERC) rather than being masked by
        the snap producing a silent zero or raising a confusing error."""
        assert math.isnan(snap_coord(math.nan, 2.54))
        assert math.isinf(snap_coord(math.inf, 2.54))
        assert math.isinf(snap_coord(-math.inf, 2.54))


class TestSnapMoved:
    def test_identical_values_not_moved(self) -> None:
        assert snap_moved(5.08, 5.08) is False

    def test_large_delta_moved(self) -> None:
        assert snap_moved(10.05, 10.16) is True

    def test_sub_epsilon_not_moved(self) -> None:
        """Bit-level FP drift (e.g. 5.08 vs 5.0800000000000005) mustn't
        trigger a warning — callers would see noise warnings on every
        call with on-grid inputs."""
        assert snap_moved(5.08, 5.08 + 1e-10) is False


class TestApplyGridSnap:
    def test_none_grid_passes_through(self) -> None:
        snapped, warning = apply_grid_snap({"x": 123.456, "y": -7.89}, None)
        assert snapped == {"x": 123.456, "y": -7.89}
        assert warning is None

    def test_on_grid_inputs_no_warning(self) -> None:
        snapped, warning = apply_grid_snap(
            {"x": 5.08, "y": 0.0, "z": -12.7}, 2.54
        )
        assert snapped == {"x": 5.08, "y": 0.0, "z": -12.7}
        assert warning is None

    def test_off_grid_snaps_and_warns(self) -> None:
        snapped, warning = apply_grid_snap({"at_x": 10.05, "at_y": 20.1}, 2.54)
        assert snapped["at_x"] == pytest.approx(10.16)
        assert snapped["at_y"] == pytest.approx(20.32)
        assert warning is not None
        # Warning must name each moved field so debugging is tractable.
        assert "at_x" in warning and "at_y" in warning
        # Warning must also name the config knob so operators can audit.
        assert "safety.grid_snap_mm" in warning

    def test_partial_off_grid_warns_only_for_moved(self) -> None:
        """One on-grid, one off-grid → warning mentions only the moved
        field. Prevents the warning from implying both moved when only
        one did."""
        _, warning = apply_grid_snap({"on": 5.08, "off": 10.05}, 2.54)
        assert warning is not None
        assert "off" in warning
        assert "on " not in warning  # space-suffix to rule out "on" in a longer token


# -- tool layer: snap is wired in every mutating tool --------------------


@pytest.mark.asyncio
async def test_junction_snaps_and_warns(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddJunctionTool()
    tool.set_config(_cfg_snap_on(tmp_path))
    out = await tool.run(SchAddJunctionInput(sch_path=sch, at_x=10.05, at_y=20.1))
    assert out.status == "ok"
    assert len(out.meta.warnings) == 1

    # Verify on-disk coordinate landed on grid.
    doc = SexprDocument.from_path(sch)
    junction = doc.root.find("junction")
    assert junction is not None
    at = junction.find("at")
    assert at is not None
    # 10.05 → 10.16 → renders as "10.16"; 20.1 → 20.32 → "20.32"
    assert isinstance(at.items[1], SAtom)
    assert isinstance(at.items[2], SAtom)
    # fmt_mm strips trailing zeros on integers; these are fractional
    # so the float form renders. Just assert closeness via float parse.
    assert float(at.items[1].text) == pytest.approx(10.16)
    assert float(at.items[2].text) == pytest.approx(20.32)


@pytest.mark.asyncio
async def test_no_connect_snaps_and_warns(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddNoConnectTool()
    tool.set_config(_cfg_snap_on(tmp_path))
    out = await tool.run(SchAddNoConnectInput(sch_path=sch, at_x=10.05, at_y=20.1))
    assert out.status == "ok"
    assert len(out.meta.warnings) == 1


@pytest.mark.asyncio
async def test_wire_snaps_and_warns(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddWireTool()
    tool.set_config(_cfg_snap_on(tmp_path))
    out = await tool.run(
        SchAddWireInput(
            sch_path=sch,
            start_x=10.05,
            start_y=20.1,
            end_x=20.3,
            end_y=30.5,
        )
    )
    assert out.status == "ok"
    assert len(out.meta.warnings) == 1
    # All four coord fields should be named in the warning.
    warning = out.meta.warnings[0]
    for field in ("start_x", "start_y", "end_x", "end_y"):
        assert field in warning


@pytest.mark.asyncio
async def test_wire_snap_collapse_reports_invalid_geometry(tmp_path: Path) -> None:
    """Two close endpoints that snap to the same point must be rejected
    AFTER snap, not written as a zero-length wire."""
    sch = _write_sch(tmp_path)
    tool = SchAddWireTool()
    tool.set_config(_cfg_snap_on(tmp_path))
    out = await tool.run(
        SchAddWireInput(
            sch_path=sch, start_x=10.05, start_y=10.05, end_x=10.1, end_y=10.1
        )
    )
    # Both pre-snap points are distinct; post-snap they collapse to (10.16, 10.16).
    assert out.status == "invalid_geometry"
    # The snap warning still fires so the caller sees what happened.
    assert len(out.meta.warnings) == 1


@pytest.mark.asyncio
async def test_label_snaps_and_warns(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddLabelTool()
    tool.set_config(_cfg_snap_on(tmp_path))
    out = await tool.run(
        SchAddLabelInput(sch_path=sch, text="NET_A", at_x=10.05, at_y=20.1, kind="local")
    )
    assert out.status == "ok"
    assert len(out.meta.warnings) == 1


@pytest.mark.asyncio
async def test_symbol_snaps_and_warns(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path, _SCH_WITH_R_SMALL)
    tool = SchAddSymbolTool(_cfg_snap_on(tmp_path))
    out = await tool.run(
        SchAddSymbolInput(
            sch_path=sch,
            lib_id="Device:R_Small",
            reference="R1",
            value="10k",
            at_x=10.05,
            at_y=20.1,
        )
    )
    assert out.status == "ok"
    assert len(out.meta.warnings) == 1


@pytest.mark.asyncio
async def test_power_snaps_and_warns(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchAddPowerTool(_cfg_snap_on(tmp_path))
    out = await tool.run(
        SchAddPowerInput(sch_path=sch, net_name="GND", at_x=10.05, at_y=20.1)
    )
    assert out.status == "ok"
    # Power may also emit an embed-fallback warning if KiCAD isn't
    # installed — filter for the snap-specific one.
    snap_warnings = [w for w in out.meta.warnings if "grid_snap_mm" in w]
    assert len(snap_warnings) == 1


@pytest.mark.asyncio
async def test_sheet_snaps_position_and_size(tmp_path: Path) -> None:
    """sch_add_sheet snaps all four fields: at_x, at_y, size_w, size_h.
    The outline must land fully on grid — a half-tick gap on the right
    edge is worse than one on the left."""
    sch = _write_sch(tmp_path)
    tool = SchAddSheetTool(_cfg_snap_on(tmp_path))
    out = await tool.run(
        SchAddSheetInput(
            sch_path=sch,
            sheet_name="Power",
            sheet_file="sheets/power.kicad_sch",
            at_x=10.05,
            at_y=20.1,
            size_w=60.1,
            size_h=40.1,
        )
    )
    assert out.status == "ok"
    # Single warning listing all four moved fields.
    snap_warnings = [w for w in out.meta.warnings if "grid_snap_mm" in w]
    assert len(snap_warnings) == 1
    for field in ("at_x", "at_y", "size_w", "size_h"):
        assert field in snap_warnings[0]


@pytest.mark.asyncio
async def test_on_grid_inputs_emit_no_snap_warning(tmp_path: Path) -> None:
    """Sanity: a caller passing pre-snapped coords should see no
    snap warning — the guardrail is a no-op."""
    sch = _write_sch(tmp_path)
    tool = SchAddJunctionTool()
    tool.set_config(_cfg_snap_on(tmp_path))
    out = await tool.run(SchAddJunctionInput(sch_path=sch, at_x=5.08, at_y=12.7))
    assert out.status == "ok"
    snap_warnings = [w for w in out.meta.warnings if "grid_snap_mm" in w]
    assert snap_warnings == []


@pytest.mark.asyncio
async def test_opt_out_via_none_preserves_subgrid_coords(tmp_path: Path) -> None:
    """A caller who sets ``safety.grid_snap_mm = null`` gets exact
    pass-through — no snap, no warning."""
    sch = _write_sch(tmp_path)
    tool = SchAddJunctionTool()
    tool.set_config(
        load_config(
            user_global=tmp_path / "__u.toml",
            project_local=tmp_path / "__p.toml",
            session_overrides={
                "safety": {"snapshot_mode": "off", "grid_snap_mm": None}
            },
        )
    )
    out = await tool.run(SchAddJunctionInput(sch_path=sch, at_x=10.05, at_y=20.1))
    assert out.status == "ok"
    assert out.meta.warnings == []

    # On-disk coords must match the inputs verbatim.
    doc = SexprDocument.from_path(sch)
    junction = doc.root.find("junction")
    assert junction is not None
    at = junction.find("at")
    assert at is not None
    assert isinstance(at.items[1], SAtom) and float(at.items[1].text) == 10.05
    assert isinstance(at.items[2], SAtom) and float(at.items[2].text) == 20.1


# -- config-level validation ----------------------------------------------


def test_config_rejects_zero_grid_snap(tmp_path: Path) -> None:
    """Zero or negative grid_snap_mm must fail at Config load rather than
    divide-by-zero downstream."""
    with pytest.raises(Exception) as exc_info:
        load_config(
            user_global=tmp_path / "__u.toml",
            project_local=tmp_path / "__p.toml",
            session_overrides={"safety": {"grid_snap_mm": 0}},
        )
    assert "grid_snap_mm" in str(exc_info.value)


def test_config_rejects_negative_grid_snap(tmp_path: Path) -> None:
    with pytest.raises(Exception) as exc_info:
        load_config(
            user_global=tmp_path / "__u.toml",
            project_local=tmp_path / "__p.toml",
            session_overrides={"safety": {"grid_snap_mm": -2.54}},
        )
    assert "grid_snap_mm" in str(exc_info.value)


def test_config_default_is_2_54mm(tmp_path: Path) -> None:
    """Production default must be KiCAD's native eeschema grid (100 mil =
    2.54 mm) — flipping this without ceremony would silently break every
    caller that relied on on-grid behavior."""
    cfg = load_config(
        user_global=tmp_path / "__u.toml",
        project_local=tmp_path / "__p.toml",
    )
    assert cfg.safety.grid_snap_mm == 2.54
