# Plan 017: Parametrize `errors.classify` and `scr/_common.classificar_erro_impala` tests

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/errors.py scr/_common.py tests/test_ui_ux_audit_implementation.py tests/test_mock_contract.py`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

`dispatch/errors.classify` maps orchestrator log tails to five error codes
(SYNTAX, TABLE_NOT_FOUND, MEMORY, AUTH, QUEUE) that drive the dashboard state
column and the Job Detail error banner. It is tested only for SYNTAX + an
unknown pattern (`tests/test_ui_ux_audit_implementation.py:32-40`); the
QUEUE path is exercised only indirectly via a UI test
(`tests/test_production_polish.py:190-211`). The other three codes (AUTH,
MEMORY, TABLE_NOT_FOUND) and the `suggestion()`/`first_matching_line()`
helpers have zero direct tests.

`scr/_common.classificar_erro_impala` is the orchestrator-side classifier
that decides fatal-vs-retriable (and thus whether the pool retry loop
continues or `sys.exit(1)` fires). It has no tests at all. The two classifiers
can drift (the TUI's `errors.PATTERNS` vs `_common`'s `if`-chain) with no
regression signal.

## Current state

`dispatch/errors.py:8-14` — five `PATTERNS`:

```
8: PATTERNS: list[tuple[str, str]] = [
9:     ("SYNTAX", r"AnalysisException.*Syntax error|Erro mapeado: SYNTAX_ERROR|\bSYNTAX_ERROR\b"),
10:     ("TABLE_NOT_FOUND", r"Table.*does not exist|TableNotFoundException|\bTABLE_NOT_FOUND\b"),
11:     ("MEMORY", r"Memory limit exceeded|MEMORY_LIMIT_EXCEEDED"),
12:     ("AUTH", r"AuthorizationException|AuthenticationException|Kerberos.*expired|\bAUTH_ERROR\b"),
13:     ("QUEUE", r"Rejected.*pool|All pools busy|queue timeout|exceeded timeout: queue is full"),
14: ]
```

`dispatch/errors.py:43-76` — `classify`, `suggestion`, `first_matching_line`.

`scr/_common.py:35-76` — `classificar_erro_impala` with ~20 `if` branches
returning `{"categoria": ..., "detalhes": ...}`.

`tests/test_ui_ux_audit_implementation.py:32-40` — only SYNTAX + unknown.

**Repo conventions**: parametrized tests use `@pytest.mark.parametrize`
(see `tests/test_pure_logic.py` for examples). `scr/_common` is importable
directly (`from _common import classificar_erro_impala`) — the
`mock_env` fixture puts `scr/` on the path via `DISPATCH_SCR_DIR` but
`_common.py` is importable as a module from the `scr/` dir; check how
`tests/test_monthly_query_processor.py` imports it.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `tests/test_ui_ux_audit_implementation.py` — parametrize `classify` over
  all five codes + unknown.
- `tests/test_scr_common.py` (new file) — parametrize
  `classificar_erro_impala` over representative stderr fixtures.

**Out of scope**:
- `dispatch/errors.py`, `scr/_common.py` — no code changes; this plan adds
  tests only. If a test reveals a classification bug, file it separately;
  do not fix the classifier in this plan.
- Reconciling the two classifiers' pattern sets — a larger design task; this
  plan characterizes current behavior so drift is caught.

## Git workflow

- Branch: `advisor/017-classifier-tests`
- Commit per step; message style: `test(errors): parametrize classify and scr/_common classification`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Parametrize `dispatch.errors.classify` tests

In `tests/test_ui_ux_audit_implementation.py`, add a parametrized test class
covering all five codes + the unknown fallback. Use representative log line
snippets that match the regexes in `errors.PATTERNS`:

```python
import pytest
from pathlib import Path
from dispatch import errors

@pytest.mark.parametrize("code,log_line", [
    ("SYNTAX", "AnalysisException: Syntax error in line 1"),
    ("SYNTAX", "Erro mapeado: SYNTAX_ERROR"),
    ("TABLE_NOT_FOUND", "TableNotFoundException: table 'x' does not exist"),
    ("TABLE_NOT_FOUND", "TABLE_NOT_FOUND"),
    ("MEMORY", "Memory limit exceeded: query memory"),
    ("AUTH", "AuthorizationException: User 'x' does not have privileges"),
    ("AUTH", "Kerberos ticket expired"),
    ("QUEUE", "All pools busy; queue timeout exceeded"),
])
def test_classify_identifies_code(tmp_path, code, log_line):
    log = tmp_path / "run.log"
    log.write_text(log_line + "\n", encoding="utf-8")
    assert errors.classify(log) == code

def test_classify_returns_none_for_unknown(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("some random orchestrator output\n", encoding="utf-8")
    assert errors.classify(log) is None

def test_suggestion_returns_guidance_for_known_code():
    assert "Kerberos" in errors.suggestion("AUTH") or "kinit" in errors.suggestion("AUTH")

def test_suggestion_returns_default_for_unknown():
    assert errors.suggestion(None) == "Check the log for details."

def test_first_matching_line_returns_the_line(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("ok line\nMemory limit exceeded\n", encoding="utf-8")
    line = errors.first_matching_line(log, "MEMORY")
    assert "Memory limit exceeded" in line
```

If the existing test file already has a `TestErrorClassification` class,
extend it; otherwise add the functions standalone.

### Step 2: Add `tests/test_scr_common.py` for `classificar_erro_impala`

Create `tests/test_scr_common.py`:

```python
"""Characterization tests for scr/_common.classificar_erro_impala.

These pin the orchestrator-side error mapping that drives fatal-vs-retriable
decisions. Narrow ADR-0005 exception: tests only, no scr/ code changes."""
import pytest
import sys
from pathlib import Path

# scr/ is not a package; import _common directly by putting scr/ on sys.path
SCR_DIR = Path(__file__).resolve().parents[1] / "scr"
sys.path.insert(0, str(SCR_DIR))
from _common import classificar_erro_impala  # noqa: E402

@pytest.mark.parametrize("categoria,stderr_snippet", [
    ("MEMORY_EXCEEDED", "Memory limit exceeded"),
    ("SYNTAX_ERROR", "ParseException: syntax error near 'SELECT'"),
    ("AUTH_ERROR", "AuthenticationException: invalid credentials"),
    ("TABLE_NOT_FOUND", "Table not found: foo.bar"),
    ("TIMEOUT", "query timed out"),
    ("QUEUE_FULL", "queue is full"),
    ("GENERIC_ERROR", "something entirely novel"),
])
def test_classificar_erro_impala_maps_stderr(categoria, stderr_snippet):
    result = classificar_erro_impala(stderr_snippet)
    assert result["categoria"] == categoria
    assert result["detalhes"] == stderr_snippet
```

The exact `categoria` strings come from `scr/_common.py:35-76` — read the file
before finalizing the parametrize list to ensure the expected categories
match the actual `if`-chain order (the first matching branch wins).

**Verify**: `python -m pytest tests/test_scr_common.py tests/test_ui_ux_audit_implementation.py -q` → all pass.

## Test plan

- New tests: parametrized `test_classify_identifies_code` + helpers in
  `tests/test_ui_ux_audit_implementation.py`; parametrized
  `test_classificar_erro_impala_maps_stderr` in new
  `tests/test_scr_common.py`.
- Structural pattern: `@pytest.mark.parametrize` usage in
  `tests/test_pure_logic.py`; `scr/` import pattern in
  `tests/test_monthly_query_processor.py`.
- Verification: `python -m pytest tests -q` → all pass.

## Done criteria

- [ ] `python -m pytest tests/test_ui_ux_audit_implementation.py -q` exits 0;
      the parametrized classify tests pass for all 5 codes + unknown
- [ ] `python -m pytest tests/test_scr_common.py -q` exits 0
- [ ] `python -m pytest tests -q` exits 0 (no regressions)
- [ ] `grep -n "test_classify_identifies_code" tests/test_ui_ux_audit_implementation.py` returns a match
- [ ] `test -f tests/test_scr_common.py` succeeds
- [ ] No files in `dispatch/` or `scr/` are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `dispatch/errors.py:8-14` `PATTERNS` no longer has five codes (the set
  changed — adapt the parametrize list).
- `scr/_common.py:35-76` `classificar_erro_impala` returns categories not in
  the parametrize list (read the file and align; report the final list).
- Importing `_common` from `tests/` fails because `scr/` is not on `sys.path`
  in the test environment — use the `mock_env` fixture's `DISPATCH_SCR_DIR`
  or `sys.path.insert(0, str(SCR_DIR))` as shown; if it still fails, STOP and
  report how `tests/test_monthly_query_processor.py` imports `scr/` modules.
- A test reveals a classification discrepancy between `errors.PATTERNS` and
  `_common`'s branches — that's a finding, not a fix; record it in the report
  and file a separate plan. Do NOT "fix" the classifier in this plan.

## Maintenance notes

- These are characterization tests: they pin *current* behavior. If a
  classification is genuinely wrong (e.g. a real AUTH stderr maps to
  GENERIC_ERROR), the test will pass (it pins the current mapping) — the bug
  is for a separate fix plan. Update the test only when the mapping
  intentionally changes.
- When a new error code is added to `errors.PATTERNS` or
  `classificar_erro_impala`, add a parametrize row here in the same PR.
- The `scr/` import via `sys.path.insert` is a test-only convenience; it does
  not violate ADR-0005 (no `scr/` code is modified).
- Reviewer: confirm the `categoria` strings in the parametrize list exactly
  match `scr/_common.py`'s return values (read the file first).
