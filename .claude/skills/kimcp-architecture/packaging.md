# Packaging & Distribution

How KiMCP ships. No schedules here — see `DECISIONS.md` ADR-0011.

## Primary: PyPI wheel + sdist

- Package name: `kimcp`.
- Console script entry points: `kimcp` (server) and `kimcp-cli` (admin utilities).
- Pure-Python wheel unless Rust extension is built; then platform-tagged wheels via `cibuildwheel`.
- `sdist` always uploaded alongside wheels for reproducibility.

## Rust extension (optional)

- Separate optional extra: `pip install "kimcp[fast]"` installs prebuilt wheels for supported platforms.
- On platforms without prebuilt wheels, pip falls back to source build — which requires Rust toolchain. Clear error at install time if toolchain absent.
- Pure-Python fallback always available and automatically selected when extension is missing.

## macOS

- Homebrew tap: `brew install <tap>/kimcp/kimcp`.
- Universal2 wheel (arm64 + x86_64) via `cibuildwheel`.
- No code signing required for library; CLI binary signed with Developer ID if distributed outside Homebrew.

## Windows

- MSI installer via `msix` or `wixtools` — optional, lower priority than pip.
- `winget` manifest when stable.

## Linux

- `pip` primary.
- `.deb` and `.rpm` via `fpm` packaging for distro users who prefer system packaging.
- Arch AUR package maintained by community if interest emerges.

## Docker

- `<registry>/kimcp:<version>` image.
- Base: `python:3.11-slim`.
- KiCAD CLI installed in the image (Ubuntu-based variant for kicad-cli availability).
- Runs server in HTTP mode by default; STDIO mode via entrypoint flag.
- `-slim` variant without KiCAD CLI for users who mount their own KiCAD install.

## VS Code / JetBrains extensions

- Not a packaging target for the core. Extensions are client-side integrations that launch the `kimcp` server via stdio. Maintained in separate repos under the same org.

## Claude Code plugin

- Manifest at `<registry>/kimcp-claude-plugin` registering the server with Claude Code's MCP integration.
- Pins a compatible `kimcp` version range.

## KiCAD plugin (IPC complement)

- Optional action-plugin shipped separately that opens an IPC channel to the `kimcp` server from inside the KiCAD GUI.
- Not required — `kimcp` works against any KiCAD install via IPC/CLI/SWIG.

## Versioning

- Semver.
- Major: backend-dispatcher contract change, removal of a tool, config schema breaking change.
- Minor: new tool, new skill integration, new backend, non-breaking config additions.
- Patch: bugfix, doc, perf.

Supported window: latest major + previous major for one year after a new major.

## Release process

1. Changelog updated (`CHANGELOG.md`, keep-a-changelog format).
2. Version bumped (single-source in `pyproject.toml`, exposed via `kimcp.__version__`).
3. Tag `v<version>` pushed → CI builds wheels, runs full matrix, publishes to test PyPI.
4. Manual smoke tests on test PyPI install.
5. Promote to PyPI via workflow.
6. Docker + brew workflows triggered from the tag.
7. Release notes published on GitHub with downloads.

## Reproducibility

- Pinned `uv.lock` / `poetry.lock` per release.
- `pyproject.toml` locks `build-system` versions.
- CI builds are deterministic (timestamps pinned via `SOURCE_DATE_EPOCH`).
- Wheel hashes published in release notes; `pip install --require-hashes` supported.

## Dependencies

- No unvetted dependencies. Each new dep requires justification in PR description.
- Avoid adding dependencies that fight Pydantic v2 or asyncio.
- Rust extension keeps dep tree minimal; no heavy parser frameworks.

## Support matrix

Declared in `pyproject.toml` classifiers + a `SUPPORT.md` doc:
- Python: oldest supported minor + current.
- OS: macOS 13+, Windows 10+, Ubuntu 22.04+, Debian 12+.
- KiCAD: two most recent majors.

Dropping support for a platform/Python/KiCAD version requires ADR + 90-day deprecation notice.

## Telemetry

None by default. If opt-in telemetry is ever added it requires ADR, is off by default, and documents exactly what is sent and where.
