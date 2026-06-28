# Plan 001: Fix Browser SHOW TABLES to use SQL `%` wildcard, not shell-glob `*`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/screens/browser.py dispatch/impala.py`
> If either file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

The Browser screen auto-loads tables on mount (`auto_load=True`) and submits
`SHOW TABLES IN <schema> LIKE '<pattern>';` to `impala-shell`. When the filter
input is empty — the common case — the code defaults the pattern to `"*"`. But
Impala's `LIKE` uses SQL wildcards (`%` = any sequence, `_` = single char); `*`
is a **literal** character. So on production Impala, `SHOW TABLES IN aa_enc
LIKE '*'` matches only a table literally named `*`, and the Browser shows
"(no tables)" for **every** schema on first load. The documented placeholder
`dispatch_*` is equally broken.

This is invisible in dev because the mock `impala-shell` returns its two
fixtures regardless of the LIKE pattern (`mocks/bin/impala-shell:124-131`), so
the bug ships undetected. It breaks the Browse-metadata screen — one of the
five top-level destinations — for every real user.

## Current state

`dispatch/screens/browser.py:52-53` — the filter Input and its placeholder:

```53: yield Input(value="*", placeholder="Filter (e.g. dispatch_*)", id="filter")
```

`dispatch/screens/browser.py:160-165` — `action_show_tables` resolves the
filter, defaulting empty to `"*"`:

```
160: async def action_show_tables(self, *, describe_selection: bool = True) -> None:
161:     self._show_table_list_message("Loading tables…", severity="dim")
162:     try:
163:         schema = self._schema()
164:         filter_val = self.query_one("#filter", Input).value.strip() or "*"
165:         self._tables = await impala.show_tables(schema, filter_val)
```

`dispatch/impala.py:39-40` — the pattern is interpolated straight into the
LIKE clause:

```
39: async def show_tables(schema: str, pattern: str = "*") -> list[str]:
40:     output = await query(f"SHOW TABLES IN {schema} LIKE '{pattern}';")
```

`mocks/bin/impala-shell:124-131` — the mock ignores the LIKE pattern and always
returns `dispatch_result` and `dispatch_monthly_fulljoin`, which is why the bug
is invisible in dev:

```
124:     if tag == "SHOW_TABLES":
128:         print("name")
129:         print("dispatch_result")
130:         print("dispatch_monthly_fulljoin")
131:         return 0
```

**Repo conventions**: Input defaults and placeholders are set in
`dispatch/screens/*.py` directly (see `new_job.py:96-117` for the pattern).
Async impala calls live in `dispatch/impala.py`. Error messages surface through
the screen's `except Exception` path (`browser.py:166-169`). Match these.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |
| Help      | `python -m dispatch --help`      | exit 0, prints usage |

Run tests from the repo root with the venv (`/workspace/.venv/bin/python` on
Cursor Cloud, or `python` after `pip install -e ".[dev]"` elsewhere). On
Windows, `source mocks/dev-env.sh` is not available; the unit tests that need
the mock PATH use the `mock_env` fixture in `tests/conftest.py` automatically.

## Scope

**In scope**:
- `dispatch/screens/browser.py`
- `dispatch/impala.py`
- `tests/test_qa_fixes.py` (add a unit test for the wildcard default; this file
  already has `test_show_tables_strips_impala_shell_name_header` — extend it)

**Out of scope**:
- `mocks/bin/impala-shell` — do NOT make the mock honor the LIKE pattern. The
  mock is intentionally scenario-driven (ADR-0004) and changing its contract
  is a separate, larger task. The fix must be verified by a unit test that
  asserts the *submitted SQL*, not by making the mock realistic.
- `dispatch/screens/new_job.py`, `dispatch/manifest.py` — unrelated to the
  Browser wildcard bug.
- Any change to the `DESCRIBE` / `DROP TABLE` interpolation — that is
  Plan 010 (identifier validation). This plan only fixes the wildcard default
  and placeholder.

## Git workflow

- Branch: `advisor/001-browser-like-wildcard`
- Commit per step; message style: `fix(browser): <what changed>` (the repo
  uses `fix(scope):` per `git log --oneline`, e.g. `fix(impala): strip SHOW
  TABLES print_header row`).
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Change the empty-filter default from `*` to `%`

In `dispatch/impala.py:39`, change the `show_tables` default parameter and the
f-string so the SQL-LIKE wildcard is explicit:

```python
async def show_tables(schema: str, pattern: str = "%") -> list[str]:
    output = await query(f"SHOW TABLES IN {schema} LIKE '{pattern}';")
```

In `dispatch/screens/browser.py:164`, change the runtime default:

```python
filter_val = self.query_one("#filter", Input).value.strip() or "%"
```

### Step 2: Update the placeholder and the initial Input value

In `dispatch/screens/browser.py:53`, change the Input so its initial value is
the SQL wildcard and the placeholder teaches the SQL convention:

```python
yield Input(value="%", placeholder="Filter (e.g. dispatch_%)", id="filter")
```

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 3: Add a unit test asserting the submitted SQL uses `%`

In `tests/test_qa_fixes.py`, add (next to the existing
`test_show_tables_strips_impala_shell_name_header`):

```python
async def test_show_tables_default_pattern_is_sql_wildcard(monkeypatch):
    """Empty filter must submit LIKE '%', not shell-glob '*'."""
    from dispatch import impala

    captured = {}
    async def fake_query(sql):
        captured["sql"] = sql
        return "name\ndispatch_result\n"

    monkeypatch.setattr(impala, "query", fake_query)
    await impala.show_tables("aa_enc", "")
    assert "LIKE '%'" in captured["sql"]
    assert "*" not in captured["sql"]
```

If the existing test file uses a different async-test style (e.g. `@pytest.mark.asyncio`),
match it. Check `tests/test_qa_fixes.py` for the decorator pattern before
writing.

**Verify**: `python -m pytest tests/test_qa_fixes.py -q` → all pass,
including the new test.

## Test plan

- New test: `test_show_tables_default_pattern_is_sql_wildcard` in
  `tests/test_qa_fixes.py` — asserts the submitted SQL contains `LIKE '%'`
  and no `*` when the filter is empty.
- Structural pattern to mirror: the existing
  `test_show_tables_strips_impala_shell_name_header` in the same file (it
  already monkeypatches `impala.query` — copy that shape).
- Verification: `python -m pytest tests/test_qa_fixes.py -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests/test_qa_fixes.py -q` exits 0; the new
      `test_show_tables_default_pattern_is_sql_wildcard` passes
- [ ] `grep -n 'or "\*"' dispatch/screens/browser.py` returns no matches
- [ ] `grep -n 'pattern: str = "\*"' dispatch/impala.py` returns no matches
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- The code at `dispatch/screens/browser.py:53,164` or `dispatch/impala.py:39-40`
  doesn't match the excerpts above (the codebase has drifted).
- The existing `test_show_tables_strips_impala_shell_name_header` test does not
  exist in `tests/test_qa_fixes.py` (the structural pattern moved — find the
  new location before proceeding).
- Step 3's test fails to pass after a reasonable fix attempt, because the
  async-test decorator convention in the file differs from `@pytest.mark.asyncio`.

## Maintenance notes

- After this lands, the mock `impala-shell` still ignores the LIKE pattern. A
  future task (NOT this plan) could make the mock filter its fixtures by the
  LIKE pattern so the Browser's filtering is exercised end-to-end in dev.
  File that separately if desired.
- If `dispatch/impala.py` later gains identifier validation (Plan 010), the
  `show_tables` signature stays the same; the validator runs on `schema` and
  `pattern` before the f-string. No conflict.
- Reviewer: confirm the placeholder text reads correctly in a narrow terminal
  (it's shown in the Browser's left pane, `dispatch/app.tcss` controls width).
