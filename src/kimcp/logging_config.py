"""Logging configuration driven by `ObservabilityCfg`.

`observability.md` spec:

* Structured JSON is the default for long-running hosts; text is the human-
  readable fallback for local dev and tests.
* STDIO transport reserves stdout for JSON-RPC, so logs always go to stderr
  (plus an optional rotating file sink).
* Every event carries the correlation fields
  ``ts / level / event / session_id / tool / request_id / duration_ms /
  backend / error`` — absent fields serialise as ``null`` so log consumers
  can rely on a stable shape.

This module is deliberately stdlib-only (``logging`` + ``logging.handlers``
+ ``json``). The observability.md vision calls for structlog + OpenTelemetry
+ Prometheus, but those live behind optional-extras we're not ready to ship
yet; the JSON formatter here covers the 80 % that matters for Thread C
forensics, and keeps the server bootable on a bare Python install.

Usage:

    from kimcp.logging_config import configure_logging
    from kimcp.config import Config

    cfg = Config()
    configure_logging(cfg.observability)

Callers that want to decorate their log events with correlation data pass
``extra={}`` through the stdlib logger API:

    log.info("tool call start", extra={"event": "tool.start", "tool": name})

The JSON formatter picks those keys out of the ``LogRecord`` and places
them at the event's top level. Extras not listed in the schema flow
through into an ``extras`` sub-object so nothing is silently dropped.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from kimcp.config import ObservabilityCfg

# Mapping config strings to stdlib levels. "trace" is a custom level (5)
# below DEBUG (10). `observability.md` reserves "trace" for raw backend
# I/O, which is too noisy to ship under DEBUG but needs a dedicated rung.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

_LEVEL_MAP: dict[str, int] = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

# Log-record attributes already owned by stdlib ``logging``. Anything passed
# via ``extra={}`` ends up in ``record.__dict__``, and the JSON formatter
# wants to treat only the "new" extras as correlation metadata. We subtract
# the stdlib set so callers don't accidentally have their custom fields
# collide with ``pathname`` / ``filename`` / ``funcName``.
_STDLIB_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord(
        "name", logging.INFO, "pathname", 0, "msg", None, None
    ).__dict__.keys()
)

# Correlation fields documented in observability.md. Everything in this set
# is promoted to the event's top level; anything else goes under ``extras``.
_TOP_LEVEL_EXTRAS: frozenset[str] = frozenset(
    {
        "event",
        "session_id",
        "tool",
        "request_id",
        "duration_ms",
        "backend",
        "error",
    }
)

_TEXT_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_TEXT_DATEFMT = "%Y-%m-%dT%H:%M:%S"

# Rotating file defaults from observability.md: 10 MB x 5 files.
_ROTATE_MAX_BYTES = 10_000_000
_ROTATE_BACKUP_COUNT = 5


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Shape matches `observability.md` exactly. Fields that haven't been
    populated on the record serialise as ``null`` so the schema is stable
    — a downstream consumer can always index into the same keys regardless
    of the event type.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = _dt.datetime.fromtimestamp(record.created, _dt.UTC).isoformat(
            timespec="milliseconds"
        )
        # strip the trailing "+00:00" for the Z-suffix the spec requires
        if ts.endswith("+00:00"):
            ts = ts[: -len("+00:00")] + "Z"

        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname.lower(),
            "event": getattr(record, "event", record.name),
            "session_id": getattr(record, "session_id", None),
            "tool": getattr(record, "tool", None),
            "request_id": getattr(record, "request_id", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "backend": getattr(record, "backend", None),
            "error": getattr(record, "error", None),
            "msg": record.getMessage(),
        }

        # Any extras the caller threw in that aren't stdlib + aren't already
        # top-level get bundled. Keeps "kitchen-sink logging" cheap without
        # leaking them into the schema's first level.
        extras: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in _STDLIB_ATTRS or key in _TOP_LEVEL_EXTRAS:
                continue
            extras[key] = value
        if extras:
            payload["extras"] = extras

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    cfg: ObservabilityCfg,
    *,
    override_level: str | None = None,
) -> None:
    """Apply ``ObservabilityCfg`` to the root logger.

    Idempotent — existing handlers are dropped first so tests and repeat
    CLI invocations don't accumulate duplicates.

    Args:
        cfg: Observability config (normally ``Config().observability``).
        override_level: CLI-provided ``--log-level`` beats the config's
            ``log_level``. This is the escape hatch that lets operators
            flip to ``debug`` without editing a config file.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    level_name = (override_level or cfg.log_level).lower()
    level = _LEVEL_MAP.get(level_name, logging.INFO)
    root.setLevel(level)

    formatter: logging.Formatter
    if cfg.log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(_TEXT_FMT, datefmt=_TEXT_DATEFMT)

    # stderr sink is mandatory. stdout is reserved for JSON-RPC on STDIO
    # transport (transport.md §Stdio); logging there would corrupt the
    # JSON-RPC stream an MCP client is parsing.
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.setLevel(level)
    root.addHandler(stream)

    # Optional file sink — only wired when the caller set a non-empty path.
    # Empty-string default keeps tests and first-run users from surprise-
    # writing to ``~/.kimcp/logs/kimcp.log``.
    if cfg.log_path:
        path = Path(cfg.log_path).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=_ROTATE_MAX_BYTES,
                backupCount=_ROTATE_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            root.addHandler(file_handler)
        except OSError as exc:
            # A misconfigured log_path must not crash the server at boot —
            # surface the failure on stderr and keep running with the
            # stream-only sink.
            root.error("log_path %s unusable: %s — continuing on stderr", path, exc)


__all__ = ["TRACE", "JsonFormatter", "configure_logging"]
