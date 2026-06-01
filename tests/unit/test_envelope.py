"""Unit tests for the output envelope."""

from __future__ import annotations

from kimcp._types import Backend, Severity
from kimcp.schemas.envelope import Meta, Suggestion, ToolOutput


class _DemoOutput(ToolOutput):
    value: int


def test_tool_output_has_meta_by_default() -> None:
    out = _DemoOutput(value=42)
    assert out.meta.backend_used is None
    assert out.meta.live_sync is True
    assert out.meta.suggestions == []
    assert out.meta.warnings == []
    assert out.meta.duration_ms == 0


def test_meta_round_trip_json() -> None:
    meta = Meta(
        backend_used=Backend.IPC,
        live_sync=False,
        duration_ms=123,
        warnings=["snapshot-skipped"],
        suggestions=[
            Suggestion(
                rule_id="SI-014",
                skill="signal-integrity",
                severity=Severity.WARN,
                message="Return path discontinuity",
                why="Trace crosses a plane split — see SI-014",
                fix_hint="Add stitching cap near crossing",
                references=["IPC-2221"],
            )
        ],
        snapshot_ref="git:abc123",
    )
    data = meta.model_dump(mode="json")
    restored = Meta.model_validate(data)
    assert restored == meta


def test_suggestion_is_frozen() -> None:
    s = Suggestion(
        rule_id="CAD-001",
        skill="electrical-cad-best-practices",
        severity=Severity.INFO,
        message="m",
        why="w",
        fix_hint="",
    )
    # frozen=True prevents mutation
    try:
        s.severity = Severity.ERROR
    except Exception:
        return
    raise AssertionError("Suggestion should be frozen")


def test_tool_output_exposes_json_schema() -> None:
    schema = _DemoOutput.model_json_schema()
    assert "meta" in schema["properties"]
    assert "value" in schema["properties"]
