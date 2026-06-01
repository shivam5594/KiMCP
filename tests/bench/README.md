# Benchmark tests

Latency budgets from `.claude/skills/kimcp-architecture/performance.md` are
enforced here. CI compares current runs to a rolling baseline; >10% regression
on a named benchmark fails the run unless the PR carries a
`perf-regression-approved` label with justification.

Mark with `@pytest.mark.bench`. Run on a consistent machine profile
documented in `bench/README.md` (impl repo).
