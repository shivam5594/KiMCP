"""Unit tests for KiCAD version parsing + comparison."""

from __future__ import annotations

import pytest

from kimcp.cli.version import KiCadVersion, parse_cli_version

# -- parsing ----------------------------------------------------------------


def test_parse_bare_triple() -> None:
    v = KiCadVersion.parse("9.0.1")
    assert v is not None
    assert v.as_tuple() == (9, 0, 1)
    assert v.raw == "9.0.1"


def test_parse_with_suffix_preserves_raw_but_not_ordering() -> None:
    v = KiCadVersion.parse("10.0.0-rc1-1234-gabcdef01")
    assert v is not None
    assert v.as_tuple() == (10, 0, 0)
    assert "rc1" in v.raw


def test_parse_returns_none_on_garbage() -> None:
    assert KiCadVersion.parse("not a version") is None
    assert KiCadVersion.parse("") is None


def test_parse_cli_output_prefers_version_line() -> None:
    stdout = (
        "Application: kicad-cli\n"
        "Version: 9.0.1+9.0.0-0-10.fc40, release build\n"
        "\n"
        "Libraries:\n"
        "\twxWidgets 3.2.5\n"
        "\tboost 1.82.0\n"
    )
    v = parse_cli_version(stdout)
    assert v is not None
    assert v.as_tuple() == (9, 0, 1)


def test_parse_cli_output_nightly() -> None:
    stdout = "Application: kicad-cli\nVersion: 10.0.0-rc1-1234-gabcdef01, development build\n"
    v = parse_cli_version(stdout)
    assert v is not None
    assert v.as_tuple() == (10, 0, 0)


def test_parse_cli_output_bare_form() -> None:
    v = parse_cli_version("9.0.1\n")
    assert v is not None
    assert v.as_tuple() == (9, 0, 1)


def test_parse_cli_output_empty_returns_none() -> None:
    assert parse_cli_version("") is None


def test_parse_cli_output_skips_wxwidgets_line() -> None:
    # Libraries section has 3.2.5 — parser must not snag that over
    # the Version: line above.
    stdout = (
        "Application: kicad-cli\nVersion: 9.0.1, release build\n\nLibraries:\n\twxWidgets 3.2.5\n"
    )
    v = parse_cli_version(stdout)
    assert v is not None
    assert v.as_tuple() == (9, 0, 1)


# -- ordering ---------------------------------------------------------------


def test_ordering_ignores_suffix() -> None:
    a = KiCadVersion.parse("9.0.1-rc1")
    b = KiCadVersion.parse("9.0.1+release-build")
    assert a is not None and b is not None
    assert a == b  # suffix must not affect equality
    assert not (a < b)
    assert not (a > b)


def test_ordering_on_major_minor_patch() -> None:
    assert KiCadVersion(9, 0, 0) < KiCadVersion(9, 0, 1)
    assert KiCadVersion(9, 0, 1) < KiCadVersion(9, 1, 0)
    assert KiCadVersion(9, 1, 0) < KiCadVersion(10, 0, 0)
    assert KiCadVersion(10, 0, 0) >= KiCadVersion(9, 0, 1)


def test_min_version_gate_9_0_0() -> None:
    required = KiCadVersion.parse("9.0.0")
    assert required is not None
    assert KiCadVersion.parse("9.0.0") >= required  # boundary
    assert KiCadVersion.parse("9.0.1") >= required
    assert KiCadVersion.parse("10.0.0") >= required
    assert KiCadVersion.parse("8.99.99") < required


def test_negative_components_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        KiCadVersion(major=-1, minor=0, patch=0)


def test_hash_matches_equality() -> None:
    a = KiCadVersion.parse("9.0.1-rc1")
    b = KiCadVersion.parse("9.0.1+release-build")
    assert a is not None and b is not None
    assert hash(a) == hash(b)
    # Usable as dict/set key.
    assert len({a, b}) == 1
