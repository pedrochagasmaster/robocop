# Plan 008: Add lint and typecheck gates (ruff + mypy on `dispatch/`)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- pyproject.toml AGENTS.md`
> If either changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

There is no lint, format, or typecheck config anywhere in the repo, yet the
code carries orphaned `# pylint: disable=` pragmas (`dispatch/runner.py:2`,
`dispatch/manifest.py:2`, four `scr/*.py` files) and `# flake8: noqa` markers
that no runner ever checks. This hurts in two ways: style drift is unchecked,
and — critically for this repo — every plan written by the improve skill
*needs* machine-checkable lint/typecheck commands to cite as verification
gates. Without them, plans fall back to "compileall + pytest" which misses
whole classes of regressions a typecheck would catch.

This plan adds `ruff` (lint + format) and `mypy` (typecheck) scoped to
`dispatch/` and `tests/`, configured in `pyproject.toml`, and wires them into
`AGENTS.md` Validation so agents and humans have a canonical gate.

## Current state

`pyproject.toml` (28 lines total) — no `[tool.*]` sections beyond build/package:

```
[build-system]
requires = ["setuptools>=65"]
build-backend = "setuptools.build_meta"

[project]
name = "dispatch"
version = "1.0.0"
...
dependencies = ["textual==8.2.5"]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]
```

Orphaned pragmas (examples): `dispatch/runner.py:2`
(`# pylint: disable=global-statement`), `dispatch/manifest.py:2`
(`# pylint: disable=too-many-arguments,...`), `scr/Query_Impala_Parametrized.py:2`
(`# pylint: disable=...`), `scr/_common.py:1` (`# pylint: disable=...`),
`dispatch/manifest.py:218` area (`# flake8: noqa`).

`AGENTS.md:93-109` — Validation lists `compileall`, `dispatch --help`, mock
smoke; no lint/typecheck.

**Repo conventions**: `pyproject.toml` is the single config home (no
`setup.cfg`, no standalone linter configs). `scr/` is frozen-API per ADR-0005
and its files start with `# flake8: noqa` — leave `scr/` lint-silent to avoid
touching production orchestrators. Scope lint/typecheck to `dispatch/` and
`tests/` only.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Install dev | `pip install -e ".[dev]"` + `pip install ruff mypy` | exit 0 |
| Lint      | `ruff check dispatch tests`      | exit 0 (after fixing) |
| Format    | `ruff format --check dispatch tests` | exit 0 |
| Typecheck | `mypy dispatch`                  | exit 0 (after fixing) |

Add `ruff` and `mypy` to `[project.optional-dependencies] dev` so future
`pip install -e ".[dev]"` includes them.

## Scope

**In scope**:
- `pyproject.toml` — add `[tool.ruff]`, `[tool.mypy]`, and `ruff`/`mypy` to
  the `dev` extra.
- `AGENTS.md` — add lint/typecheck commands to the Validation section.
- `dispatch/` and `tests/` — fix only what ruff/mypy flag as errors on the
  current code. Do NOT do a stylistic rewrite; fix the minimum to get a clean
  gate.

**Out of scope**:
- `scr/` — frozen per ADR-0005. Do NOT add it to ruff/mypy targets. The
  `# flake8: noqa` and `# pylint: disable=` pragmas there are intentional and
  stay.
- Removing the orphaned `# pylint: disable=` pragmas in `dispatch/` — that's
  a cleanup follow-up; this plan only makes the gate exist and pass. If ruff
  flags the pragmas as unnecessary, removing them is in-scope (it's a ruff
  fix, not a rewrite).

## Git workflow

- Branch: `advisor/008-lint-typecheck-gates`
- Commit per step; message style: `chore(dx): add ruff and mypy gates for dispatch/`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add `ruff` and `mypy` to the dev extra and configure them in `pyproject.toml`

In `[project.optional-dependencies]`, replace:

```toml
dev = [
  "pytest",
  "pytest-asyncio",
]
```

with (pin to current stable versions — check the latest at install time, do
not invent versions):

```toml
dev = [
  "pytest",
  "pytest-asyncio",
  "ruff",
  "mypy",
]
```

Add at the end of `pyproject.toml`:

```toml
[tool.ruff]
line-length = 100
target-version = "py310"
extend-exclude = ["scr", "vendor", "tools/prod_tui", "mocks"]

[tool.ruff.lint]
# Start conservative: real errors + obvious style, not the full default set
# that churns the codebase. Expand later once the baseline is clean.
select = ["E", "F", "W", "I", "UP"]
ignore = ["E501"]  # line length handled by formatter; keep to avoid noise

[tool.ruff.format]
line-ending = "lf"

[tool.mypy]
python_version = "3.10"
packages = ["dispatch"]
exclude = ["scr/", "tools/prod_tui/", "mocks/"]
ignore_missing_imports = true
# Start non-strict: catch real type errors without forcing annotations on
# every function. Tighten later once the baseline is clean.
check_untyped_defs = true
warn_unused_ignores = true
warn_redundant_casts = true
```

### Step 2: Run ruff and fix the minimum to get a clean check

```
ruff check dispatch tests
ruff format --check dispatch tests
```

Fix what's flagged. Common expected fixes: unused imports (`F401`), unused
variables (`F841`), `from __future__ import annotations` ordering. If ruff
flags the orphaned `# pylint: disable=` lines as `noqa`-equivalent issues,
remove those specific lines in `dispatch/` (NOT in `scr/`). Do NOT reformat
whole files beyond what `ruff format` does automatically.

Apply auto-fixes with `ruff check --fix dispatch tests` then
`ruff format dispatch tests`, then re-run the check to confirm clean.

**Verify**: `ruff check dispatch tests` → exit 0;
`ruff format --check dispatch tests` → exit 0.

### Step 3: Run mypy and fix the minimum to get a clean check

```
mypy dispatch
```

Fix real type errors. Common expected ones: `None` returned where `int`
declared, missing `-> None` on `__init__`. If mypy reports errors in code
that's genuinely fine, add a targeted `# type: ignore[<code>]` with a comment
explaining why — do NOT blanket-`# type: ignore`. If the error count is large
(>20), STOP and report — the baseline may need a loosened mypy config instead
of a sweep of fixes in this plan.

**Verify**: `mypy dispatch` → exit 0.

### Step 4: Wire the gates into `AGENTS.md` Validation

In `AGENTS.md` under `## Validation` (around `:93-109`), add after the
compileall line:

```markdown
- `ruff check dispatch tests` — lint
- `ruff format --check dispatch tests` — format check
- `mypy dispatch` — typecheck
```

And add `pip install -e ".[dev]"` (or the cloud venv equivalent) as the
prerequisite for the test/lint/typecheck commands.

**Verify**: `grep -n "ruff check" AGENTS.md` returns a match.

## Test plan

- No new tests required — this plan adds tooling, not behavior. The gate
  *itself* is the verification.
- Verification: the three commands in Done criteria all exit 0.

## Done criteria

- [ ] `ruff check dispatch tests` exits 0
- [ ] `ruff format --check dispatch tests` exits 0
- [ ] `mypy dispatch` exits 0
- [ ] `python -m pytest tests -q` exits 0 (no regressions from the fixes)
- [ ] `grep -n "ruff" AGENTS.md` returns matches (the gate is documented)
- [ ] `grep -n "mypy" AGENTS.md` returns a match
- [ ] No files in `scr/` are modified (`git status -- scr/` empty)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `ruff check` flags >40 issues on the first run (the baseline is noisier than
  expected — STOP and report the count; the plan may need to start with a
  smaller `select` set or `--select` scoped to `E,F` only).
- `mypy dispatch` reports >20 errors (the baseline has real type gaps — STOP
  and report; the plan may need a looser config, e.g. start with
  `check_untyped_defs = false` and tighten incrementally).
- Fixing a ruff/mypy issue requires changing behavior (not just style/types) —
  STOP and report; behavioral fixes are out of scope for a dx-gate plan.
- `scr/` files are flagged (the exclude didn't work) — STOP and verify the
  `extend-exclude`/`exclude` syntax; do NOT fix `scr/`.

## Maintenance notes

- The `select` set (`E, F, W, I, UP`) is deliberately conservative. Expand it
  once the baseline is clean and stable — do not add the full ruff default
  set in this plan or it will churn every file.
- mypy is configured non-strict. A future plan can tighten to
  `strict = true` once annotations are comprehensive. Do not attempt strict
  in this plan.
- The `dev` extra now pulls `ruff` and `mypy`. CI (Plan 009) will install
  `.[dev]` and run all three gates.
- Reviewer: confirm `scr/` is untouched and the gates pass on a clean
  `pip install -e ".[dev]"`.
