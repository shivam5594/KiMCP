"""Unit tests for the config loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kimcp._types import Backend
from kimcp.config import (
    CONFIG_VERSION,
    Config,
    KiCadCfg,
    load_config,
    project_config_path,
    user_global_config_path,
)


def test_default_config_is_ipc_first() -> None:
    cfg = Config()
    assert cfg.version == CONFIG_VERSION
    # Per ADR-0014 IPC is first in preference order.
    assert cfg.kicad.preferred_backend_order[0] == Backend.IPC
    assert cfg.kicad.min_version == "9.0.0"


def test_default_domain_knowledge_enables_all_skills() -> None:
    cfg = Config()
    assert "signal-integrity" in cfg.domain_knowledge.enabled_skills
    assert "dfm" in cfg.domain_knowledge.enabled_skills
    assert cfg.domain_knowledge.strictness == "hints"


def test_load_config_from_nonexistent_files_uses_defaults(tmp_path: Path) -> None:
    cfg = load_config(
        user_global=tmp_path / "never_exists.toml",
        project_local=tmp_path / "also_missing.toml",
    )
    assert cfg == Config()


def test_load_config_project_overrides_user(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    user.write_text('[domain_knowledge]\nstrictness = "off"\n', encoding="utf-8")
    project = tmp_path / "project.toml"
    project.write_text('[domain_knowledge]\nstrictness = "enforce"\n', encoding="utf-8")

    cfg = load_config(user_global=user, project_local=project)
    assert cfg.domain_knowledge.strictness == "enforce"


def test_load_config_session_overrides_win(tmp_path: Path) -> None:
    project = tmp_path / "project.toml"
    project.write_text("[safety]\nmass_rename_threshold = 5\n", encoding="utf-8")

    cfg = load_config(
        project_local=project,
        user_global=tmp_path / "missing.toml",
        session_overrides={"safety": {"mass_rename_threshold": 100}},
    )
    assert cfg.safety.mass_rename_threshold == 100


def test_load_config_ignores_unknown_top_level(tmp_path: Path) -> None:
    project = tmp_path / "project.toml"
    project.write_text(
        '[future_section]\nsomething = "tbd"\n[safety]\nmass_rename_threshold = 7\n',
        encoding="utf-8",
    )
    cfg = load_config(
        project_local=project,
        user_global=tmp_path / "missing.toml",
    )
    assert cfg.safety.mass_rename_threshold == 7


def test_paths_are_absolute() -> None:
    assert user_global_config_path().is_absolute()
    p = project_config_path(Path("/tmp/example"))
    assert str(p).endswith(".kimcp/config.toml")


# -- min_version validator (pins audit-fix I5) -----------------------------


def test_min_version_validator_accepts_valid_semver() -> None:
    """Good inputs round-trip untouched."""
    assert KiCadCfg(min_version="9.0.0").min_version == "9.0.0"
    assert KiCadCfg(min_version="10.1.2").min_version == "10.1.2"


def test_min_version_validator_rejects_garbage() -> None:
    """A typo in `[kicad] min_version = "..."` in config.toml must fail
    loudly at load time, not crash cryptically inside CliBackend's
    constructor downstream."""
    with pytest.raises(ValidationError) as excinfo:
        KiCadCfg(min_version="not-a-version")
    # Error message should name the field and include the offending value.
    assert "min_version" in str(excinfo.value)
    assert "not-a-version" in str(excinfo.value)


def test_min_version_validator_rejects_empty_string() -> None:
    with pytest.raises(ValidationError):
        KiCadCfg(min_version="")


def test_load_config_rejects_bad_min_version_in_toml(tmp_path: Path) -> None:
    """End-to-end: bad value in a real TOML file surfaces as ValidationError."""
    project = tmp_path / "project.toml"
    project.write_text('[kicad]\nmin_version = "garbage"\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(
            project_local=project,
            user_global=tmp_path / "missing.toml",
        )
