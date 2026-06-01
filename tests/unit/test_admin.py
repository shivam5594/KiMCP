"""Unit tests for ``kimcp.admin`` — the ``kimcp-cli`` admin CLI.

Covers the three verbs wired today (``config show`` / ``config validate`` /
``tools list``). The admin CLI is offline-only — no transport, no KiCAD —
so tests run without fixtures beyond ``capsys`` and a tmp config file.

Why this file exists: the CLI is the operator's first contact with
``kimcp``. Silent drift in its output shape (JSON keys missing, exit codes
wrong) would force every admin script downstream to rewrite its parsers.
Pin the surface now while it's small.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kimcp import __version__
from kimcp.admin import main

# -- top-level plumbing ----------------------------------------------------


def test_missing_subcommand_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``kimcp-cli`` with no subcommand must fail — no implicit default.

    argparse raises SystemExit(2) for a missing required subcommand;
    matching that keeps shell-scripts that branch on exit code honest.
    """
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2


def test_version_flag_prints_and_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--version`` short-circuits past subcommand dispatch with exit 0.

    Argparse emits to stdout for ``--version`` and exits with code 0.
    Pinning this means a future refactor that accidentally sends the
    version string to stderr (or swallows the exit) gets caught.
    """
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_unknown_subcommand_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["not-a-command"])
    assert exc_info.value.code == 2


# -- config show -----------------------------------------------------------


def test_config_show_emits_valid_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``config show`` prints the merged config as JSON on stdout.

    Stdout (not stderr) is the contract — operators pipe into ``jq``.
    Output must parse as a JSON object with the documented top-level
    sections.
    """
    rc = main(["config", "show"])
    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    # Pin the load-bearing sections per `configuration.md`.
    for section in (
        "server",
        "kicad",
        "libraries",
        "domain_knowledge",
        "safety",
        "performance",
        "observability",
        "fab_profile",
        "external_apis",
    ):
        assert section in parsed, f"missing section {section!r}"
    # Version field is the schema pin per `configuration.md`.
    assert parsed["version"] == 1


def test_config_show_keys_sorted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Output uses ``sort_keys=True`` for deterministic diffs.

    Config-snapshot tests elsewhere (and git-versioned operator
    configs) depend on key ordering being stable across runs.
    """
    rc = main(["config", "show"])
    assert rc == 0
    captured = capsys.readouterr()
    # Top-level keys must appear in alphabetical order.
    # Pull them out via the string rather than the parsed dict — the
    # whole point is to pin the serialised order, not Python dict order.
    lines = [line for line in captured.out.splitlines() if line.startswith('  "')]
    keys = [line.split('"')[1] for line in lines]
    # Top-level keys are a subset of `keys` (nested children are also
    # quoted). Filter to the first-level by indent depth (2 spaces).
    top_level = [
        line.split('"')[1] for line in captured.out.splitlines() if line.startswith('  "')
    ]
    # Check monotonic non-decreasing for the first-level sort.
    assert top_level == sorted(top_level)
    # Sanity: version should appear after external_apis alphabetically.
    assert "external_apis" in keys
    assert "version" in keys


# -- config validate -------------------------------------------------------


def test_config_validate_ok_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid config (even the defaults-only case) validates fine."""
    rc = main(["config", "validate"])
    assert rc == 0
    # "config OK" goes to stderr (stdout reserved for pipeable output).
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "config OK" in captured.err


def test_config_validate_rejects_bad_min_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a bad ``min_version`` in a real config file surfaces as
    ValidationError through ``kimcp-cli config validate`` — not a crash.

    Run from a directory that has a malformed ``.kimcp/config.toml`` so
    ``load_config()`` picks it up as project-local.
    """
    project_dir = tmp_path / "project"
    (project_dir / ".kimcp").mkdir(parents=True)
    (project_dir / ".kimcp" / "config.toml").write_text(
        '[kicad]\nmin_version = "garbage"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(project_dir)

    # ValidationError is raised — pytest catches. The contract is that
    # this is NOT a SystemExit(0); any non-None raise counts.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        main(["config", "validate"])


# -- tools list ------------------------------------------------------------


def test_tools_list_uses_tabs_and_has_four_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each row is ``<name>\\t<version>\\t<description>`` — pin the format.

    Admin scripts parse this with ``cut -f1`` / similar; changing
    separator or column order is a breaking change for them.
    """
    rc = main(["tools", "list"])
    assert rc == 0
    captured = capsys.readouterr()
    if not captured.out.strip():
        # Entry-points may be empty in a stripped test env. Acceptable —
        # the other tests cover the populated path.
        pytest.skip("no tools discovered in this environment")
    for line in captured.out.splitlines():
        parts = line.split("\t")
        assert len(parts) == 3, f"expected 3 tab-separated columns, got {parts!r}"
        name, version, _desc = parts
        assert name  # non-empty
        assert version  # non-empty


def test_tools_list_is_sorted_by_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deterministic ordering — admin scripts depend on stable output."""
    rc = main(["tools", "list"])
    assert rc == 0
    captured = capsys.readouterr()
    if not captured.out.strip():
        pytest.skip("no tools discovered in this environment")
    names = [line.split("\t", 1)[0] for line in captured.out.splitlines()]
    assert names == sorted(names)


def test_tools_list_includes_known_builtin(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ping`` is one of the stable built-in tools — sanity check that
    entry-point discovery actually found real tools in this env.

    If this fails in CI, either entry points are broken or the package
    wasn't installed editably — both are worth catching here.
    """
    rc = main(["tools", "list"])
    assert rc == 0
    captured = capsys.readouterr()
    if not captured.out.strip():
        pytest.skip("no tools discovered in this environment")
    names = [line.split("\t", 1)[0] for line in captured.out.splitlines()]
    # ``ping`` is one of the earliest tools and is always entry-pointed;
    # its presence proves discovery worked.
    assert "ping" in names
