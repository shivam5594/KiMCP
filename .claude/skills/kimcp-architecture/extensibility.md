# Extensibility

Tools, resources, prompts, backends, auth adapters, fab profiles, and domain rules all extensible. Plugin SDK defined here.

## Plugin types

| Type | Entry-point group | Contract |
|---|---|---|
| Tool | `kimcp.tools` | subclass of `Tool` with `InputModel`, `OutputModel`, `execute()` |
| Resource handler | `kimcp.resources` | subclass of `ResourceHandler` with `uri_pattern`, `read()` |
| Prompt | `kimcp.prompts` | subclass of `Prompt` with `arguments`, `expand()` |
| Backend | `kimcp.backends` | subclass of `Backend` with `name`, capability flags, `execute_op()` |
| Auth adapter | `kimcp.auth` | subclass of `AuthAdapter` with `authenticate()`, `authorize()` |
| Fab profile | `kimcp.fab_profiles` | `FabProfile` instance with name + fields (see `configuration.md`) |
| Domain rule set | `kimcp.rules` | subclass of `RuleSet` with id prefix, `rules` list |

Entry points declared per-package:

```
[project.entry-points."kimcp.tools"]
my_tool = "my_package.tools:MyTool"

[project.entry-points."kimcp.rules"]
my_rules = "my_package.rules:MyRuleSet"
```

## Tool contract

```
class MyTool(Tool):
    name = "my_tool"
    version = "1.0.0"
    description = "One-liner for MCP clients."

    class Input(BaseModel):
        foo: str = Field(..., description="...")

    class Output(BaseModel):
        result: int

    InputModel = Input
    OutputModel = Output

    required_backends = {Backend.SEXPR, Backend.IPC}
    preferred = [Backend.SEXPR, Backend.IPC]
    live_gui_visible = False
    mutates = True
    destructive = False

    async def execute(self, inp: Input, ctx: ToolContext) -> Output:
        ...
```

`ToolContext` gives access to:
- `ctx.backend(name)` — selected backend adapter
- `ctx.resource(uri)` — addressable state
- `ctx.project` — active project handle
- `ctx.config` — effective configuration
- `ctx.suggest(rule_id, severity, message, why, fix_hint, refs)` — append a suggestion
- `ctx.snapshot()` — take a snapshot before mutation (auto-called for destructive tools)
- `ctx.emit_progress(stage, pct)` — progress event

## Rule set contract

```
class MyRuleSet(RuleSet):
    id_prefix = "MYX-"
    skill_name = "my-external-skill"

    rules = [
        Rule(
            id="MYX-001",
            applicability=..., # predicate over project state
            check=...,         # returns bool or (bool, details)
            severity="warn",
            message="...",
            why="...",
            fix_hint="...",
            references=["AN-xxx"],
        ),
        ...
    ]
```

Rules compiled once per session. Results cached per `(project_hash, rule_id)`.

## Backend contract

```
class MyBackend(Backend):
    name = "mybackend"
    capabilities = {
        Capability.READ_SCH,
        Capability.WRITE_SCH,
        ...
    }

    async def probe(self) -> bool: ...
    async def execute_op(self, op: Operation) -> OperationResult: ...
```

Backends are rarely added; adding one requires an ADR (see `backends.md`).

## Discovery & load order

1. Core loads first (registers built-ins).
2. Entry points discovered via `importlib.metadata`.
3. User-global plugin dir (`~/.kimcp/plugins/`) scanned for `.py` files exporting `register(registry)` — opt-in with `KIMCP_LOAD_LOCAL_PLUGINS=1`.
4. Project-local plugin dir (`<project>/.kimcp/plugins/`) scanned last — opt-in per project (`plugins_enabled = true`).

Duplicate tool names across plugins: last-loaded wins with a `warn` log. Project-local always wins over user-global.

## Plugin trust

- Entry-point plugins execute on import. Default: **only plugins in pip-installed packages** are trusted.
- Local-dir plugins require explicit opt-in.
- `kimcp plugins list` prints every loaded plugin with provenance.
- `kimcp plugins disable <name>` adds a block entry.

## Versioning & compatibility

- Plugins declare `kimcp_api_version` they target (semver range).
- Core refuses to load plugins with incompatible ranges.
- Deprecations:
  - Core API changes never silent.
  - Deprecated hooks stay one minor version with `DeprecationWarning`.
  - Plugin-contract breakage documented in the core `CHANGELOG.md`.

## Config contributions

Plugins can register configuration sections under namespaced keys:
```
[plugins.my_package]
my_setting = 42
```
Pydantic models for those sections declared in the plugin.

## Rule-skill pairing

A plugin rule set can point at a sibling skill file that lives outside the repo — suggestions will reference the skill by name. Users then decide whether to install that skill locally.

## Testing plugins

- Plugin authors ship their own tests; we publish a test harness (`kimcp.testing`) exposing fixtures: `project_fixture`, `make_context`, `fake_backend`.
- CI of core runs a contract suite against every built-in plugin to detect silent breakage.

## Anti-patterns

- Tools that mutate global state at import time.
- Plugins that monkey-patch the core.
- Rules without stable IDs (IDs are the user's handle for override/audit).
- Backends that ignore the dispatcher's `live_gui_visible` / `mutates` flags.
