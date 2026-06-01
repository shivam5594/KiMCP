# Golden-file tests

S-expression parser round-trip: `read → modify → write → re-read → re-modify
→ write`. The result must equal the first-write byte-for-byte for untouched
sections.

Fixtures land under `tests/fixtures/projects/` at M1 when the parser arrives.
See `.claude/skills/kimcp-architecture/testing.md` for the full strategy.

Mark tests with `@pytest.mark.golden`.
