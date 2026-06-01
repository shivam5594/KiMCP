# Schemas

## Source of truth: Pydantic v2

All tool inputs, outputs, and resource payloads are Pydantic models. JSON Schema exposed to MCP clients is *generated* from the Pydantic models — never written by hand (see ADR-0006).

## Anatomy of a tool

A tool is a class (or callable) that declares:

```
name: str                              # stable, snake_case
version: semver                        # per-tool version
description: str                       # one-liner; shown to MCP clients
InputModel: type[BaseModel]            # Pydantic input schema
OutputModel: type[BaseModel]           # Pydantic output schema
required_backends: set[Backend]
preferred: list[Backend]
live_gui_visible: bool
mutates: bool
destructive: bool
deprecated_in: semver | None           # None = not deprecated
remove_in: semver | None
```

Input / output models ship with `Field(..., description=…, examples=…)`. Descriptions are extracted into the MCP JSON Schema so clients (and humans) can understand each parameter.

## Standard envelope for outputs

Every tool output embeds a `meta` sub-object:

```
meta: {
  backend_used: "ipc" | "cli" | "sexpr" | "swig",
  live_sync: bool,                      # did the GUI update, if applicable?
  duration_ms: int,
  warnings: list[str],
  suggestions: list[Suggestion],        # from domain-knowledge engine
  snapshot_ref: str | None              # git commit / snapshot dir if one was taken
}
```

Suggestion:

```
{
  rule_id: "SI-014",                    # stable id from the sibling skill
  skill: "signal-integrity",
  severity: "info" | "hint" | "warn" | "error",
  message: str,
  why: str,                             # the reasoning, citing the rule
  fix_hint: str,
  references: list[str]                 # URLs, standard names, datasheet refs
}
```

## Units and enumerations

- All lengths in **millimeters** (float). Inputs may accept `"1.5mm"` / `"60mil"` strings; they are parsed to mm and the canonical form returned.
- All angles in **degrees**.
- All currents in **amperes**, voltages in **volts**, temperatures in **°C**.
- Layer names use KiCAD canonical names (`"F.Cu"`, `"In1.Cu"`, `"B.Silkscreen"`, …).
- Ref designators follow IEEE 315 prefixes — never enforced on existing projects, only warned via the domain-knowledge engine.

## Validation pipeline

On each tool call:

1. Parse raw JSON → Pydantic `InputModel`. Pydantic raises rich errors mapped to JSON-RPC error codes.
2. Domain-knowledge pre-validators run (opt-in per tool). Can short-circuit with `severity=error`.
3. Backend executes.
4. Result wrapped in `OutputModel` + envelope.
5. Post-validators run (e.g., "did we produce a valid Gerber?" checks).

## Versioning & deprecation

- Semver per tool.
- Breaking change → new major → parallel tools briefly (`export_gerber` v1 and v2) where useful.
- `deprecated_in` / `remove_in` surfaced in `meta.warnings`.
- JSON Schema documents include `x-deprecated-in`, `x-remove-in` extensions so clients can surface warnings.
- Removals require an ADR.

## Error codes

JSON-RPC error space:

- `-32600` to `-32700` reserved by JSON-RPC spec.
- `-32000` validation error (Pydantic).
- `-32001` backend unavailable.
- `-32002` destructive op refused (no snapshot storage writable).
- `-32003` KiCAD version incompatible for this operation.
- `-32004` rule violation (when a domain-knowledge pre-validator short-circuits).
- Details object carries `rule_id`, `suggestion` fields where applicable.

## Resource payloads

Resources (`project`, `schematic`, `pcb`, `library`, `netlist`, `drc_report`, `erc_report`) expose structured payloads. Large resources support partial reads (e.g., `?sheet=root&include=components`). Resource URIs follow:

```
kimcp://project/{project_id}
kimcp://project/{project_id}/schematic/{sheet_path}
kimcp://project/{project_id}/pcb
kimcp://project/{project_id}/pcb/nets/{net_name}
kimcp://library/symbol/{lib}/{symbol}
kimcp://library/footprint/{lib}/{footprint}
```

## Schema stability promise

- Adding a field with a default → minor.
- Adding a required field → major.
- Removing a field → major.
- Tightening validation (narrower type) → major.
- Loosening validation → minor.
- Renaming → major, with an alias for one minor version.
