<p align="center">
  <img src="https://img.shields.io/badge/KiCAD-9%2B-blue?style=for-the-badge&logo=kicad" alt="KiCAD 9+"/>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/MCP-2025--06--18-8A2BE2?style=for-the-badge" alt="MCP Protocol"/>
  <img src="https://img.shields.io/badge/license-Apache%202.0-green?style=for-the-badge" alt="License"/>
</p>

<h1 align="center">KiMCP</h1>

<p align="center">
  <strong>The Model Context Protocol server for KiCAD</strong><br/>
  Talk to your PCBs and schematics through AI — 46 tools, 4 backends, zero GUI required.
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> ·
  <a href="#-what-can-it-do">Features</a> ·
  <a href="#-architecture">Architecture</a> ·
  <a href="#-tool-catalog">Tools</a> ·
  <a href="#-contributing">Contributing</a>
</p>

---

## What is KiMCP?

**KiMCP** bridges [KiCAD](https://www.kicad.org/) — the world's most popular open-source EDA suite — with the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), enabling AI assistants like Claude to **read, analyze, modify, and export** your PCB designs and schematics programmatically.

Think of it as an API layer that lets an AI:
- Draw schematics from a text description
- Run DRC and ERC checks and explain violations
- Export Gerbers, drill files, and pick-and-place files for manufacturing
- Search and manage component libraries
- Review your board stackup and track layout

All without ever opening the KiCAD GUI.

---

## Why KiMCP?

| Pain Point | KiMCP Solution |
|---|---|
| Manual schematic entry is slow | AI places symbols, wires, and labels from natural language |
| DRC/ERC errors are cryptic | AI runs checks and explains every violation in context |
| Manufacturing output is error-prone | One command exports all Gerbers, drills, BOM, and placement files |
| Library management is tedious | Search, create, and register symbols and footprints programmatically |
| Design reviews take hours | AI reads the netlist, stackup, and layout — flags issues instantly |

---

## Quick Start

### Prerequisites

- **Python 3.11+**
- **KiCAD 9.0+** (KiCAD 10.x supported for stable IPC subset)

### Install

```bash
# Clone the repository
git clone https://github.com/shivam5594/KiMCP.git
cd KiMCP

# Create a virtual environment with Python 3.11+
python3.12 -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows

# Install in development mode
pip install -e ".[dev]"

# Verify installation
kimcp --help
```

> **Troubleshooting:** If you see `requires a different Python`, your default
> `python`/`pip` points to an older version. Create the venv explicitly with
> `python3.12` (or `python3.11` / `python3.13`) as shown above.

### Run the MCP Server

```bash
# Start the server (STDIO transport — for Claude Code / Claude Desktop)
uv run kimcp --transport stdio

# Or, if installed in an activated venv:
kimcp --transport stdio
```

### Connect to Claude Code

Create a `.mcp.json` in the project root (or add to your global MCP config).
Use `uv run` so the server launches correctly regardless of working directory:

```json
{
  "mcpServers": {
    "kimcp": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/path/to/KiMCP",
        "kimcp",
        "--transport", "stdio"
      ]
    }
  }
}
```

### Verify

```bash
# List all discovered tools
kimcp-cli tools list

# Run the test suite
pytest
```

---

## What Can It Do?

### Schematic Operations

| Tool | Description |
|---|---|
| `sch_add_symbol` | Place component symbols on a schematic sheet |
| `sch_add_wire` | Draw wires between pins |
| `sch_add_label` | Add net labels (local, global, hierarchical) |
| `sch_add_power` | Place power symbols (VCC, GND, etc.) |
| `sch_add_junction` | Add wire junctions |
| `sch_add_no_connect` | Mark unconnected pins |
| `sch_add_sheet` | Create hierarchical sub-sheets |
| `sch_compose` | Batch-compose multiple schematic elements atomically |
| `sch_delete` | Remove schematic elements |
| `sch_embed_lib_symbol` | Embed library symbols into the schematic |
| `sch_list_symbols` | List all symbols on a sheet |
| `sch_list_labels` | List all labels and their positions |
| `sch_list_nets` | Enumerate all nets in the design |
| `sch_list_wires` | List all wire segments |
| `sch_set_title_block` | Set title block metadata |
| `sch_erc` | Run Electrical Rules Check |
| `sch_export_pdf` | Export schematic to PDF |
| `sch_export_svg` | Export schematic to SVG |
| `sch_export_bom` | Export Bill of Materials |
| `sch_export_netlist` | Export netlist for layout |

### PCB Operations

| Tool | Description |
|---|---|
| `pcb_drc` | Run Design Rules Check |
| `pcb_drc_violations` | List DRC violations with details |
| `pcb_list_footprints` | List all footprints on the board |
| `pcb_list_tracks` | List all tracks with width and layer info |
| `pcb_get_stackup` | Read the board layer stackup |
| `pcb_export_gerbers` | Export Gerber files for fabrication |
| `pcb_export_drill` | Export drill/Excellon files |
| `pcb_export_step` | Export 3D STEP model |
| `pcb_export_pos` | Export pick-and-place position file |
| `pcb_export_pdf` | Export board layout to PDF |
| `pcb_export_svg` | Export board layout to SVG |
| `pcb_export_dxf` | Export board outline to DXF |

### Library Management

| Tool | Description |
|---|---|
| `lib_list_symbols` | List symbols in a library |
| `lib_list_footprints` | List footprints in a library |
| `lib_search_symbol` | Search for symbols by name/keyword |
| `lib_search_footprint` | Search for footprints by name/keyword |
| `lib_add_symbol` | Create new library symbols |
| `lib_add_footprint` | Create new library footprints |
| `lib_register_library` | Register a new symbol/footprint library |
| `lib_attach_3d_model` | Attach 3D models to footprints |

### Diagnostics

| Tool | Description |
|---|---|
| `ping` | Health check |
| `version` | Server version info |
| `config_show` | Show active configuration |
| `kicad_version` | Detect installed KiCAD version |
| `kicad_ipc_status` | Check IPC API connection status |
| `ipc_get_version` | Get KiCAD version via IPC protocol |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    MCP Client                       │
│            (Claude Code / Claude Desktop)           │
└──────────────────────┬──────────────────────────────┘
                       │ JSON-RPC over STDIO
┌──────────────────────▼──────────────────────────────┐
│                   KiMCP Server                      │
│  ┌─────────┐  ┌──────────┐  ┌────────────────────┐  │
│  │  Tools  │  │ Resources│  │     Prompts        │  │
│  │Registry │  │ Provider │  │    Registry        │  │
│  └────┬────┘  └──────────┘  └────────────────────┘  │
│       │                                             │
│  ┌────▼──────────────────────────────────────────┐  │
│  │         Four-Backend Dispatcher               │  │
│  │  ┌─────┐  ┌─────┐  ┌───────┐  ┌──────────┐   │  │
│  │  │ IPC │  │ CLI │  │ S-Expr│  │   SWIG   │   │  │
│  │  │proto│  │kicad│  │parser │  │  pcbnew  │   │  │
│  │  │ buf │  │-cli │  │       │  │          │   │  │
│  │  └──┬──┘  └──┬──┘  └───┬───┘  └────┬─────┘   │  │
│  └─────┼────────┼────────┼──────────┼────────┘  │
│        │        │        │          │            │
└────────┼────────┼────────┼──────────┼────────────┘
         │        │        │          │
    ┌────▼────────▼────────▼──────────▼────┐
    │            KiCAD 9+ / 10.x           │
    │   .kicad_sch  .kicad_pcb  libraries  │
    └──────────────────────────────────────┘
```

### Four-Backend Dispatcher

KiMCP doesn't lock you into a single way of talking to KiCAD. Each tool declares which backends can service it, and the dispatcher picks the best available one at runtime:

| Backend | Mechanism | Best For |
|---|---|---|
| **IPC** | Protobuf over nng sockets | Live mutations while GUI is open |
| **CLI** | `kicad-cli` subprocess | DRC, exports, batch operations |
| **S-Expr** | Native `.kicad_sch`/`.kicad_pcb` parser | Schematic read/write without KiCAD running |
| **SWIG** | `pcbnew` Python bindings | Direct board manipulation (future) |

### Safety by Default

- **`dry_run=true`** on every mutating tool — preview changes before they land
- **Automatic snapshots** (git or file copy) before destructive writes
- **Audit logging** — every mutation is recorded with timestamp and input summary
- **Confirmation tokens** for irreversible operations

### Plugin Architecture

Tools are discovered via Python entry points — drop a package into your environment and KiMCP finds it:

```toml
# In your plugin's pyproject.toml
[project.entry-points."kimcp.tools"]
my_custom_tool = "my_package:MyCustomTool"
```

---

## Configuration

KiMCP uses a layered configuration system (environment → global → project-local):

```bash
# Point to a custom KiCAD installation
export KIMCP_KICAD_CLI_EXE="/usr/bin/kicad-cli"

# Or use a project-local config file
# .kimcp.toml in your project root
```

Key configuration options:

| Option | Default | Description |
|---|---|---|
| `kicad.cli_exe` | auto-detect | Path to `kicad-cli` executable |
| `kicad.min_version` | `9.0` | Minimum supported KiCAD version |
| `safety.snapshot_every_n_calls` | `5` | Snapshot cadence for mutations |
| `safety.audit_enabled` | `true` | Enable audit logging |
| `performance.file_watch` | `true` | Watch files for cache invalidation |
| `performance.cache_max_mb` | `64` | S-expression parse cache size |

---

## Project Status

> **Pre-Alpha (v0.0.0)** — Actively under development

### What's Working

- Full MCP server with STDIO transport
- 46 built-in tools across schematic, PCB, library, and diagnostics
- Four-backend dispatcher with runtime probe and fallback
- S-expression parser/writer with round-trip fidelity
- File-watching cache with stat-based invalidation
- Safety layer: dry-run, snapshots, audit logging
- Pydantic v2 schemas for all tool inputs/outputs
- Comprehensive test suite (119 test files — unit, integration, e2e, property-based, golden)
- MCP resources (project file discovery) and prompts (design review, manufacturing handoff)

### Roadmap

- [ ] HTTP + SSE transport
- [ ] Domain-knowledge engine (signal integrity, DFM, power integrity suggestions)
- [ ] Full KiCAD 10 IPC coverage (pending `kicad-python` 0.7)
- [ ] SWIG `pcbnew` backend
- [ ] Plugin marketplace
- [ ] Batch/pipeline operations
- [ ] VS Code extension

---

## Development

```bash
# Install with all dev dependencies
pip install -e ".[dev,ipc]"

# Run the full test suite
pytest

# Run only unit tests
pytest tests/unit/

# Run with coverage
coverage run -m pytest && coverage report

# Linting and type checking
ruff check src/ tests/
mypy src/
```

### Test Layers

| Layer | Count | Purpose |
|---|---|---|
| Unit | 80+ | Pure logic, mocked backends |
| Integration | 6 | Real `kicad-cli` invocations |
| E2E | 30+ | Full transport → tool → backend round-trips |
| Property | 4 | Hypothesis-based invariant testing |
| Golden | 1 | S-expression round-trip fidelity |

---

## Supported Platforms

| Platform | Status |
|---|---|
| macOS (Apple Silicon & Intel) | Supported |
| Linux (x86_64, aarch64) | Supported |
| Windows | Supported |

---

## Contributing

Contributions are welcome! Whether it's a bug fix, new tool, documentation improvement, or a new backend — we'd love your help.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-tool`)
3. Write tests for your changes
4. Ensure `pytest`, `ruff check`, and `mypy` all pass
5. Submit a pull request

Please see the architecture documentation in `.claude/skills/kimcp-architecture/` for design decisions and conventions.

---

## License

[Apache 2.0](LICENSE) — use it freely in personal and commercial projects.

---

<p align="center">
  <strong>Built for hardware engineers who want AI to understand their boards.</strong><br/>
  <sub>If KiMCP helps your workflow, consider starring the repo — it helps others find it.</sub>
</p>
