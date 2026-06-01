# Observability

The server is trustworthy only if what it did is visible. Three pillars: **logs**, **traces**, **metrics**. Plus an **audit log** for destructive ops (already referenced by `safety.md`).

## Logs

- Structured JSON by default (`structlog`). One event per line.
- Levels: `trace`, `debug`, `info`, `warn`, `error`, `critical`.
- Default level `info` — surfaces tool calls, backend selection, warnings. Nothing chatty at `info`.
- `debug` adds parsed arguments, selection-matrix reasoning, cache hits.
- `trace` adds raw backend I/O (IPC frames, CLI stdout). Never on by default — verbose and may include project filenames.

Required fields per event:
```
ts:           ISO-8601 UTC
level:        str
event:        str                  # stable short name ("tool.start", "backend.select", ...)
session_id:   str
tool:         str | null
request_id:   str | null
duration_ms:  int | null
backend:      str | null
error:        { type, message, rule_id? } | null
```

Logs rotate (`RotatingFileHandler`, 10 MB × 5 by default). On HTTP transport, stdout is free for logs; on STDIO transport, logs go to stderr or a configured file — never stdout.

## Traces

- OpenTelemetry-compatible tracing.
- One span per tool call; child spans per backend call, per validator, per cache lookup.
- Exporter pluggable: OTLP, Zipkin, Jaeger, stdout-JSON.
- Off by default; enabled via `observability.trace_enabled=true` + exporter config.
- Traces sampled (default: 5% of successful calls, 100% of errors, 100% of destructive ops).

Span naming:
- `tool.<tool_name>`
- `backend.<backend_name>.<op>`
- `validator.<rule_id>`
- `cache.<resource_uri>`
- `snapshot.<mode>`

## Metrics

- Prometheus-style metrics on an optional port (`observability.metrics_port`).

Counters:
- `kimcp_tool_calls_total{tool, backend, outcome}`
- `kimcp_backend_errors_total{backend, error_type}`
- `kimcp_rule_violations_total{rule_id, severity}`
- `kimcp_snapshots_total{mode, trigger}`

Histograms:
- `kimcp_tool_duration_seconds{tool, backend}`
- `kimcp_cache_lookup_duration_seconds{cache}`
- `kimcp_parse_duration_seconds{file_type}`

Gauges:
- `kimcp_cache_bytes`
- `kimcp_sessions_active`
- `kimcp_ipc_connected` (0/1)

## Audit log

Per `safety.md`. Append-only. One line per destructive op or override:
```
ts | session | user_id | tool | destructive | snapshot_ref | confirmation_token | justification | input_summary
```

Separate file from operational logs; not rotated by default (records are small and legally-relevant).

## Correlation IDs

- `session_id`: stable per MCP connection.
- `request_id`: JSON-RPC request id, propagated through logs + traces + metrics.
- `snapshot_ref`: stable across all logs tied to a destructive op.

## Debugging tools

- `kimcp debug dump-session <session_id>` — extracts all logs/traces for a session into a single file for bug reports.
- `kimcp debug explain-selection <tool>` — prints the dispatcher's selection reasoning for a tool in the current environment.
- `kimcp debug rules --skill signal-integrity --dry-run <project>` — lists which rules would fire on a project without mutating.
- `kimcp debug cache --stats` / `--clear`.

## Errors

- Errors always include `rule_id` when produced by a validator.
- Errors include a "nearest source" for parse errors (file + line + column).
- Stack traces only at `debug` level or above; users see a clean message.
- Typed error codes (see `schemas.md`) surface in logs/metrics/responses consistently.

## Privacy

- File paths truncated in default-level logs to the project-relative form (`./schematic.kicad_sch` not `/Users/shivam/Desktop/.../schematic.kicad_sch`) unless `log_level=trace`.
- Datasheet URLs logged; content never logged.
- External API responses logged at length-bounded; secrets never logged.
- `debug dump-session` offers `--redact-paths` / `--redact-content` for sharing with third parties.

## Runtime visibility

- HTTP transport: `GET /healthz` (liveness), `GET /readyz` (backends available).
- `GET /status` returns structured status: backends probed, KiCAD version, cache stats, last N errors. STDIO equivalent via `server.status` method.

## Anti-patterns

- `print()` anywhere in production code — always structured logger.
- Logging Pydantic models with sensitive data via repr — explicit field selection.
- Silent swallowing of exceptions — always logged with context and rule_id.
- Log spamming at `info` — if it fires > 10/sec in normal use, it's `debug`.
