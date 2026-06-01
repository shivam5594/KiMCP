"""Unit tests for ``kimcp.resources.ResourceProvider`` (M13).

Covers both sides of the primitive:

* ``list()`` — discovery / filtering / exclusion / ordering.
* ``read(uri)`` — URI parsing, path-traversal containment, suffix gate,
  UTF-8 decode, not-found vs out-of-root error separation.

Path-traversal hygiene is the load-bearing axis here: the resources
primitive is the first boundary between LLM-controlled input and the local
filesystem, so every rejection path gets its own pin.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kimcp.errors import INVALID_PARAMS, RpcError
from kimcp.resources import ResourceProvider

_SCH_BODY = """\
(kicad_sch
\t(version 20240108)
\t(generator "eeschema")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols))
"""

_PCB_BODY = """\
(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(paper "A4")
\t(layers))
"""


def _write(path: Path, body: str = _SCH_BODY) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# =========================================================================
# list()
# =========================================================================


def test_list_returns_empty_for_missing_root(tmp_path: Path) -> None:
    """Resolving against a nonexistent dir yields [] — not a raise.

    The MCP ``resources/list`` contract doesn't have a "no such root"
    error shape; returning empty matches the "this server has no
    resources today" case cleanly.
    """
    missing = tmp_path / "does-not-exist"
    provider = ResourceProvider(missing)
    assert provider.list_resources() == []


def test_list_flat_single_schematic(tmp_path: Path) -> None:
    sch = _write(tmp_path / "board.kicad_sch")
    provider = ResourceProvider(tmp_path)

    result = provider.list_resources()
    assert len(result) == 1
    entry = result[0]
    assert entry["uri"] == sch.resolve().as_uri()
    assert entry["name"] == "board.kicad_sch"
    assert entry["description"] == "KiCAD schematic: board.kicad_sch"
    assert entry["mimeType"] == "application/x-kicad-schematic"
    assert entry["size"] == len(_SCH_BODY.encode("utf-8"))


def test_list_returns_all_kicad_extensions(tmp_path: Path) -> None:
    """Every suffix in ``_MIME_BY_SUFFIX`` is surfaced."""
    _write(tmp_path / "a.kicad_sch")
    _write(tmp_path / "a.kicad_pcb", _PCB_BODY)
    _write(tmp_path / "a.kicad_pro", "{}")
    _write(tmp_path / "lib.kicad_sym", "(kicad_symbol_lib)")
    _write(tmp_path / "fp.kicad_mod", "(footprint)")
    _write(tmp_path / "wks.kicad_wks", "(kicad_wks)")
    _write(tmp_path / "rules.kicad_dru", "(version 1)")

    provider = ResourceProvider(tmp_path)
    names = [r["name"] for r in provider.list_resources()]

    # Seven extensions, all surfaced.
    assert names == sorted(names)
    assert set(names) == {
        "a.kicad_pcb",
        "a.kicad_pro",
        "a.kicad_sch",
        "fp.kicad_mod",
        "lib.kicad_sym",
        "rules.kicad_dru",
        "wks.kicad_wks",
    }


def test_list_ignores_non_kicad_files(tmp_path: Path) -> None:
    _write(tmp_path / "board.kicad_sch")
    _write(tmp_path / "README.md", "# noise\n")
    _write(tmp_path / "data.json", "{}\n")
    _write(tmp_path / "noext", "nope")

    provider = ResourceProvider(tmp_path)
    names = [r["name"] for r in provider.list_resources()]
    assert names == ["board.kicad_sch"]


def test_list_walks_nested_directories_in_stable_order(tmp_path: Path) -> None:
    _write(tmp_path / "top.kicad_sch")
    _write(tmp_path / "sub" / "a" / "deep.kicad_sch")
    _write(tmp_path / "sub" / "mid.kicad_pcb", _PCB_BODY)

    provider = ResourceProvider(tmp_path)
    names = [r["name"] for r in provider.list_resources()]
    # Sorted by POSIX-style rel path — tests + prompt context both need
    # stable ordering so diffing resource lists across calls is meaningful.
    assert names == ["sub/a/deep.kicad_sch", "sub/mid.kicad_pcb", "top.kicad_sch"]


@pytest.mark.parametrize(
    "excluded",
    [".git", ".kimcp", ".venv", "node_modules", "__pycache__", "fp-info-cache"],
)
def test_list_prunes_excluded_directories(tmp_path: Path, excluded: str) -> None:
    _write(tmp_path / "real.kicad_sch")
    _write(tmp_path / excluded / "hidden.kicad_sch")

    provider = ResourceProvider(tmp_path)
    names = [r["name"] for r in provider.list_resources()]
    assert names == ["real.kicad_sch"]


def test_list_prunes_exclusion_even_when_deeply_nested(tmp_path: Path) -> None:
    """Exclusion matches on directory name, not path — applies at any depth.

    This pins the default behavior: a ``.git`` checked in to a submodule
    folder still gets pruned. If we ever change this to root-only, the
    test fails loudly.
    """
    _write(tmp_path / "keep.kicad_sch")
    _write(tmp_path / "some" / "path" / ".git" / "inside.kicad_sch")
    _write(tmp_path / "deep" / "nest" / ".kimcp" / "snapshots" / "stale.kicad_sch")

    provider = ResourceProvider(tmp_path)
    names = [r["name"] for r in provider.list_resources()]
    assert names == ["keep.kicad_sch"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks required")
def test_list_skips_symlinked_files(tmp_path: Path) -> None:
    real = _write(tmp_path / "real.kicad_sch")
    link = tmp_path / "link.kicad_sch"
    link.symlink_to(real)

    provider = ResourceProvider(tmp_path)
    names = [r["name"] for r in provider.list_resources()]
    assert names == ["real.kicad_sch"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks required")
def test_list_skips_symlinked_directories(tmp_path: Path) -> None:
    """A symlinked dir is not walked — prevents loops and off-tree leaks."""
    _write(tmp_path / "keep.kicad_sch")
    outside = tmp_path.parent / f"{tmp_path.name}__outside"
    outside.mkdir()
    try:
        _write(outside / "leaked.kicad_sch")
        linked = tmp_path / "via_link"
        linked.symlink_to(outside)

        provider = ResourceProvider(tmp_path)
        names = [r["name"] for r in provider.list_resources()]
        assert names == ["keep.kicad_sch"]
    finally:
        # tmp_path cleanup wouldn't reach ``outside``.
        for f in outside.rglob("*"):
            if f.is_file():
                f.unlink()
        outside.rmdir()


def test_list_resolves_project_root_at_construction_time(tmp_path: Path) -> None:
    """Constructor canonicalizes the root; list() works with symlink inputs."""
    if os.name == "nt":
        pytest.skip("POSIX symlinks required")
    real_root = tmp_path / "real"
    real_root.mkdir()
    _write(real_root / "x.kicad_sch")
    alias = tmp_path / "alias"
    alias.symlink_to(real_root)

    provider = ResourceProvider(alias)
    # Both .project_root and the emitted URIs are rooted in the real path.
    assert provider.project_root == real_root.resolve()
    names = [r["name"] for r in provider.list_resources()]
    assert names == ["x.kicad_sch"]


# =========================================================================
# read() — happy path
# =========================================================================


def test_read_returns_text_content_for_valid_schematic(tmp_path: Path) -> None:
    sch = _write(tmp_path / "board.kicad_sch")
    provider = ResourceProvider(tmp_path)

    contents = provider.read(sch.resolve().as_uri())
    assert len(contents) == 1
    item = contents[0]
    assert item["uri"] == sch.resolve().as_uri()
    assert item["mimeType"] == "application/x-kicad-schematic"
    assert item["text"] == _SCH_BODY


def test_read_handles_url_encoded_space_in_path(tmp_path: Path) -> None:
    sch = _write(tmp_path / "my board.kicad_sch")
    provider = ResourceProvider(tmp_path)

    # ``Path.as_uri()`` already percent-encodes spaces; this just pins that
    # we unquote on the way in.
    contents = provider.read(sch.resolve().as_uri())
    assert contents[0]["text"] == _SCH_BODY


def test_read_accepts_localhost_authority(tmp_path: Path) -> None:
    """RFC 8089 permits ``file://localhost/path`` as well as ``file:///path``."""
    sch = _write(tmp_path / "board.kicad_sch")
    provider = ResourceProvider(tmp_path)

    abs_path = sch.resolve().as_posix()
    contents = provider.read(f"file://localhost{abs_path}")
    assert contents[0]["text"] == _SCH_BODY


# =========================================================================
# read() — URI parsing rejections
# =========================================================================


def test_read_rejects_non_file_scheme(tmp_path: Path) -> None:
    provider = ResourceProvider(tmp_path)
    with pytest.raises(RpcError) as exc:
        provider.read("https://example.com/a.kicad_sch")
    assert exc.value.code == INVALID_PARAMS
    assert "scheme" in exc.value.message


def test_read_rejects_empty_scheme(tmp_path: Path) -> None:
    provider = ResourceProvider(tmp_path)
    with pytest.raises(RpcError) as exc:
        provider.read("/absolute/but/no/scheme.kicad_sch")
    assert exc.value.code == INVALID_PARAMS


def test_read_rejects_foreign_host(tmp_path: Path) -> None:
    provider = ResourceProvider(tmp_path)
    with pytest.raises(RpcError) as exc:
        provider.read("file://some-other-host/x.kicad_sch")
    assert exc.value.code == INVALID_PARAMS
    assert "host" in exc.value.message


def test_read_rejects_empty_path(tmp_path: Path) -> None:
    provider = ResourceProvider(tmp_path)
    with pytest.raises(RpcError) as exc:
        provider.read("file://")
    assert exc.value.code == INVALID_PARAMS
    assert "missing a path" in exc.value.message


# =========================================================================
# read() — containment / suffix / existence
# =========================================================================


def test_read_rejects_path_traversal(tmp_path: Path) -> None:
    _write(tmp_path / "board.kicad_sch")
    outside = tmp_path.parent / f"{tmp_path.name}__sibling.kicad_sch"
    outside.write_text(_SCH_BODY, encoding="utf-8")
    try:
        provider = ResourceProvider(tmp_path)
        with pytest.raises(RpcError) as exc:
            provider.read(outside.resolve().as_uri())
        assert exc.value.code == INVALID_PARAMS
        assert "outside the project root" in exc.value.message
    finally:
        outside.unlink()


def test_read_rejects_parent_traversal_via_dotdot(tmp_path: Path) -> None:
    """``file:///project/../outside.kicad_sch`` must not escape the root."""
    outside = tmp_path.parent / f"{tmp_path.name}__dotdot.kicad_sch"
    outside.write_text(_SCH_BODY, encoding="utf-8")
    try:
        provider = ResourceProvider(tmp_path)
        crafted = f"file://{tmp_path.resolve().as_posix()}/../{outside.name}"
        with pytest.raises(RpcError) as exc:
            provider.read(crafted)
        assert exc.value.code == INVALID_PARAMS
        assert "outside the project root" in exc.value.message
    finally:
        outside.unlink()


def test_read_rejects_unsupported_extension(tmp_path: Path) -> None:
    (tmp_path / "secrets.txt").write_text("password: hunter2", encoding="utf-8")
    provider = ResourceProvider(tmp_path)
    with pytest.raises(RpcError) as exc:
        provider.read((tmp_path / "secrets.txt").resolve().as_uri())
    assert exc.value.code == INVALID_PARAMS
    assert "unsupported resource extension" in exc.value.message


def test_read_rejects_missing_file_with_distinct_error(tmp_path: Path) -> None:
    """Missing file ≠ out-of-root. Keeping the error separate avoids
    collapsing a real security rejection into a generic 'not found'."""
    provider = ResourceProvider(tmp_path)
    missing = (tmp_path / "ghost.kicad_sch").resolve().as_uri()
    with pytest.raises(RpcError) as exc:
        provider.read(missing)
    assert exc.value.code == INVALID_PARAMS
    assert exc.value.message == "resource not found"


def test_read_rejects_non_utf8_bytes(tmp_path: Path) -> None:
    bad = tmp_path / "bad.kicad_sch"
    bad.write_bytes(b"\xff\xfe\x00\x00not-utf-8")
    provider = ResourceProvider(tmp_path)
    with pytest.raises(RpcError) as exc:
        provider.read(bad.resolve().as_uri())
    assert exc.value.code == INVALID_PARAMS
    assert "UTF-8" in exc.value.message


def test_read_rejects_relative_path(tmp_path: Path) -> None:
    """``file:`` URIs must carry absolute paths — urlparse + Path enforce it."""
    provider = ResourceProvider(tmp_path)
    # Force a file URI where the path isn't absolute. Hand-crafted string
    # because ``Path.as_uri`` only accepts absolute paths.
    with pytest.raises(RpcError) as exc:
        provider.read("file:relative/path.kicad_sch")
    assert exc.value.code == INVALID_PARAMS


def test_read_rejects_directory_with_valid_suffix(tmp_path: Path) -> None:
    """A directory named like a KiCAD file isn't a file — not a resource."""
    (tmp_path / "looks.kicad_sch").mkdir()
    provider = ResourceProvider(tmp_path)
    with pytest.raises(RpcError) as exc:
        provider.read((tmp_path / "looks.kicad_sch").resolve().as_uri())
    assert exc.value.code == INVALID_PARAMS
    assert exc.value.message == "resource not found"
