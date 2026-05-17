# CI Security Scanning

The Drone CI pipeline runs automated security scanning on every push and pull request (excluding main). This catches vulnerabilities before they reach production.

## Pipeline Steps

| Step | Tool | What it catches |
|------|------|-----------------|
| **lint-test** | ruff + pytest | Code errors, style violations, broken tests |
| **sast** | bandit + semgrep | Insecure code patterns (hardcoded secrets, injection, unsafe deserialization, etc.) |
| **dep-scan** | pip-audit + trivy | Known CVEs in dependencies, leaked secrets in files, misconfigurations |

## Why Each Tool

**Bandit** -- Python-specific static analysis. Knows about Python footguns like `eval()`, `pickle.loads()`, weak crypto, hardcoded passwords. Fast and focused.

**Semgrep** -- Pattern-based SAST with community rules. Catches broader issues bandit misses: logic bugs, framework-specific vulnerabilities, tainted data flows. Uses the `auto` config (curated OSS ruleset).

**pip-audit** -- Checks installed packages against the OSV vulnerability database (same source as GitHub Dependabot). Fails on any known vulnerability.

**Trivy** -- Scans the filesystem for HIGH/CRITICAL severity issues across three dimensions:
- `vuln`: dependency vulnerabilities (cross-references pip-audit but also catches non-Python deps)
- `secret`: hardcoded API keys, tokens, passwords in source files
- `misconfig`: insecure config patterns (Dockerfiles, IaC, etc.)

## Configuration

Tuning lives in `pyproject.toml`:

```toml
[tool.bandit]
exclude_dirs = ["tests", ".venv"]
skips = ["B101", "B112"]    # assert usage, try-except-continue
severity = "medium"          # only medium+ blocks CI

[tool.ruff.lint]
select = ["E", "F", "W", "I", "S", "B"]
```

## Severity Strategy

Each tool runs twice: first a **full report** (all severities, non-blocking) so you can see everything, then a **gate** that only fails the build on actionable severity:

| Tool | Report | Gate (blocks build) |
|------|--------|--------------------|
| Bandit | All severities | Medium+ only |
| Trivy | All severities | HIGH+CRITICAL only |
| Semgrep | All findings | Any finding |
| pip-audit | All CVEs | Any CVE |

This means low-severity bandit findings and medium trivy findings are **visible in build logs** but don't block merges. You can still track and fix them on your own schedule.

## When CI Fails

1. **Bandit/Semgrep finding** -- review the flagged code. If it's a false positive, suppress with `# nosec BXXX` (bandit) or `# nosec` / a `.semgrepignore` entry (semgrep)
2. **pip-audit finding** -- upgrade the vulnerable package. If no fix exists yet, track it and suppress temporarily
3. **Trivy secret** -- rotate the credential immediately, then remove it from source
4. **Trivy vuln** -- same as pip-audit (usually overlapping findings)

## Smart Skip (docs-only changes)

Not every change needs full scanning. The clone step computes `git diff` and writes a flag file to the shared workspace:

- If only docs, config, or non-code files changed: `SKIP_HEAVY=true`
- If `src/`, `tests/`, `requirements.txt`, or `pyproject.toml` changed: `SKIP_HEAVY=false`

Each heavy step (test, sast, dep-scan) sources the flag and exits immediately if no code changed. Lint always runs.

| Change type | Steps that run | Approx time |
|-------------|---------------|-------------|
| Docs only | clone + lint | ~15 seconds |
| Code change | All steps | ~10 minutes |

## Resource Usage

- `concurrency: limit: 1` -- only one pipeline runs at a time (no container spam)
- All steps use `python:3.13-alpine` (~50MB image)
- Total pipeline: ~4-5 minutes (full scan) or ~15 seconds (docs-only)
- Trivy binary downloaded on-the-fly (~40MB, cached by runner between runs)
- Semgrep uses OSS rules only (no cloud account or API key needed)
