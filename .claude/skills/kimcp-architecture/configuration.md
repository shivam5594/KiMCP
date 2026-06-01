# Configuration

User-facing configuration surface. Centralized here so nothing else in the architecture needs to invent its own config model.

## Sources (merge order, later overrides earlier)

1. Built-in defaults (in-code).
2. User-global: `~/.config/kimcp/config.toml` (XDG on Linux/macOS, `%APPDATA%/KiMCP/config.toml` on Windows).
3. Project-local: `<project>/.kimcp/config.toml`.
4. Session overrides: passed on transport init (CLI flags for STDIO, HTTP headers for remote).
5. Per-call overrides: some tools accept a `config` parameter for one-shot overrides.

All config is TOML. JSON schema published so editors can offer autocomplete.

## Top-level sections

```
[server]
transport       = "stdio" | "http"
host            = "127.0.0.1"         # http only
port            = 8787                # http only
auth_mode       = "none" | "token" | "oidc" | "mtls"

[kicad]
kicad_exe       = "auto" | <path>     # probe if "auto"
cli_exe         = "auto" | <path>
preferred_backend_order = ["ipc", "sexpr", "cli", "swig"]
ipc_socket      = "auto" | <path>

[libraries]
symbol_lib_tables   = ["global", "project"]
footprint_lib_tables = ["global", "project"]
path_vars = { KIPRJMOD = "<auto>", KICAD_3DMODEL_DIR = "<auto>" }

[fab_profile]
preset          = "jlc_4-6_layer"     # or "custom"
# when preset = "custom", inline values override
min_trace       = 0.127
min_space       = 0.127
# ... see dfm/SKILL.md for the full set

[domain_knowledge]
strictness      = "hints" | "off" | "enforce"
enabled_skills  = ["signal-integrity", "power-integrity", "dfm", ...]
disabled_rules  = ["CAD-601", "SI-062"]   # with justification in audit log
severity_overrides = { "DFM-043" = "warn" }  # downgrade silk-over-pad from error

[safety]
dry_run_default = false
snapshot_mode   = "git" | "copy" | "off"   # "off" requires explicit acknowledge
destructive_confirmation = "always" | "session" | "never"
mass_rename_threshold = 20
snapshot_retention = 20               # for copy-mode snapshots

[performance]
cache_max_mb    = 256
file_watch      = true
parallel_exports = 4
rust_sexpr_parser = "auto"            # auto/on/off

[observability]
log_level       = "info"              # trace/debug/info/warn/error
log_path        = "~/.kimcp/logs/kimcp.log"
trace_enabled   = false
metrics_port    = 0                   # 0 = disabled

[external_apis]
# optional keys for vendor-search, datasheet-search, etc.
digikey_client_id     = ""
digikey_client_secret = ""
mouser_api_key        = ""
octopart_api_key      = ""
# keys MUST NOT live in config.toml directly — see security.md; instead reference an env var or OS keychain entry
# e.g. digikey_client_id = "env:KIMCP_DIGIKEY_CLIENT_ID"
# or   digikey_client_id = "keychain:kimcp/digikey_client_id"
```

## Fab profile presets

Shipped:
- `jlc_1-2_layer`
- `jlc_4-6_layer`
- `jlc_hdi`
- `pcbway_standard`
- `pcbway_advanced`
- `oshpark_4layer`
- `custom_high_speed`

Users can ship additional presets in `~/.config/kimcp/fab_profiles/<name>.toml`. Project can ship `<project>/.kimcp/fab_profiles/<name>.toml`.

## Strictness model

`domain_knowledge.strictness`:
- `off` — no validators run; tools execute the literal request. Never silently fail; the server still runs schema validation.
- `hints` (default) — validators run, emit `info` / `hint` suggestions; no refusals.
- `enforce` — validators run; severity `error` refusals block mutating tools unless `override_rule=[...]` is supplied with reason.

## Per-call overrides

Tools accept an optional `config` param that deep-merges into the effective config for that call only:

```
{
  "config": {
    "domain_knowledge": { "strictness": "enforce" },
    "safety": { "dry_run_default": true }
  }
}
```

Overrides are logged in the audit log (see `observability.md` + `safety.md`).

## Secrets handling

Secrets never appear in plaintext in config files. Supported indirections:

- `env:VAR_NAME` — read from process environment.
- `keychain:<service>/<account>` — OS keychain (macOS Keychain, Windows Credential Manager, Secret Service on Linux).
- `file:<path>` — read from a file (permissions must be `0600` or read is refused).

See `security.md` for full secrets policy.

## Validation

- Config is validated against a Pydantic model at startup.
- Unknown keys produce warnings, not errors, to allow forward/backward migration.
- Type errors produce actionable errors with file + line.
- `kimcp config validate` CLI command runs validation and prints a report.
- `kimcp config show --effective` prints the merged config with source annotations per key.

## Hot reload

- Non-runtime-critical sections (`domain_knowledge.strictness`, `safety.*` thresholds, `performance.cache_max_mb`) reload on SIGHUP (Unix) or a named-pipe signal (Windows).
- Transport/backend/KiCAD paths require restart.

## Migration

- Config schema is versioned (`version = 1` at the top of the file).
- Missing version treated as `1`.
- Forward migration: on load, if version < current, the server offers `kimcp config migrate --write` which upgrades the file in place and writes `.bak`.
