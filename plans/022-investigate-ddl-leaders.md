# Plan 022: Investigate — `is_self_contained_ddl` misses `UPDATE`/`DELETE`/`REPLACE`/`CALL`

> **Executor instructions**: This is an INVESTIGATE plan, not a fix plan.
> Read the code, confirm the gap, decide whether it's a real user-facing
> risk, and either (a) write a fix plan as a new file under `plans/` and
> STOP, or (b) record the finding as low-impact in `plans/README.md`'s
> "Findings considered and rejected" and STOP. Do NOT implement a fix
> directly in this plan.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/sql.py dispatch/manifest.py dispatch/screens/new_job.py`
> If `dispatch/sql.py` changed since this plan was written, re-confirm the
> gap before concluding.

## Status

- **Priority**: P3
- **Effort**: S (investigation) → S if a fix plan results
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug (investigate)
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters (the smell)

`dispatch/sql.py:26` defines `_DDL_LEADERS = ("create", "drop", "insert",
"alter", "truncate", "merge")`. `is_self_contained_ddl` (`:29-50`) returns
True only when the SQL file's first keyword is in that set; otherwise the
file is treated as a bare `SELECT` and wrapped via `table_wrapper` (`:53-63`)
as `DROP TABLE … / CREATE TABLE … AS <body>`.

A `SqlFile → Table` job whose body opens with `UPDATE …`, `DELETE FROM …`,
`REPLACE …`, or `CALL …` returns False from `is_self_contained_ddl` and is
wrapped, yielding `CREATE TABLE … AS UPDATE/DELETE/…`, which is not valid
Impala SQL. The user gets a confusing syntax error from Impala at run time
with no inline pre-launch validation catching it.

The question is whether real users ever point a `SqlFile → Table` job at a
maintenance script (UPDATE/DELETE/REPLACE/CALL). If they do, this is a real
UX bug; if they don't (the cell is documented as SELECT-only), it's a
defensive hardening with low impact.

## Current state

`dispatch/sql.py:26`:

```
26: _DDL_LEADERS = ("create", "drop", "insert", "alter", "truncate", "merge")
```

`dispatch/sql.py:29-50` — `is_self_contained_ddl` skips leading comments then
checks `first.startswith(leader)`.

`dispatch/sql.py:53-63` — `table_wrapper` produces the `DROP/CREATE TABLE …
AS <body>` wrapper.

`dispatch/manifest.py:166-177` — `_effective_job_sql` wraps only when
`not sql.is_self_contained_ddl(sql_text)` for Table/Table+Csv destinations.

`dispatch/screens/new_job.py:551-559` — `action_preview` mirrors the same
rule for the preview.

`CONTEXT.md:11-14` — `SqlFile` is defined as "a single `.sql` file holding
one `SELECT`". This suggests the cell is SELECT-only by design, but does not
explicitly forbid a file that opens with DML.

## Investigation steps

### Step 1: Confirm the gap is real

Read `dispatch/sql.py:26-50` and confirm:
- `UPDATE`, `DELETE`, `REPLACE`, `CALL`, `LOAD`, `REFRESH` are not in
  `_DDL_LEADERS`.
- A file opening with any of those keywords would be wrapped into
  `CREATE TABLE … AS <body>`.
- `NewJobScreen._validate` (`new_job.py:461-492`) does not inspect the SQL
  body shape for Table-destined files (it only checks `is_malformed_template`
  and `template_is_complete` for SqlTemplate).

### Step 2: Assess the user-facing risk

Check:
- `CONTEXT.md:11-14` — is `SqlFile → Table` documented as SELECT-only?
- `docs/` — any user-facing doc that says "Table jobs wrap a SELECT"?
- `tests/` — any test that feeds a non-SELECT SqlFile to a Table job?

If `CONTEXT.md` defines `SqlFile` as "one SELECT", then a user who points it
at an UPDATE is off-spec, and the failure mode (a syntax error at run time)
is acceptable but unfriendly. If docs are silent, the gap is a real bug for
any user who assumes any SQL file can be a Table job.

### Step 3: Decide and record

- **If low-impact (off-spec use, friendly error acceptable)**: record in
  `plans/README.md`'s "Findings considered and rejected":
  > `CORRECTNESS-08` `is_self_contained_ddl` misses UPDATE/DELETE/REPLACE/CALL:
  > `CONTEXT.md` defines `SqlFile` as a single SELECT, so pointing a
  > Table-destination job at a DML file is off-spec. The run-time syntax
  > error is unfriendly but not a correctness bug for in-spec use. NOT worth
  > a plan; optionally harden `_DDL_LEADERS` in a future polish pass.
  
  Optionally write a tiny hardening plan (new file, e.g.
  `plans/022a-harden-ddl-leaders.md`) that adds the missing leaders so
  DML files pass through unwrapped (producing a cleaner Impala error or
  succeeding if the DML is valid standalone), and STOP.

- **If real bug (docs silent or users hit it)**: write a fix plan (new file,
  e.g. `plans/022b-validate-table-sql-shape.md`) that either (a) expands
  `_DDL_LEADERS` to include `update`, `delete`, `replace`, `call`, `load`,
  `refresh` so DML passes through unwrapped, or (b) adds a pre-launch
  validation in `NewJobScreen._validate` that rejects non-SELECT/non-DDL
  bodies for Table destinations with a clear message. Prefer (a) —
  passthrough is the safe default for any complete statement. STOP after
  writing the fix plan.

## Done criteria

- [ ] The investigation is documented: `CONTEXT.md` and docs checked, the
      gap confirmed, the user-facing risk assessed.
- [ ] A decision is recorded: low-impact (rejected or tiny hardening plan)
      OR real bug (fix plan file created).
- [ ] `plans/README.md` is updated: either the "Findings considered and
      rejected" entry for `CORRECTNESS-08`, or a new row pointing at the
      fix plan.
- [ ] No source code is modified in this plan (investigation only).

## STOP conditions

Stop and report back (do not improvise) if:
- `dispatch/sql.py:26` `_DDL_LEADERS` already includes the missing keywords
  (the gap was fixed independently — mark REJECTED in the index).
- `CONTEXT.md:11-14` no longer defines `SqlFile` as "one SELECT" (the spec
  changed — re-assess whether DML is now in-spec).
- A test in `tests/test_pure_logic.py` already covers UPDATE/DELETE as
  `is_self_contained_ddl` inputs (the gap may be smaller than the audit
  thought — record what's actually tested).

## Maintenance notes

- This plan exists because the audit flagged LOW confidence: the gap is
  certain, the user-facing frequency is not. Investigate before fixing.
- The safest hardening (option (a), expanding `_DDL_LEADERS`) is a one-line
  change with no behavior change for in-spec SELECT files — DML files just
  pass through unwrapped instead of being wrapped into invalid SQL. If in
  doubt, prefer (a).
- Reviewer (maintainer): if you know users run DML via `SqlFile → Table`,
  upgrade this to a real fix plan; if not, the rejection is fine.
