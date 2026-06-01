# Fuzz tests

Fuzz corpora for the S-expression parser, Pydantic validation on every tool,
and path-sanitization logic. Nightly runs grow the corpus; crashes auto-file
GitHub issues.

Tests mark `@pytest.mark.fuzz`; skipped by default. See
`.claude/skills/kimcp-architecture/testing.md`.
