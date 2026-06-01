# Resources & Prompts

MCP exposes two non-tool surfaces: resources and prompts. Both are first-class here.

## Resources

Structured, addressable, cacheable views into project state.

### URI scheme

```
kimcp://project/{project_id}
kimcp://project/{project_id}/schematic[/{sheet_path}]
kimcp://project/{project_id}/schematic/{sheet_path}/components
kimcp://project/{project_id}/schematic/{sheet_path}/nets
kimcp://project/{project_id}/pcb
kimcp://project/{project_id}/pcb/layers
kimcp://project/{project_id}/pcb/footprints
kimcp://project/{project_id}/pcb/nets[/{net_name}]
kimcp://project/{project_id}/pcb/drc_report
kimcp://project/{project_id}/netlist
kimcp://project/{project_id}/bom
kimcp://project/{project_id}/snapshots
kimcp://library/symbol/{lib}
kimcp://library/symbol/{lib}/{symbol}
kimcp://library/footprint/{lib}
kimcp://library/footprint/{lib}/{footprint}
kimcp://library/3d/{path}
kimcp://calc/{calculator_name}
```

### Reading

- Each resource returns JSON (default) or a more specific mime type (SVG for plots, application/pdf for renders).
- Query params:
  - `include=a,b,c` — sparse selection of sub-sections.
  - `range=bytes=0-1048575` — byte range for large payloads.
  - `fields=name,value,position` — project only named fields.
- Resources expose `etag` and `last-modified`; clients use `If-None-Match` / `If-Modified-Since` for cheap revalidation.

### Writing

Resources are read-only. All mutations go through tools — this keeps the write audit trail consistent.

### Subscribing

Clients may subscribe to a resource URI. The server sends a notification when the underlying state changes (via file watcher or IPC event). Default-off to avoid noisy streams.

## Prompts

Canned workflows that chain tools with domain-knowledge checks. Prompts are *templates*, not free-form text.

### Built-in prompts

- `new-project-wizard`: ask for board type (digital / mixed-signal / RF / power), layer count, fab profile → scaffold project, stackup, netclasses, drawing sheet.
- `design-review`: runs every applicable `check_*` / `validate_*` tool, cross-references domain skills, produces a structured review report.
- `manufacturing-handoff`: DRC clean → Gerber + drill + position + BOM + 3D STEP + assembly drawing → zip → attach fab profile + PCB stackup PDF → produce handoff package.
- `component-swap`: given old MPN + reason, find alternates (via `vendor-search` + `datasheet-search` + `3d-models-and-footprints-search`), compare footprints, propose swap plan with risks.
- `signal-routing-plan`: for a given net or differential pair, propose routing constraints (width, clearance, length match) sourced from `signal-integrity` skill and the current stackup.
- `decoupling-plan`: for a given IC, propose decoupling scheme (count, values, placement rules) sourced from `power-integrity` skill and the IC's datasheet.
- `dfm-review`: runs `check_dfm` against a configured fab-capability profile; highlights all violations with fix hints.

### Prompt structure

A prompt is declared as:

```
name: str
description: str
arguments: [ {name, description, required, type} ]
```

The server expands the prompt into a sequence of tool calls, streaming progress. Clients can "step through" by invoking one stage at a time.

### Extensibility

Prompts, like tools, register via entry points. A project can add its own prompts without forking the core.

## Relationship to tools

- Resources expose state; tools change state or compute.
- Prompts compose tools.
- The domain-knowledge engine runs across all three — a suggestion attached to a tool output can reference a resource URI ("see `kimcp://.../pcb/nets/DDR_CK`") and recommend a prompt ("run `signal-routing-plan` on this net").
