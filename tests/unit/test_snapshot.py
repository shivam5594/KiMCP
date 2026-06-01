"""Unit tests for safety.snapshot + safety.audit."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kimcp.safety import audit_log_path, record, snapshot
from kimcp.safety.snapshot import SnapshotError, is_git_repo


def _write_project(root: Path) -> None:
    (root / "schematic.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    (root / "pcb.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")


def test_copy_snapshot_creates_directory(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _write_project(project)

    ref = snapshot(project, mode="copy", reason="unit-test")
    assert ref.startswith("copy:")
    snap_dir = Path(ref[len("copy:") :])
    assert snap_dir.is_dir()
    assert (snap_dir / "schematic.kicad_sch").is_file()
    assert (snap_dir / "pcb.kicad_pcb").is_file()
    assert (snap_dir / "_kimcp_snapshot.txt").is_file()


def test_copy_snapshot_skips_kimcp_state_dir(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _write_project(project)
    (project / ".kimcp").mkdir()
    (project / ".kimcp" / "stale.txt").write_text("should not copy", encoding="utf-8")

    ref = snapshot(project, mode="copy")
    snap_dir = Path(ref[len("copy:") :])
    # The copy's `.kimcp` should not contain the stale file.
    assert not (snap_dir / ".kimcp" / "stale.txt").exists()


def test_snapshot_disabled_returns_sentinel(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _write_project(project)

    assert snapshot(project, mode="off") == "disabled"


def test_snapshot_raises_for_missing_project(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        snapshot(tmp_path / "missing")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_snapshot_commits_when_repo(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _write_project(project)
    subprocess.run(["git", "-C", str(project), "init", "-q", "-b", "main"], check=True)
    # Initial commit so HEAD exists.
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.email=test@example",
            "-c",
            "user.name=tester",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    assert is_git_repo(project)

    ref = snapshot(project, mode="git", reason="test")
    assert ref.startswith("git:")
    sha = ref[len("git:") :]
    assert len(sha) >= 7


def test_audit_record_appends_jsonl(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()

    record(
        project,
        tool="move_component",
        input_summary={"ref": "R12", "dx": 2.54},
        snapshot_ref="copy:/tmp/snap",
        note="dry-run",
    )
    record(
        project,
        tool="delete_component",
        input_summary={"ref": "C7"},
        snapshot_ref=None,
        note="",
    )

    log_path = audit_log_path(project)
    assert log_path.is_file()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    entries = [json.loads(line) for line in lines]
    assert {entry["tool"] for entry in entries} == {"move_component", "delete_component"}
    assert entries[0]["snapshot_ref"] == "copy:/tmp/snap"


# -- SnapshotPolicy (Perf P1b) ---------------------------------------------


def test_snapshot_policy_rejects_zero() -> None:
    from kimcp.safety import SnapshotPolicy
    with pytest.raises(ValueError, match=">= 1"):
        SnapshotPolicy(every_n_calls=0)


def test_snapshot_policy_n_equals_one_snapshots_every_call(tmp_path: Path) -> None:
    """N=1 should match the unconditional snapshot() semantics exactly."""
    from kimcp.safety import SnapshotPolicy
    project = tmp_path / "proj"; project.mkdir()
    _write_project(project)
    policy = SnapshotPolicy(every_n_calls=1)
    ref1 = policy.maybe_snapshot(project, mode="copy", reason="call1")
    ref2 = policy.maybe_snapshot(project, mode="copy", reason="call2")
    assert ref1.startswith("copy:")
    assert ref2.startswith("copy:")
    assert ref1 != ref2  # different timestamps


def test_snapshot_policy_throttles_to_every_n(tmp_path: Path) -> None:
    """With N=3, only calls 1, 4, 7, … actually snapshot; the rest skip."""
    from kimcp.safety import SnapshotPolicy
    project = tmp_path / "proj"; project.mkdir()
    _write_project(project)
    policy = SnapshotPolicy(every_n_calls=3)
    refs = [
        policy.maybe_snapshot(project, mode="copy", reason=f"c{i}")
        for i in range(7)
    ]
    # Call indices 1, 4, 7 → snapshot. (next_count 1, 4, 7; (n-1)%3==0)
    assert refs[0].startswith("copy:")
    assert refs[1].startswith("skipped:every-3:call=2")
    assert refs[2].startswith("skipped:every-3:call=3")
    assert refs[3].startswith("copy:")  # call 4
    assert refs[4].startswith("skipped:every-3:call=5")
    assert refs[5].startswith("skipped:every-3:call=6")
    assert refs[6].startswith("copy:")  # call 7


def test_snapshot_policy_per_project_independent(tmp_path: Path) -> None:
    """Two project_roots should have independent counters."""
    from kimcp.safety import SnapshotPolicy
    proj_a = tmp_path / "a"; proj_a.mkdir(); _write_project(proj_a)
    proj_b = tmp_path / "b"; proj_b.mkdir(); _write_project(proj_b)
    policy = SnapshotPolicy(every_n_calls=2)
    # Call: A,B,A,B → both projects get their 1st (snapshot) and 2nd (skip).
    rA1 = policy.maybe_snapshot(proj_a, mode="copy", reason="a1")
    rB1 = policy.maybe_snapshot(proj_b, mode="copy", reason="b1")
    rA2 = policy.maybe_snapshot(proj_a, mode="copy", reason="a2")
    rB2 = policy.maybe_snapshot(proj_b, mode="copy", reason="b2")
    assert rA1.startswith("copy:")
    assert rB1.startswith("copy:")
    assert rA2.startswith("skipped:every-2:call=2")
    assert rB2.startswith("skipped:every-2:call=2")


def test_take_snapshot_with_none_policy_matches_direct_snapshot(tmp_path: Path) -> None:
    """take_snapshot(None, ...) must behave like calling snapshot() directly."""
    from kimcp.safety import take_snapshot
    project = tmp_path / "proj"; project.mkdir()
    _write_project(project)
    ref = take_snapshot(None, project, mode="copy", reason="test")
    assert ref.startswith("copy:")
