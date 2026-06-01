"""Unit tests for the audit-log dispatch integration (M32).

The pieces under test live in `kimcp.server`:

* ``Server._handle_tools_call`` — the single dispatch path. After a tool
  completes, it either writes a line to ``<project>/.kimcp/audit.log``
  (via ``kimcp.safety.audit.record``) or doesn't, based on policy.
* ``Server._should_audit`` — the policy gate. READ calls skip by default,
  everything else logs by default, and ``audit_enabled=False`` suppresses
  all of them.
* ``_summarize_input`` / ``_summarize_value`` — module-level helpers that
  shape raw ``arguments`` into a compact, JSON-safe summary. We don't
  want the audit log to accidentally become a data dump.

Tests stick to synthetic tools so the dispatch seam is exercised in
isolation — regressions in per-tool behavior shouldn't flip these
results. The snapshot path is also stubbed: the ``_SnapshottingMutateTool``
just writes ``output.meta.snapshot_ref`` directly rather than invoking
the real ``snapshot()`` helper. That keeps this file independent of
``safety.snapshot`` internals.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import BaseModel

from kimcp._types import ToolClass
from kimcp.config import load_config
from kimcp.safety.audit import audit_log_path
from kimcp.schemas.envelope import ToolOutput
from kimcp.server import Server, _summarize_input, _summarize_value
from kimcp.tools.base import Tool

# ---------------------------------------------------------------------------
# Synthetic tools
# ---------------------------------------------------------------------------


class _PassThroughInput(BaseModel):
    payload: str = ""
    count: int = 0
    flags: list[str] = []
    options: dict[str, str] = {}


class _PassThroughOutput(ToolOutput):
    status: Literal["ok"] = "ok"


class _ReadTool(Tool[_PassThroughInput, _PassThroughOutput]):
    name = "_read_probe"
    version = "0.0.1"
    description = "Test-only READ tool."
    input_model = _PassThroughInput
    output_model = _PassThroughOutput
    classification = ToolClass.READ
    preferred_backends = ()

    async def run(self, input: _PassThroughInput) -> _PassThroughOutput:
        return _PassThroughOutput()


class _MutateTool(Tool[_PassThroughInput, _PassThroughOutput]):
    name = "_mutate_probe"
    version = "0.0.1"
    description = "Test-only MUTATE tool."
    input_model = _PassThroughInput
    output_model = _PassThroughOutput
    classification = ToolClass.MUTATE
    mutates = True
    preferred_backends = ()

    async def run(self, input: _PassThroughInput) -> _PassThroughOutput:
        out = _PassThroughOutput()
        out.meta.snapshot_ref = "copy:/tmp/fake-snap"
        return out


class _DestructiveTool(Tool[_PassThroughInput, _PassThroughOutput]):
    name = "_destructive_probe"
    version = "0.0.1"
    description = "Test-only DESTRUCTIVE tool."
    input_model = _PassThroughInput
    output_model = _PassThroughOutput
    classification = ToolClass.DESTRUCTIVE
    mutates = True
    destructive = True
    preferred_backends = ()

    async def run(self, input: _PassThroughInput) -> _PassThroughOutput:
        return _PassThroughOutput()


class _ExternalTool(Tool[_PassThroughInput, _PassThroughOutput]):
    name = "_external_probe"
    version = "0.0.1"
    description = "Test-only EXTERNAL tool."
    input_model = _PassThroughInput
    output_model = _PassThroughOutput
    classification = ToolClass.EXTERNAL
    preferred_backends = ()

    async def run(self, input: _PassThroughInput) -> _PassThroughOutput:
        return _PassThroughOutput()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _server(tmp_path: Path, **safety_overrides: object) -> Server:
    """Build a Server rooted at ``tmp_path`` with optional safety overrides."""
    cfg = load_config(
        user_global=tmp_path / "__no_u.toml",
        project_local=tmp_path / "__no_p.toml",
        session_overrides={"safety": safety_overrides} if safety_overrides else None,
    )
    return Server(config=cfg, project_root=tmp_path)


async def _call(
    server: Server,
    tool_name: str,
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    params = {"name": tool_name, "arguments": arguments or {}}
    raw = await server._handle_tools_call(params)
    return cast(dict[str, object], raw["structuredContent"])


def _read_entries(project: Path) -> list[dict[str, object]]:
    path = audit_log_path(project)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Dispatch-side policy: which classifications get audited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutate_tool_writes_audit_entry(tmp_path: Path) -> None:
    """MUTATE calls drop a line — baseline for the feature."""
    server = _server(tmp_path)
    server.register_tool(_MutateTool())

    await _call(server, "_mutate_probe", {"payload": "hello"})

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["tool"] == "_mutate_probe"
    assert entries[0]["input_summary"] == {
        "payload": "hello",
        # defaults fall through because `model_validate` doesn't invent them
        # on the raw arguments dict — only what the caller passed.
    }
    assert entries[0]["snapshot_ref"] == "copy:/tmp/fake-snap"


@pytest.mark.asyncio
async def test_destructive_tool_writes_audit_entry(tmp_path: Path) -> None:
    server = _server(tmp_path)
    server.register_tool(_DestructiveTool())

    await _call(server, "_destructive_probe")

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["tool"] == "_destructive_probe"


@pytest.mark.asyncio
async def test_external_tool_writes_audit_entry(tmp_path: Path) -> None:
    server = _server(tmp_path)
    server.register_tool(_ExternalTool())

    await _call(server, "_external_probe")

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["tool"] == "_external_probe"


@pytest.mark.asyncio
async def test_read_tool_default_skips_audit(tmp_path: Path) -> None:
    """READ calls are high-volume; skip by default."""
    server = _server(tmp_path)
    server.register_tool(_ReadTool())

    await _call(server, "_read_probe")

    # No log file created at all.
    assert not audit_log_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_read_tool_audited_when_opted_in(tmp_path: Path) -> None:
    """`audit_read_tools=True` brings READ calls into the log."""
    server = _server(tmp_path, audit_read_tools=True)
    server.register_tool(_ReadTool())

    await _call(server, "_read_probe", {"payload": "listed"})

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["tool"] == "_read_probe"
    assert entries[0]["input_summary"]["payload"] == "listed"


@pytest.mark.asyncio
async def test_audit_disabled_suppresses_everything(tmp_path: Path) -> None:
    """`audit_enabled=False` is the master kill-switch."""
    server = _server(tmp_path, audit_enabled=False, audit_read_tools=True)
    server.register_tool(_MutateTool())
    server.register_tool(_ReadTool())

    await _call(server, "_mutate_probe")
    await _call(server, "_read_probe")

    assert not audit_log_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# Dispatch-side failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_failure_skips_audit(tmp_path: Path) -> None:
    """Input validation fails → no audit entry (the tool never ran)."""
    server = _server(tmp_path)
    server.register_tool(_MutateTool())

    # `count` must be an int — passing a string trips Pydantic.
    with pytest.raises(Exception):  # noqa: B017
        await _call(server, "_mutate_probe", {"count": "not-an-int"})

    assert not audit_log_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_audit_write_failure_does_not_break_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError on audit write is logged but doesn't propagate.

    The legitimate tool result must survive a broken disk. Simulate it
    by monkeypatching ``audit_record`` to raise; the dispatcher should
    still return the tool's envelope verbatim.
    """
    server = _server(tmp_path)
    server.register_tool(_MutateTool())

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("kimcp.server.audit_record", _boom)

    result = await _call(server, "_mutate_probe")

    # Envelope came back clean.
    assert result["status"] == "ok"
    # No audit file left behind.
    assert not audit_log_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_multiple_calls_append(tmp_path: Path) -> None:
    """Each call adds a fresh line — JSONL, not truncated on each write."""
    server = _server(tmp_path)
    server.register_tool(_MutateTool())

    await _call(server, "_mutate_probe", {"payload": "first"})
    await _call(server, "_mutate_probe", {"payload": "second"})
    await _call(server, "_mutate_probe", {"payload": "third"})

    entries = _read_entries(tmp_path)
    assert [e["input_summary"]["payload"] for e in entries] == [
        "first",
        "second",
        "third",
    ]


# ---------------------------------------------------------------------------
# Audit entry shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_entry_has_expected_keys(tmp_path: Path) -> None:
    server = _server(tmp_path)
    server.register_tool(_MutateTool())

    await _call(server, "_mutate_probe", {"payload": "p"})

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    # Contract with safety.audit.record — these five keys are the stable
    # schema every downstream compliance tool reads.
    assert set(entry.keys()) == {"ts", "tool", "input_summary", "snapshot_ref", "note"}
    assert entry["note"] == ""
    assert isinstance(entry["ts"], str)
    # Timestamp is UTC ISO-8601 with Z suffix per safety.audit.record.
    assert entry["ts"].endswith("Z")


@pytest.mark.asyncio
async def test_snapshot_ref_omitted_when_tool_did_not_snapshot(
    tmp_path: Path,
) -> None:
    """A mutate tool that skipped the snapshot (dry_run) leaves snapshot_ref null."""
    server = _server(tmp_path)
    server.register_tool(_DestructiveTool())

    await _call(server, "_destructive_probe")

    entries = _read_entries(tmp_path)
    assert entries[0]["snapshot_ref"] is None


# ---------------------------------------------------------------------------
# _should_audit — policy matrix, directly
# ---------------------------------------------------------------------------


def test_should_audit_matrix(tmp_path: Path) -> None:
    """Each cell of the ``(classification, config)`` policy table."""

    def run(
        classification: ToolClass,
        *,
        enabled: bool = True,
        read_tools: bool = False,
    ) -> bool:
        server = _server(
            tmp_path,
            audit_enabled=enabled,
            audit_read_tools=read_tools,
        )

        class _T(Tool[_PassThroughInput, _PassThroughOutput]):
            name = "x"
            input_model = _PassThroughInput
            output_model = _PassThroughOutput

            async def run(self, input: _PassThroughInput) -> _PassThroughOutput:
                return _PassThroughOutput()

        _T.classification = classification
        return server._should_audit(_T())

    # disabled master switch always wins
    assert run(ToolClass.MUTATE, enabled=False) is False
    assert run(ToolClass.READ, enabled=False, read_tools=True) is False

    # enabled: MUTATE / DESTRUCTIVE / EXTERNAL always audited
    assert run(ToolClass.MUTATE) is True
    assert run(ToolClass.DESTRUCTIVE) is True
    assert run(ToolClass.EXTERNAL) is True

    # enabled: READ gated by read_tools toggle
    assert run(ToolClass.READ) is False
    assert run(ToolClass.READ, read_tools=True) is True


# ---------------------------------------------------------------------------
# _summarize_input / _summarize_value — pure-function tests
# ---------------------------------------------------------------------------


def test_summarize_passes_primitives_through() -> None:
    assert _summarize_value(None) is None
    assert _summarize_value(True) is True
    assert _summarize_value(42) == 42
    assert _summarize_value(3.14) == 3.14
    assert _summarize_value("hi") == "hi"


def test_summarize_truncates_long_strings() -> None:
    """Strings over the 160-char limit get ellipsized."""
    value = "A" * 500
    summarized = _summarize_value(value)
    assert isinstance(summarized, str)
    assert len(summarized) == 160
    assert summarized.endswith("...")


def test_summarize_preserves_short_strings() -> None:
    value = "A" * 160
    assert _summarize_value(value) == value


def test_summarize_list_hides_contents() -> None:
    """Lists become length markers — never raw dumps."""
    assert _summarize_value([1, 2, 3, 4]) == {"_type": "list", "len": 4}


def test_summarize_dict_recurses() -> None:
    """Nested dicts get the same treatment (str truncation, list markers)."""
    value = {
        "name": "small",
        "big": "B" * 500,
        "items": [1, 2, 3],
    }
    out = _summarize_value(value)
    assert isinstance(out, dict)
    assert out["name"] == "small"
    assert isinstance(out["big"], str)
    assert out["big"].endswith("...")
    assert out["items"] == {"_type": "list", "len": 3}


def test_summarize_bytes_hides_contents() -> None:
    assert _summarize_value(b"\x00" * 1024) == {"_type": "bytes", "len": 1024}


def test_summarize_unknown_types_have_repr_marker() -> None:
    """Path et al. fall through to a type-name + repr marker. JSON-safe."""
    p = Path("/tmp/x.kicad_sch")
    out = _summarize_value(p)
    assert isinstance(out, dict)
    assert out["_type"] == "PosixPath" or out["_type"] == "WindowsPath"
    assert "x.kicad_sch" in cast(str, out["repr"])


def test_summarize_input_top_level_keys() -> None:
    """The outer dict keeps its keys so audit readers can grep by field."""
    args = {"path": "x.sch", "dry_run": True, "letters": ["a", "b"]}
    summary = _summarize_input(args)
    assert set(summary.keys()) == {"path", "dry_run", "letters"}
    assert summary["dry_run"] is True
    assert summary["letters"] == {"_type": "list", "len": 2}
