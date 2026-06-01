"""Unit tests for ``kimcp.logging_config``.

These tests pin the JSON log schema from ``observability.md`` — any
regression here means downstream log consumers (Thread C forensics, audit
tooling) would see missing or mis-shaped fields. Also exercises the
idempotency contract ``configure_logging`` offers so repeat CLI invocations
and pytest's re-use of the root logger don't accumulate duplicate handlers.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections.abc import Iterator
from pathlib import Path

import pytest

from kimcp.config import ObservabilityCfg
from kimcp.logging_config import TRACE, JsonFormatter, configure_logging


@pytest.fixture
def isolated_root_logger() -> Iterator[None]:
    """Save/restore the root logger so tests don't bleed into each other.

    ``configure_logging`` mutates the root logger's handlers + level.
    Without this fixture the first test to run would leave handlers on
    the root logger that subsequent tests (e.g. ``caplog`` users elsewhere
    in the suite) would inherit and fight with.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        # Tear down whatever configure_logging added, then splat the
        # original handlers back in.
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


# -- TRACE level registration ----------------------------------------------


def test_trace_level_is_registered_below_debug() -> None:
    """TRACE=5 is below DEBUG=10, per observability.md's rung ordering."""
    assert TRACE == 5
    assert TRACE < logging.DEBUG
    assert logging.getLevelName(TRACE) == "TRACE"


# -- JsonFormatter schema --------------------------------------------------


def _format_record(
    *,
    level: int = logging.INFO,
    msg: str = "hello",
    extra: dict[str, object] | None = None,
    exc_info: tuple[type[BaseException], BaseException, object] | None = None,
) -> dict[str, object]:
    """Build a LogRecord, run it through JsonFormatter, parse the JSON."""
    record = logging.LogRecord(
        name="kimcp.test",
        level=level,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=None,
        exc_info=exc_info,
    )
    # Stdlib applies ``extra={}`` at .log() time; replicate the effect.
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    line = JsonFormatter().format(record)
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return parsed


def test_json_formatter_emits_required_fields_even_when_absent() -> None:
    """observability.md: every event carries the full correlation shape.

    Missing values are ``null`` — not absent — so a log consumer can
    always index into the same keys regardless of which call site fired.
    """
    payload = _format_record()
    for required in (
        "ts",
        "level",
        "event",
        "session_id",
        "tool",
        "request_id",
        "duration_ms",
        "backend",
        "error",
        "msg",
    ):
        assert required in payload, f"missing required field {required!r}"
    # When the caller didn't set them, the correlation keys must be null.
    assert payload["session_id"] is None
    assert payload["tool"] is None
    assert payload["request_id"] is None
    assert payload["duration_ms"] is None
    assert payload["backend"] is None
    assert payload["error"] is None


def test_json_formatter_uses_iso_utc_with_z_suffix() -> None:
    """Timestamps must be ISO-8601 UTC ending in ``Z`` per spec."""
    payload = _format_record()
    ts = payload["ts"]
    assert isinstance(ts, str)
    assert ts.endswith("Z")
    # ms precision: ``2026-04-16T12:34:56.789Z`` → 24 chars.
    assert len(ts) == 24


def test_json_formatter_lowercases_level() -> None:
    """Level names are lowercase — matches observability.md sample events."""
    assert _format_record(level=logging.WARNING)["level"] == "warning"
    assert _format_record(level=logging.ERROR)["level"] == "error"
    assert _format_record(level=TRACE)["level"] == "trace"


def test_json_formatter_promotes_documented_extras() -> None:
    """Top-level correlation keys come out of ``extra={}`` onto the root."""
    payload = _format_record(
        extra={
            "event": "tool.start",
            "session_id": "sess-1",
            "tool": "sch_erc",
            "request_id": 42,
            "duration_ms": 17,
            "backend": "sexpr",
            "error": None,
        },
    )
    assert payload["event"] == "tool.start"
    assert payload["session_id"] == "sess-1"
    assert payload["tool"] == "sch_erc"
    assert payload["request_id"] == 42
    assert payload["duration_ms"] == 17
    assert payload["backend"] == "sexpr"
    # Nothing leaks into extras — those were all promoted.
    assert "extras" not in payload


def test_json_formatter_buckets_unknown_extras() -> None:
    """Non-schema extras land under ``extras`` so they aren't dropped."""
    payload = _format_record(
        extra={
            "event": "tool.end",
            "custom_metric": 3.14,
            "trace_id": "abc-123",
        },
    )
    assert payload["event"] == "tool.end"
    assert payload["extras"] == {"custom_metric": 3.14, "trace_id": "abc-123"}


def test_json_formatter_defaults_event_to_logger_name() -> None:
    """If the caller didn't set ``event``, fall back to the logger name.

    Keeps old-style ``log.info("something")`` call sites searchable by
    module name while the codebase migrates to explicit event strings.
    """
    payload = _format_record()
    assert payload["event"] == "kimcp.test"


def test_json_formatter_captures_exception_info() -> None:
    """Exceptions serialise into ``exc`` as a formatted traceback."""
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    assert exc_info[0] is not None
    payload = _format_record(level=logging.ERROR, exc_info=exc_info)
    assert "exc" in payload
    assert "ValueError" in str(payload["exc"])
    assert "boom" in str(payload["exc"])


def test_json_formatter_json_is_single_line() -> None:
    """One event per line — log aggregators split on newlines."""
    record = logging.LogRecord(
        name="kimcp.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="multi\nline\nmessage",
        args=None,
        exc_info=None,
    )
    line = JsonFormatter().format(record)
    # The formatted output itself must contain zero newlines — ``msg``
    # internals are JSON-escaped (``\n``) not emitted raw.
    assert "\n" not in line
    payload = json.loads(line)
    # But the original newlines survive round-trip through JSON.
    assert payload["msg"] == "multi\nline\nmessage"


def test_json_formatter_uses_ensure_ascii_false() -> None:
    """Non-ASCII characters pass through — don't mangle user data."""
    record = logging.LogRecord(
        name="kimcp.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="résumé ✓",
        args=None,
        exc_info=None,
    )
    line = JsonFormatter().format(record)
    assert "résumé ✓" in line


# -- configure_logging behaviour -------------------------------------------


def test_configure_logging_installs_stderr_handler(
    isolated_root_logger: None,
) -> None:
    """Stderr is the mandatory sink on STDIO transport (stdout is JSON-RPC)."""
    configure_logging(ObservabilityCfg())
    root = logging.getLogger()
    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert stream_handlers, "expected at least one StreamHandler on root"
    # Exactly one stream handler after a default call.
    assert len(stream_handlers) == 1
    import sys

    assert stream_handlers[0].stream is sys.stderr


def test_configure_logging_is_idempotent(isolated_root_logger: None) -> None:
    """Repeat calls must replace handlers, not stack them.

    Tests and repeat CLI invocations (think REPL reloads) would otherwise
    duplicate every log line once per prior call.
    """
    cfg = ObservabilityCfg()
    configure_logging(cfg)
    configure_logging(cfg)
    configure_logging(cfg)
    root = logging.getLogger()
    # Exactly one handler: the stderr sink. Not three.
    assert len(root.handlers) == 1


def test_configure_logging_level_respects_config(isolated_root_logger: None) -> None:
    """``log_level`` from config drives both root and handler levels."""
    configure_logging(ObservabilityCfg(log_level="debug"))
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert root.handlers[0].level == logging.DEBUG


def test_configure_logging_override_level_beats_config(
    isolated_root_logger: None,
) -> None:
    """--log-level CLI arg wins over config's log_level."""
    configure_logging(ObservabilityCfg(log_level="info"), override_level="trace")
    root = logging.getLogger()
    assert root.level == TRACE


def test_configure_logging_trace_level_is_selectable(
    isolated_root_logger: None,
) -> None:
    """trace is a first-class choice on the CLI + in config.

    Without this the custom TRACE level wouldn't be reachable through
    normal config paths — you could only raise it by calling
    ``logger.log(TRACE, ...)`` directly.
    """
    configure_logging(ObservabilityCfg(log_level="trace"))
    assert logging.getLogger().level == TRACE


def test_configure_logging_warn_is_synonym_for_warning(
    isolated_root_logger: None,
) -> None:
    """observability.md uses "warn"; stdlib calls it WARNING — both resolve."""
    configure_logging(ObservabilityCfg(log_level="warn"))
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_text_format_is_default(
    isolated_root_logger: None,
) -> None:
    """Default format is "text" — dev-friendly, not JSON."""
    configure_logging(ObservabilityCfg())
    root = logging.getLogger()
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)


def test_configure_logging_json_format_switches_formatter(
    isolated_root_logger: None,
) -> None:
    """log_format="json" plugs in JsonFormatter on every handler."""
    configure_logging(ObservabilityCfg(log_format="json"))
    root = logging.getLogger()
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_configure_logging_no_file_handler_by_default(
    isolated_root_logger: None,
) -> None:
    """Empty log_path keeps the filesystem untouched — safe for tests + CI."""
    configure_logging(ObservabilityCfg())
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert file_handlers == []


def test_configure_logging_adds_rotating_file_handler_when_path_set(
    isolated_root_logger: None,
    tmp_path: Path,
) -> None:
    """Non-empty log_path wires a RotatingFileHandler per observability.md.

    Pins the 10MB / 5-file defaults too — flipping those silently would
    change on-disk disk usage + retention across every deployment.
    """
    log_path = tmp_path / "kimcp.log"
    configure_logging(ObservabilityCfg(log_path=str(log_path)))
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    fh = file_handlers[0]
    assert Path(fh.baseFilename) == log_path
    assert fh.maxBytes == 10_000_000
    assert fh.backupCount == 5


def test_configure_logging_creates_missing_parent_dir(
    isolated_root_logger: None,
    tmp_path: Path,
) -> None:
    """Deep log_path auto-creates its parent directory.

    Operators set log_path=~/.kimcp/logs/kimcp.log and expect it to Just
    Work on first boot — not require a mkdir step.
    """
    nested = tmp_path / "deep" / "nested" / "kimcp.log"
    configure_logging(ObservabilityCfg(log_path=str(nested)))
    assert nested.parent.is_dir()


def test_configure_logging_file_writes_after_setup(
    isolated_root_logger: None,
    tmp_path: Path,
) -> None:
    """End-to-end: a log call actually lands in the file sink."""
    log_path = tmp_path / "kimcp.log"
    configure_logging(ObservabilityCfg(log_path=str(log_path), log_format="json"))
    logging.getLogger("kimcp.test").info("hello-from-test", extra={"event": "t.t"})
    # Force flush via the handlers (they're line-buffered by default,
    # but a flush here makes the assert deterministic on all platforms).
    for h in logging.getLogger().handlers:
        h.flush()
    contents = log_path.read_text(encoding="utf-8")
    assert "hello-from-test" in contents
    # JSON formatter means one JSON object per line.
    payload = json.loads(contents.splitlines()[-1])
    assert payload["event"] == "t.t"
    assert payload["msg"] == "hello-from-test"


def test_configure_logging_unusable_log_path_does_not_crash(
    isolated_root_logger: None,
    tmp_path: Path,
) -> None:
    """A log_path we can't write must not block server boot.

    Falls back to stderr-only and logs an error — matches the
    ``observability.md`` contract that logging failures never take the
    server down.
    """
    # Point log_path at a file whose parent would fail to mkdir because
    # a *file* already occupies that parent name. ``mkdir(parents=True)``
    # raises FileExistsError (an OSError subclass).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    broken = blocker / "kimcp.log"  # blocker is a file, not a dir
    # Should not raise:
    configure_logging(ObservabilityCfg(log_path=str(broken)))
    root = logging.getLogger()
    # Stderr sink still wired, no file handler.
    assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert file_handlers == []


def test_configure_logging_expands_tilde(
    isolated_root_logger: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``~/path`` in log_path expands to the user's home — spec requirement.

    Operators paste config snippets like ``log_path = "~/.kimcp/logs/kimcp.log"``;
    if we didn't expanduser this, they'd get a literal ``./~/.kimcp/...`` file
    in the project directory.
    """
    # Redirect $HOME so the test doesn't actually write under the real home.
    monkeypatch.setenv("HOME", str(tmp_path))
    configure_logging(ObservabilityCfg(log_path="~/logs/kimcp.log"))
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename) == tmp_path / "logs" / "kimcp.log"
