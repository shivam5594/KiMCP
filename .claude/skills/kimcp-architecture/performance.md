# Performance

Performance is a feature. The user called out speed as a reason existing MCPs fail.

## Measurement first

No optimization without a baseline. Every performance change must cite a before/after number on a named benchmark.

Benchmark suite (lives in `bench/` in the implementation repo):

- `parse_large_pcb` — a representative 8-layer, ~2000-component board.
- `parse_hierarchical_schematic` — 10-deep hierarchy, ~500 nets.
- `drc_medium_board` — DRC on a 4-layer, ~500-component board.
- `export_gerber` — full stackup Gerber generation.
- `bulk_rename_nets` — rename 100 nets across schematic + PCB.
- `list_components_paged` — 10 000-component artificial board.

Results in JSON, tracked over time, regressions fail CI beyond a configurable threshold.

## Strategies

### Caching

- Parsed representations keyed by `(absolute_path, mtime, sha256)`.
- LRU cache with size cap (default: 256 MiB).
- File-watched invalidation (`watchdog`) — cache entries evict on detected change.
- Write paths must update the cache entry (avoid re-read after write).

### Lazy parsing

- S-expression parsing returns a lazy tree. Children are parsed on access.
- Accessing `schematic.components` parses the component section only; hierarchy sheets parse on descent.
- Serialization preserves unread sections verbatim — round-trip fidelity without full parse.

### Parallelism

- Asyncio everywhere in the server.
- Backend calls that release the GIL (subprocess) concurrent by default.
- Independent batch operations (export across all sheets, render thumbnails) fan out via `asyncio.gather` with a bounded semaphore.

### Streaming

- Large outputs (Gerbers, STEP, PDFs) stream bytes as they are produced.
- Progress events sent via transport (see `transport.md`).
- Never build a 100 MiB response in memory to `return` it.

### Rust where it pays

- S-expression parser in Rust via PyO3 — opt-in, selected at runtime based on availability.
- Gerber writer in Rust when profile-justified.
- Nothing else until measured.

### Connection pooling

- One long-lived `pynng.Req0` socket per KiCAD instance for IPC API (not gRPC — see ADR-0015).
- `kicad-cli` process startup is 200-500 ms cold — pool a warm subprocess when feasible.

### Cheap wins

- Avoid `json.dumps` → `json.loads` round-trips; pass dicts through internally.
- Precompile regexes at import time.
- Prefer `orjson` for JSON serialization on large payloads.
- Pydantic v2 `model_dump_json` beats `json.dumps(model.dict())`.

## Budget

Target latencies (p50, warm cache):

| Op | Target |
|---|---|
| Tool listing | < 5 ms |
| `list_components` (small board) | < 20 ms |
| `get_net_at_point` | < 20 ms |
| `run_drc` (small board) | < 2 s |
| `export_gerber` (small board) | < 5 s |
| `parse_large_pcb` cold | < 1 s |
| `parse_large_pcb` warm | < 20 ms |

Failing a budget is not automatically a bug, but it is automatically a review gate.

## Anti-patterns

- Re-parsing a file within a single tool call.
- Loading an entire board to list its component count.
- Holding a cache entry without invalidation.
- Shelling out to `kicad-cli` for an operation the S-expr parser can do.
- Synchronous blocking in async paths.
