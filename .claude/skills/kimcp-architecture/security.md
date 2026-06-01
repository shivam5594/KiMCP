# Security

The MCP runs on a user's machine with access to the filesystem, spawns `kicad-cli` subprocesses, talks protobuf-over-nng to KiCAD (see ADR-0015), and potentially reaches out to distributor and datasheet APIs. Small attack surface by file count, but broad by capability. This document is the policy.

## Threat model

Trusted:
- The user running the server.
- The installed KiCAD binary.

Untrusted:
- Project files (a malicious `.kicad_pcb` or `.kicad_sch` could be pointed at the parser).
- Symbol / footprint / 3D-model files from third parties.
- Datasheet PDFs from the open web.
- Responses from distributor / datasheet APIs.
- MCP clients themselves if auth is disabled.

## Principles

1. **Parse-but-do-not-execute.** S-expression inputs never evaluated; PDFs processed in a sandbox where possible.
2. **Subprocess inputs sanitized always.** No shell interpolation. Argument arrays only.
3. **Paths validated.** No traversal, no reading outside project/library roots.
4. **Secrets indirected.** Never in config plaintext.
5. **Auth defaults deny** on non-local transports.
6. **Allowlists over blocklists** where reasonable.

## Input validation

- Pydantic parses all tool inputs — reject on parse (see `schemas.md`).
- Path fields use a dedicated `SafePath` type that:
  - Resolves to an absolute path.
  - Rejects symlink escapes out of allowed roots.
  - Rejects embedded null bytes.
  - Canonicalizes (`..` resolved) before comparison.
- String fields have length caps.
- Numeric fields bounded (no unbounded sizes in exports).
- Enumerations exhaustive at the type level.

## Path policy

Allowed roots per session:
- The configured project root(s).
- The configured library roots (symbol/footprint/3D).
- The KiCAD-installed library root(s).
- `$KIMCP_TMP` (per-session temp dir).

Every filesystem operation computes `canonical(path).is_relative_to(allowed_root)` and refuses otherwise with `PathOutOfBounds`. The allowlist is derived at session-start from config; later adds require a new session.

## S-expression parser hardening

- Depth limit (default 256) — deeply nested inputs reject.
- Total-atoms limit (default 5M atoms) — prevents DoS via huge files.
- No eval, no dynamic dispatch based on atom names.
- Unknown tokens preserved as opaque to enable round-trip; never interpreted.
- Integer / float parsing via strict parsers, not `eval`.

## Subprocess handling

- `subprocess.run([...], shell=False)` only — no shell=True anywhere.
- `cwd` set explicitly.
- `env` whitelisted — never inherit arbitrary vars. KiCAD-relevant vars (`KISYSMOD`, `KICAD8_3DMODEL_DIR`, etc.) passed explicitly.
- Timeout enforced on every subprocess.
- stdout/stderr size-capped (kill on overflow).
- Subprocess failures mapped to typed errors; exit code never leaked as-is.

## Network / external APIs

- All outbound HTTPS only; TLS cert validation on by default.
- No outbound calls unless the relevant skill is enabled *and* credentials are configured.
- Domain allowlist for vendor/datasheet calls (e.g., `digikey.com`, `mouser.com`, `octopart.com`, manufacturer domains). Others require explicit config opt-in.
- Request timeouts; response size caps; rate limits respected.
- Responses validated against Pydantic models; unexpected fields logged, trusted ones extracted, rest ignored.
- Never execute code from a downloaded document. PDFs read via `pypdf`/`pdfminer`; no JavaScript / embedded-attachment handling.

## PDF handling

- Datasheet PDFs read server-side only for text/table/image extraction.
- Subprocess boundary where possible (PDF parser as a short-lived process with memory limits).
- Attachments, embedded files, and JavaScript streams in PDFs ignored.
- Downloaded PDFs hashed; hash recorded; tampered-cache files rejected on rehash mismatch.

## Secrets

- Secrets never in config plaintext (see `configuration.md`).
- Supported references: `env:`, `keychain:`, `file:` (with `0600` enforcement).
- In-memory secrets stored in a `SecretStr` that redacts on repr/logging.
- Zeroize on session end where the runtime permits.
- Rotation: reload on config reload for long-lived sessions.

## Auth

- STDIO transport: implicit trust (user-local process boundary).
- HTTP transport: **deny by default** unless `auth_mode` configured.
  - `token`: static bearer token from secret reference.
  - `oidc`: OIDC issuer-validated JWT; RBAC claims map to tool groups.
  - `mtls`: client cert pinning.
- Per-tool ACL: optional allowlist/denylist per role.
- Failed-auth events go to audit log.

## Authorization

- Mutating and destructive tools can be role-gated (`read` available to all, `mutate` to authors, `destructive` to leads). Implementation level; see `extensibility.md` for the ACL plugin point.

## Sandboxing

Process-level sandboxing where platform supports:
- Linux: `seccomp` syscall allowlist for PDF parser subprocess.
- macOS: `sandbox_init` with allow-read + allow-network-from-allowlist.
- Windows: AppContainer (future).

## Dependency hygiene

- `pip-audit` in CI.
- Renovate/Dependabot weekly.
- Pinned lockfile.
- Rust deps: `cargo audit` weekly.
- No unreviewed entry-point plugins by default (see `extensibility.md`).

## Disclosure

- Security issues reported to `security@<project>` (set during publication).
- 90-day coordinated disclosure default.
- `SECURITY.md` at repo root mirrors this policy short-form.

## Non-goals

- Confidentiality against the user running the server (they own the process).
- Multi-tenant isolation (one user per server process).
- Protecting against a malicious KiCAD install (trusted binary by assumption).
