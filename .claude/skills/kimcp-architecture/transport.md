# Transport Layer

## Contract

A transport implements two methods:

```
async read_message() -> dict          # next JSON-RPC message
async write_message(msg: dict)        # send a JSON-RPC message
```

The server's JSON-RPC handler sits above this, oblivious to whether bytes flow over stdio or an HTTP stream.

## STDIO transport

- Reads newline-delimited JSON from stdin.
- Writes newline-delimited JSON to stdout.
- stderr is reserved for logging — never JSON-RPC.
- Process lifetime == session lifetime.
- Default for local CLI integrations.

Edge cases:
- Partial lines → buffer until newline.
- Non-JSON stderr from a child (e.g., `kicad-cli`) never leaks onto stdout.
- SIGTERM / SIGINT → graceful drain of pending requests then exit.

## HTTP+SSE transport

- `POST /rpc` receives a JSON-RPC request; response is `text/event-stream` with one or more `event: message` frames.
- Streaming lets us send progress events (parsing, rendering) before the final result.
- Session keyed by `X-KiMCP-Session` header; absent header creates a new session with a UUID returned in the first response frame.
- Auth: bearer token in `Authorization` header. Tokens are opaque; the server authenticates via a pluggable auth adapter (default: static token from config; others: OIDC, mTLS).
- CORS: strictly opt-in per origin via config. Closed by default.

## Progress events

Long-running operations stream progress via SSE:

```
event: progress
data: {"tool": "export_gerber", "stage": "plotting", "pct": 37}
```

STDIO clients receive progress as notifications (`method: "$/progress"`). Clients that don't subscribe simply ignore them.

## Cancellation

- STDIO: `$/cancelRequest` notification with request id.
- HTTP+SSE: HTTP client disconnects the stream → server cancels.
- Cancellation tokens propagate through the dispatcher to backends. `kicad-cli` subprocesses receive SIGTERM then SIGKILL.

## Message size limits

- Default 64 MiB per message (configurable).
- Resource payloads above the limit must be fetched via chunked resource reads (`kimcp://...?range=...`).
- Gerbers, STEP, large PDFs are returned as resource handles, not inline bytes.

## Keepalive

- STDIO: no explicit keepalive.
- HTTP+SSE: server sends `: keepalive` comment every 20s to prevent proxy timeouts.

## Transport selection at runtime

Launcher accepts `--transport=stdio|http` with additional flags per mode. Tests exercise both transports against the same handler code.
