# Integration tests

Per `.claude/skills/kimcp-architecture/testing.md`, integration tests run
against **real** backends — `kicad-cli`, SWIG `pcbnew`, and the IPC API of a
running KiCAD instance.

Scope arrives in later milestones:

| Milestone | Integration surface landing here |
|-----------|----------------------------------|
| M1        | S-expression parser round-trip against fixture projects |
| M2        | `kicad-cli` probe + export tools |
| M3        | IPC API client + interactive mutation tools |
| M5        | SWIG gap-filler paths |

Mark tests with `@pytest.mark.integration`. They are skipped by default; run
with `pytest -m integration`.
