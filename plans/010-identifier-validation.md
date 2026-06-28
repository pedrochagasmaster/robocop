# Plan 010: Validate identifiers before interpolating into impala-shell SQL

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/impala.py dispatch/screens/browser.py dispatch/screens/new_job.py`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (Plan 001 touches the same files but is independent)
- **Category**: bug
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

`dispatch/impala.py` interpolates raw `schema`, `pattern`, and `full_table`
into SQL strings sent to `impala-shell -q`:

- `SHOW TABLES IN {schema} LIKE '{pattern}';`
- `DESCRIBE {full_table};`
- `DROP TABLE IF EXISTS {full_table};`

These come from Browser Inputs (`browser.py:52-53`) and the New Job
`existing-table` field. A value containing `'`, `;`, or `--` breaks out of the
LIKE string literal and runs arbitrary SQL against the user's own Impala
session. Severity is bounded — the user is authenticated with their own
Kerberos ticket, so this is self-injection, not privilege escalation — but
the realistic production symptom is a broken Browser on a filter typo (e.g.
`dispatch's tables`) that crashes with an Impala syntax error instead of a
clear message. Identifier validation turns an opaque impala-shell error into
an actionable in-TUI message.

## Current state

`dispatch/impala.py:39-61`:

```
39: async def show_tables(schema: str, pattern: str = "%") -> list[str]:
40:     output = await query(f"SHOW TABLES IN {schema} LIKE '{pattern}';")
...
56: async def describe_table(full_table: str) -> str:
57:     return await query(f"DESCRIBE {full_table};")
...
60: async def drop_table(full_table: str) -> str:
61:     return await query(f"DROP TABLE IF EXISTS {full_table};")
```

`dispatch/screens/browser.py:52-53` — schema and filter Inputs; `:131-137`
`_full_table()` builds `<schema>.<table>` from them.

`dispatch/screens/new_job.py:499-501` — ExistingTable source uses the typed
`existing-table` field which flows into `manifest.build_orchestrator_calls`
and ultimately a `download_to_csv.py --table-name` argv (not interpolated into
`-q` SQL, so lower risk, but the identifier still reaches Impala).

**Repo conventions**: input validation in `dispatch/` uses early-return
string checks (see `sql.validate_date_range` returning `str | None` error
messages, and `new_job._validate`). `impala.py` raises `RuntimeError` on
non-zero exit (`impala.py:34-35`); the Browser catches `Exception` and shows
the message (`browser.py:166-169`). A `ValueError` from validation follows
the same path.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `dispatch/impala.py` — add identifier validation and call it in
  `show_tables`/`describe_table`/`drop_table`.
- `tests/test_qa_fixes.py` — add unit tests for the validator.

**Out of scope**:
- `dispatch/manifest.py` `build_orchestrator_calls` — the `--table-name` argv
  is passed as a list element to `subprocess`, not interpolated into SQL, so
  it's not an injection vector. The identifier still reaches Impala via the
  orchestrator, but `scr/` is frozen-API per ADR-0005; do not add validation
  there. A defensive check in `manifest.build_orchestrator_calls` is optional
  and out of scope for this plan.
- `dispatch/sql.py` `table_wrapper` — the HDFS `LOCATION` path interpolation
  (`/das/{prefix}/enc/{user}/{table_name}`) is a separate path-traversal
  surface; it is NOT SQL injection. File separately if desired. This plan is
  SQL-interpolation only.

## Git workflow

- Branch: `advisor/010-identifier-validation`
- Commit per step; message style: `fix(impala): validate identifiers before interpolating into SQL`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add `_validate_identifier` to `dispatch/impala.py`

Add a validator that allows Impala identifier characters plus SQL-LIKE
wildcards (`%`, `_`) and the `.` separator for qualified names. Reject quotes,
semicolons, comments, whitespace, and anything else:

```python
import re

_IDENT_RE = re.compile(r"^[A-Za-z0-9_.*%-]+$")

def _validate_identifier(value: str, *, allow_wildcards: bool = False) -> str:
    """Validate an Impala identifier (or ``schema.table`` qualified name).

    Rejects characters that could break out of a SQL string literal or
    statement. ``allow_wildcards`` permits ``%`` and ``_`` for LIKE patterns.
    Returns the value on success, raises ``ValueError`` on invalid input so
    the Browser's existing ``except Exception`` path surfaces the message.
    """
    if not value:
        raise ValueError("identifier is empty")
    if not allow_wildcards:
        # Strip wildcards from the allowed set for schema/table identifiers.
        if any(c in value for c in "%_*"):
            raise ValueError(
                f"identifier {value!r} must not contain wildcards or '*'"
            )
    if not _IDENT_RE.match(value):
        raise ValueError(
            f"identifier {value!r} contains illegal characters "
            "(allowed: letters, digits, '.', '_', and for filters '%'/'*')"
        )
    return value
```

### Step 2: Call the validator in `show_tables`/`describe_table`/`drop_table`

```python
async def show_tables(schema: str, pattern: str = "%") -> list[str]:
    _validate_identifier(schema)
    _validate_identifier(pattern, allow_wildcards=True)
    output = await query(f"SHOW TABLES IN {schema} LIKE '{pattern}';")
    ...

async def describe_table(full_table: str) -> str:
    _validate_identifier(full_table)
    return await query(f"DESCRIBE {full_table};")

async def drop_table(full_table: str) -> str:
    _validate_identifier(full_table)
    return await query(f"DROP TABLE IF EXISTS {full_table};")
```

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 3: Add unit tests

In `tests/test_qa_fixes.py`, add tests asserting valid identifiers pass and
injection-shaped input raises:

```python
def test_validate_identifier_accepts_qualified_name():
    from dispatch.impala import _validate_identifier
    assert _validate_identifier("aa_enc.dispatch_result") == "aa_enc.dispatch_result"

def test_validate_identifier_rejects_injection():
    from dispatch.impala import _validate_identifier
    import pytest
    for bad in ["x'; DROP TABLE y; --", "x; DROP TABLE y", "name' OR 1=1", "x --"]:
        with pytest.raises(ValueError):
            _validate_identifier(bad)

def test_validate_identifier_wildcards_only_when_allowed():
    from dispatch.impala import _validate_identifier
    import pytest
    assert _validate_identifier("dispatch_%", allow_wildcards=True) == "dispatch_%"
    with pytest.raises(ValueError):
        _validate_identifier("dispatch_%")  # wildcards rejected by default
```

**Verify**: `python -m pytest tests/test_qa_fixes.py -q` → all pass.

## Test plan

- New tests: the three `test_validate_identifier_*` functions above in
  `tests/test_qa_fixes.py`.
- Structural pattern: existing tests in the same file (e.g.
  `test_show_tables_strips_impala_shell_name_header`).
- Verification: `python -m pytest tests/test_qa_fixes.py -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests/test_qa_fixes.py -q` exits 0; the three new
      validator tests pass
- [ ] `python -m pytest tests -q` exits 0 (no regressions — the Browser's
      existing `except Exception` path handles the new `ValueError`)
- [ ] `grep -n "_validate_identifier" dispatch/impala.py` returns the
      definition plus 3 call sites
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `dispatch/impala.py:39-61` no longer matches the excerpts (the functions
  were restructured — re-check before inserting validation).
- The Browser UI tests in `tests/test_ui_ux_closure.py` break because they
  feed filter values like `dispatch_*` that the new validator rejects — if
  so, the validator's wildcard handling needs adjusting, but STOP and report
  rather than silently loosening it.
- A legitimate production identifier contains a character the regex rejects
  (e.g. a hyphenated schema name) — STOP and report; the regex may need
  broadening, but verify the character is actually legal in Impala first.

## Maintenance notes

- The validator is defensive, not a security boundary — the real auth is
  Kerberos. Its job is to turn opaque impala-shell syntax errors into clear
  in-TUI messages and to make typos non-fatal.
- `allow_wildcards=True` is only for the LIKE pattern in `show_tables`.
  `describe_table`/`drop_table` take real identifiers and never allow
  wildcards.
- If `dispatch/sql.py` `table_wrapper`'s HDFS `LOCATION` path is later
  validated too, use a separate path-validator (filesystem paths have
  different legal characters than SQL identifiers). Do not reuse this one.
- Reviewer: confirm the Browser still loads tables on the default `%`
  pattern (Plan 001's fix) and that a typed `dispatch_` filter still works.
