# Plan 021: Investigate — does the SqlTemplate manifest need the `_fulljoin` table name?

> **Executor instructions**: This is an INVESTIGATE plan, not a fix plan.
> Read the code, confirm the divergence, decide the intended behavior with
> evidence, and either (a) write a fix plan as a new file under `plans/` and
> STOP, or (b) record the finding as by-design in `plans/README.md`'s
> "Findings considered and rejected" and STOP. Do NOT implement a fix
> directly in this plan.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/manifest.py scr/monthly_query_processor.py dispatch/screens/job_detail.py dispatch/sql.py`
> If any changed since this plan was written, re-confirm the divergence
> before concluding.

## Status

- **Priority**: P3
- **Effort**: S (investigation) → M if a fix plan results
- **Risk**: LOW (investigation) → LOW-MED if a fix plan results
- **Depends on**: none
- **Category**: bug (investigate)
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters (the smell)

`dispatch/manifest.py:304-323` builds the `monthly_query_processor.py` call
with `--table-name {table}` (the user's base name, e.g. `dispatch_result`).
But `scr/monthly_query_processor.py:83` constructs
`final_table_name = f"{args.schema}.{args.table_name}_fulljoin"` and at
`:103-108` runs `DROP/CREATE TABLE {final_table_name} … AS {union_query}`.
The manifest's `destination.table_name` stays `dispatch_result`; the
actually-created Impala table is `aa_enc.dispatch_result_fulljoin`.

If this is unintended, the consequences are:
- The dashboard "Destination" column (`dashboard.py:426-432`) and Job Detail
  summary (`job_detail.py:220-226`) report `aa_enc.dispatch_result` — a table
  that does not exist.
- Cloning the job as `ExistingTable → Csv` (`job_detail.py:396-416`
  `_prefill_from_manifest` copies `dest.table_name` into `existing_table`)
  targets `aa_enc.dispatch_result` and the export fails with
  `TABLE_NOT_FOUND`.
- The Browser cannot find the result under the recorded name.

If this is **intended** (the monthly processor deliberately appends
`_fulljoin` and the manifest records the logical base name), the displayed
destination is misleading but the system is consistent.

## Current state

`dispatch/manifest.py:304-323` — the SqlTemplate call:

```
304:     if source_type == "SqlTemplate":
305:         argv = script_argv("monthly_query_processor.py") + [
306:             "--sql-file", str(job_dir / "job.sql"),
307:             "--schema", schema,
308:             "--table-name", table,
...
323:         calls.append({"script": "monthly_query_processor.py", "argv": argv})
```

`scr/monthly_query_processor.py:83`:

```
83: final_table_name = f"{args.schema}.{args.table_name}_fulljoin"
```

`scr/monthly_query_processor.py:103-108` — `DROP/CREATE TABLE
{final_table_name}` (the `_fulljoin` table is the real output).

`dispatch/screens/job_detail.py:396-416` — `_prefill_from_manifest` uses
`dest.get("table_name")` for the clone's `existing_table` field.

`dispatch/sql.py:77-95` — `monthly_preview` generates per-month temp tables
named `{schema}.{table_name}_temp_{YYYYMM}` but does not mention
`_fulljoin` in the preview (the final join table name is invisible to the
user pre-launch).

## Investigation steps

### Step 1: Confirm the divergence is real

Read `scr/monthly_query_processor.py` end-to-end (it's 138 lines). Confirm:
- The `_fulljoin` suffix is unconditional (every SqlTemplate job produces
  `<table>_fulljoin`).
- No code path strips the suffix or records the final name in the manifest.
- The preview (`sql.monthly_preview`) does not surface the `_fulljoin` name.

### Step 2: Check git history for intent

```
git log --oneline -- scr/monthly_query_processor.py
git log -p -- scr/monthly_query_processor.py | grep -A5 -B5 _fulljoin
```

Look for the commit that introduced `_fulljoin`. The recent commit
`b3ae616 [scr/] monthly_query_processor: derive HDFS location prefix from
schema` and `8b4241e Pin monthly Impala job to one coordinator` touched this
file — check whether the `_fulljoin` suffix was added deliberately there or
predates the manifest design. If a commit message explains the suffix,
record it.

### Step 3: Check docs and ADRs for the `_fulljoin` convention

```
grep -rn "_fulljoin" docs/ CONTEXT.md
```

If `CONTEXT.md` or an ADR documents the `_fulljoin` naming convention as
intentional, the divergence is by-design (the manifest records the logical
base name; the `_fulljoin` is an implementation detail). If no doc mentions
it, it's likely undocumented drift.

### Step 4: Decide and record

- **If by-design**: the manifest's `destination.table_name` is the logical
  base name; `_fulljoin` is the orchestrator's implementation detail. The
  fix is to make the UI surface the real created name. Record in
  `plans/README.md`'s "Findings considered and rejected":
  > `CORRECTNESS-06` SqlTemplate `_fulljoin` suffix: by-design — the
  > manifest records the logical base table name; `monthly_query_processor.py`
  > appends `_fulljoin` as an implementation detail. The UI should display
  > the `_fulljoin` name and `_prefill_from_manifest` should append it for
  > ExistingTable clones — file a separate UX plan if desired. NOT a bug.
  
  Then write a small follow-up UX plan (new file, e.g.
  `plans/021a-surface-fulljoin-name.md`) that surfaces the real name in the
  dashboard/Job Detail and appends `_fulljoin` in the clone prefill, and
  STOP.

- **If drift (unintended)**: write a fix plan (new file, e.g.
  `plans/021b-fix-fulljoin-manifest.md`) that either (a) records
  `{table}_fulljoin` in the manifest's `destination.table_name` at build
  time in `manifest.build_orchestrator_calls`, or (b) adds a
  `destination.created_table_name` field to the manifest schema. Either way,
  update `sql.monthly_preview` to note the final table name, and
  `_prefill_from_manifest` to use the real name. The fix plan must note the
  manifest-schema implications (a new field is a schema bump per ADR-0001;
  reusing `table_name` is not). STOP after writing the fix plan.

## Done criteria

- [ ] The investigation is documented: git history checked, docs/ADRs
      checked, the divergence confirmed or refuted.
- [ ] A decision is recorded: by-design (with a follow-up UX plan) OR drift
      (with a fix plan file created).
- [ ] `plans/README.md` is updated: either the "Findings considered and
      rejected" entry for `CORRECTNESS-06`, or a new row in the execution
      table pointing at the fix plan.
- [ ] No source code is modified in this plan (investigation only).

## STOP conditions

Stop and report back (do not improvise) if:
- `scr/monthly_query_processor.py:83` no longer constructs `_fulljoin` (the
  divergence was fixed independently — mark REJECTED in the index).
- The git history is ambiguous (no commit explains the suffix) AND no doc
  mentions it — default to "drift (unintended)" and write the fix plan, but
  flag the ambiguity in the report so the maintainer can overrule.
- A schema bump (adding `created_table_name`) is required and you're unsure
  whether ADR-0001's "stable on-disk contract" allows it without a
  `schema_version: 2` migration — STOP and record the question for the
  maintainer; do not implement a schema change blind.

## Maintenance notes

- This plan exists because the audit flagged LOW confidence: the divergence
  is certain, the intent is not. Investigate before fixing.
- If the conclusion is "by-design", the follow-up UX plan (surfacing the
  real name) is P3 — nice-to-have, not blocking.
- If the conclusion is "drift", the fix plan should land before Plan 016's
  SqlTemplate runner test is written, so the test asserts the correct
  created-table name.
- Reviewer (maintainer): the decision is yours — the advisor can only
  present evidence. Overrule the investigation's conclusion if you have
  context the code lacks.
