"""Unit tests for the ``sch_set_title_block`` built-in tool (M12).

Unlike the CLI-shelling sibling tools (pcb_drc / sch_erc / sch_export_*),
this one runs entirely through the SEXPR backend — no kicad-cli stub, no
IPC socket. The tests exercise a real SexprDocument read → mutate →
round-trip serialize → atomic write pipeline, which is the pattern every
subsequent schematic mutation tool will follow.

Coverage matrix:

* Preflight: sch_not_found (missing / directory / wrong suffix),
  invalid_schema (top_head != kicad_sch), parse_failed (SexprParseError).
* No-op: no fields passed, values already match.
* Dry-run: correct fields_changed, file untouched, no snapshot taken.
* Simple fields: add / update / clear, round-trip persistence.
* Comment slots: add / update / multiple slots, slot-atom preservation.
* title_block synthesis: missing → inserted at canonical position.
* Snapshot plumbing: ``snapshot_mode='off'`` → ``meta.snapshot_ref
  == 'disabled'``; ``'copy'`` → ``meta.snapshot_ref`` points at a dir.
* Write failure: monkeypatched serialize forces a round-trip RuntimeError
  → ``write_failed`` status, file contents untouched.
* DI shape: ``set_config`` wires the config through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimcp.config import Config, load_config
from kimcp.sexpr.document import SexprDocument
from kimcp.sexpr.nodes import SAtom, SList
from kimcp.tools.builtin.sch_set_title_block import (
    SchSetTitleBlockInput,
    SchSetTitleBlockOutput,
    SchSetTitleBlockTool,
)

# -- helpers ---------------------------------------------------------------


def _atom_at(node: SList, idx: int) -> SAtom:
    """Narrow ``node.items[idx]`` to SAtom for mypy and test clarity."""
    item = node.items[idx]
    assert isinstance(item, SAtom), f"expected SAtom at index {idx}, got {type(item).__name__}"
    return item


def _field_text(title_block: SList, head: str) -> str:
    """Return the quoted value of ``(head "...")`` under title_block."""
    node = title_block.find(head)
    assert node is not None, f"missing title_block field: {head}"
    return _atom_at(node, 1).text


def _comments_by_slot(title_block: SList) -> dict[int, str]:
    """Walk ``(comment N "text")`` children and return {slot: text}."""
    out: dict[int, str] = {}
    for child in title_block.find_all("comment"):
        slot_atom = _atom_at(child, 1)
        text_atom = _atom_at(child, 2)
        out[int(slot_atom.text)] = text_atom.text
    return out


_EMPTY_SCH = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(title_block
\t\t(title "Old Title")
\t\t(date "2024-01-01")
\t\t(rev "A")
\t\t(company "Acme")
\t\t(comment 1 "one")
\t\t(comment 2 "two"))
\t(lib_symbols))
"""

_EMPTY_SCH_NO_TITLE_BLOCK = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols))
"""

_PCB_NOT_SCH = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew"))
"""


def _write_sch(tmp_path: Path, content: str = _EMPTY_SCH) -> Path:
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(content, encoding="utf-8")
    return sch


def _config_with_snapshot(mode: str) -> Config:
    """Return a Config with safety.snapshot_mode set to `mode`.

    We build it through ``load_config`` with ``session_overrides`` so
    the full validation path runs, not a hand-rolled Config.
    """
    nope = Path("/tmp/__nope_never_exists__")
    return load_config(
        user_global=nope,
        project_local=nope,
        session_overrides={"safety": {"snapshot_mode": mode}},
    )


async def _run(tool: SchSetTitleBlockTool, **kwargs: object) -> SchSetTitleBlockOutput:
    return await tool.run(SchSetTitleBlockInput(**kwargs))  # type: ignore[arg-type]


# -- preflight -------------------------------------------------------------


@pytest.mark.asyncio
async def test_sch_not_found_returns_status(tmp_path: Path) -> None:
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=tmp_path / "missing.kicad_sch", title="x")
    assert out.status == "sch_not_found"
    assert out.sch_path is None
    assert "no such file" in (out.note or "")


@pytest.mark.asyncio
async def test_sch_not_found_when_directory(tmp_path: Path) -> None:
    target = tmp_path / "looks_like_a_sch.kicad_sch"
    target.mkdir()
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=target, title="x")
    assert out.status == "sch_not_found"
    assert "regular file" in (out.note or "")


@pytest.mark.asyncio
async def test_sch_not_found_wrong_suffix(tmp_path: Path) -> None:
    target = tmp_path / "board.kicad_pcb"
    target.write_text(_PCB_NOT_SCH, encoding="utf-8")
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=target, title="x")
    assert out.status == "sch_not_found"
    assert ".kicad_sch" in (out.note or "")


@pytest.mark.asyncio
async def test_invalid_schema_wrong_top_head(tmp_path: Path) -> None:
    # Right suffix, wrong content — a .kicad_sch file that parses as
    # something else must not be touched.
    target = tmp_path / "impostor.kicad_sch"
    target.write_text(_PCB_NOT_SCH, encoding="utf-8")
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=target, title="x")
    assert out.status == "invalid_schema"
    assert "kicad_pcb" in (out.note or "")
    # Original file is unchanged.
    assert target.read_text(encoding="utf-8") == _PCB_NOT_SCH


@pytest.mark.asyncio
async def test_parse_failed_when_sexpr_is_malformed(tmp_path: Path) -> None:
    target = tmp_path / "broken.kicad_sch"
    target.write_text("(kicad_sch (version", encoding="utf-8")  # unterminated list
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=target, title="x")
    assert out.status == "parse_failed"
    assert "parse failed" in (out.note or "").lower()


# -- no-op paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_fields_passed_returns_no_changes(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=sch)
    assert out.status == "no_changes"
    assert out.fields_changed == []
    # File untouched.
    assert sch.read_bytes() == before


@pytest.mark.asyncio
async def test_values_matching_existing_are_no_changes(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    # Pass the same values that already exist — should be a no-op.
    out = await _run(
        tool,
        sch_path=sch,
        title="Old Title",
        date="2024-01-01",
        rev="A",
        company="Acme",
        comment1="one",
        comment2="two",
    )
    assert out.status == "no_changes"
    assert out.fields_changed == []
    assert sch.read_bytes() == before


# -- dry-run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_reports_planned_changes_without_writing(
    tmp_path: Path,
) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(
        tool,
        sch_path=sch,
        title="New Title",
        rev="B",
        dry_run=True,
    )
    assert out.status == "dry_run"
    assert set(out.fields_changed) == {"title", "rev"}
    # File is byte-for-byte unchanged.
    assert sch.read_bytes() == before
    # Dry-run doesn't touch the snapshot layer — meta.snapshot_ref stays None.
    assert out.meta.snapshot_ref is None


@pytest.mark.asyncio
async def test_dry_run_with_only_matching_values_returns_no_changes(
    tmp_path: Path,
) -> None:
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    # Every requested value already matches — dry_run should still land
    # on no_changes (not dry_run), because there's nothing to plan.
    out = await _run(tool, sch_path=sch, title="Old Title", dry_run=True)
    assert out.status == "no_changes"
    assert out.fields_changed == []
    assert sch.read_bytes() == before


# -- simple field mutation -------------------------------------------------


@pytest.mark.asyncio
async def test_update_existing_simple_field_round_trips(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=sch, title="Main 5V")
    assert out.status == "ok"
    assert out.fields_changed == ["title"]

    # Reload and verify the value stuck.
    doc = SexprDocument.from_path(sch)
    tb = doc.root.find("title_block")
    assert tb is not None
    title_node = tb.find("title")
    assert title_node is not None
    assert _atom_at(title_node, 1).text == "Main 5V"
    # Other fields must survive untouched — round-trip integrity.
    date_node = tb.find("date")
    assert date_node is not None
    assert _atom_at(date_node, 1).text == "2024-01-01"


@pytest.mark.asyncio
async def test_clearing_simple_field_with_empty_string(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=sch, company="")
    assert out.status == "ok"
    assert out.fields_changed == ["company"]

    doc = SexprDocument.from_path(sch)
    tb = doc.root.find("title_block")
    assert tb is not None
    company_node = tb.find("company")
    assert company_node is not None
    assert _atom_at(company_node, 1).text == ""


@pytest.mark.asyncio
async def test_multiple_fields_in_one_call(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(
        tool,
        sch_path=sch,
        title="Main 5V",
        date="2026-04-15",
        rev="B",
        company="NewCo",
    )
    assert out.status == "ok"
    assert set(out.fields_changed) == {"title", "date", "rev", "company"}

    doc = SexprDocument.from_path(sch)
    tb = doc.root.find("title_block")
    assert tb is not None
    assert _field_text(tb, "title") == "Main 5V"
    assert _field_text(tb, "date") == "2026-04-15"
    assert _field_text(tb, "rev") == "B"
    assert _field_text(tb, "company") == "NewCo"


@pytest.mark.asyncio
async def test_add_missing_simple_field(tmp_path: Path) -> None:
    # Strip the company entry and verify it's added fresh.
    content = _EMPTY_SCH.replace('\t\t(company "Acme")\n', "")
    sch = _write_sch(tmp_path, content)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=sch, company="Freshly Added")
    assert out.status == "ok"
    assert out.fields_changed == ["company"]

    doc = SexprDocument.from_path(sch)
    tb = doc.root.find("title_block")
    assert tb is not None
    assert _field_text(tb, "company") == "Freshly Added"


# -- comment slot mutation -------------------------------------------------


@pytest.mark.asyncio
async def test_update_existing_comment_slot(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=sch, comment1="new comment one")
    assert out.status == "ok"
    assert out.fields_changed == ["comment1"]

    doc = SexprDocument.from_path(sch)
    tb = doc.root.find("title_block")
    assert tb is not None
    # Slot 1 updated; slot 2 untouched.
    by_slot = _comments_by_slot(tb)
    assert by_slot[1] == "new comment one"
    assert by_slot[2] == "two"


@pytest.mark.asyncio
async def test_add_missing_comment_slot(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=sch, comment5="slot five is new")
    assert out.status == "ok"
    assert out.fields_changed == ["comment5"]

    doc = SexprDocument.from_path(sch)
    tb = doc.root.find("title_block")
    assert tb is not None
    comments = tb.find_all("comment")
    by_slot = _comments_by_slot(tb)
    assert by_slot[5] == "slot five is new"
    # Slot atom must be unquoted (a bare integer), matching KiCAD's format.
    slot5_node = next(c for c in comments if _atom_at(c, 1).text == "5")
    assert _atom_at(slot5_node, 1).quoted is False


@pytest.mark.asyncio
async def test_multiple_comment_slots_in_one_call(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(
        tool,
        sch_path=sch,
        comment1="a",
        comment2="b",
        comment9="z",
    )
    assert out.status == "ok"
    assert set(out.fields_changed) == {"comment1", "comment2", "comment9"}


# -- title_block synthesis --------------------------------------------------


@pytest.mark.asyncio
async def test_title_block_synthesized_when_missing(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path, _EMPTY_SCH_NO_TITLE_BLOCK)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=sch, title="Fresh", rev="X")
    assert out.status == "ok"
    assert set(out.fields_changed) == {"title", "rev"}

    doc = SexprDocument.from_path(sch)
    tb = doc.root.find("title_block")
    assert tb is not None
    assert _field_text(tb, "title") == "Fresh"
    assert _field_text(tb, "rev") == "X"

    # title_block landed right after `paper` per the canonical ordering.
    indices: dict[str | None, int] = {}
    for idx, child in enumerate(doc.root.items):
        if isinstance(child, SList):
            indices[child.head] = idx
    assert indices["title_block"] == indices["paper"] + 1


# -- snapshot plumbing -----------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_mode_off_surfaces_disabled_ref(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=sch, title="Any Change")
    assert out.status == "ok"
    # `mode='off'` returns the 'disabled' sentinel, not None.
    assert out.meta.snapshot_ref == "disabled"


@pytest.mark.asyncio
async def test_snapshot_mode_copy_creates_directory(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    sch = _write_sch(project)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("copy"))
    out = await _run(tool, sch_path=sch, title="Snapshotted")
    assert out.status == "ok"
    ref = out.meta.snapshot_ref
    assert ref is not None
    assert ref.startswith("copy:")
    snap_dir = Path(ref[len("copy:") :])
    assert snap_dir.is_dir()
    # Pre-mutation schematic bytes were captured before we wrote.
    assert (snap_dir / "board.kicad_sch").is_file()


@pytest.mark.asyncio
async def test_snapshot_skipped_when_no_changes(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    tool = SchSetTitleBlockTool(config=_config_with_snapshot("copy"))
    out = await _run(tool, sch_path=sch, title="Old Title")  # already matches
    assert out.status == "no_changes"
    # No snapshot when we didn't mutate.
    assert out.meta.snapshot_ref is None
    # And no .kimcp/snapshots directory was created.
    assert not (tmp_path / ".kimcp" / "snapshots").exists()


# -- write failure ---------------------------------------------------------


@pytest.mark.asyncio
async def test_write_failed_when_serialize_produces_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-trip guard catches a bad serialize and surfaces write_failed.

    SexprDocument.save re-parses its own output and asserts structural
    equality with the in-memory tree. If the serialize step produces
    something that doesn't round-trip, save raises — we translate that
    to status='write_failed' and keep the snapshot_ref so callers can
    recover from the pre-mutation state.
    """
    sch = _write_sch(tmp_path)
    before = sch.read_bytes()

    def _broken_serialize(_root, _source):
        return b"not an s-expression"

    monkeypatch.setattr("kimcp.sexpr.document.serialize", _broken_serialize)

    tool = SchSetTitleBlockTool(config=_config_with_snapshot("off"))
    out = await _run(tool, sch_path=sch, title="Will Not Stick")
    assert out.status == "write_failed"
    # We still took a snapshot before attempting the save.
    assert out.meta.snapshot_ref == "disabled"
    # Atomic save failed before the rename — the original file is intact.
    assert sch.read_bytes() == before


# -- DI + default config ---------------------------------------------------


@pytest.mark.asyncio
async def test_set_config_wires_snapshot_mode(tmp_path: Path) -> None:
    sch = _write_sch(tmp_path)
    # Start without a config — default snapshot mode is 'git'. Point at
    # a directory that isn't a git repo so 'git' mode falls back to
    # 'copy'; that's a known behavior we rely on for greenfield dirs.
    tool = SchSetTitleBlockTool()
    tool.set_config(_config_with_snapshot("copy"))
    out = await _run(tool, sch_path=sch, title="After DI")
    assert out.status == "ok"
    assert (out.meta.snapshot_ref or "").startswith("copy:")


@pytest.mark.asyncio
async def test_instance_without_config_defaults_to_git_mode(
    tmp_path: Path,
) -> None:
    """Standalone construction still gets a safe snapshot posture.

    No config wired → defaults to ``mode='git'``. Since tmp_path isn't
    a git repo, snapshot() falls back to copy-mode internally — the
    result is still a real snapshot directory, not a crash.
    """
    sch = _write_sch(tmp_path)
    tool = SchSetTitleBlockTool()
    out = await _run(tool, sch_path=sch, title="No Config Wired")
    assert out.status == "ok"
    ref = out.meta.snapshot_ref
    assert ref is not None
    # Not a git repo → fallback to copy.
    assert ref.startswith("copy:")
