# Plan 001: Harden launch identifiers and CSV output paths

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat a50c81d..HEAD -- dispatch/screens/new_job.py dispatch/manifest.py dispatch/sql.py dispatch/impala.py scr/download_to_csv.py scr/monthly_query_processor.py tests`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `a50c81d`, 2026-06-28

## Why this matters

Dispatch treats table/schema names and CSV output paths as operator intent, but
several paths interpolate those strings directly into Impala SQL or filesystem
paths. A malformed table name can currently move a generated CSV outside the
launch-time working directory, and identifier metacharacters can alter metadata
queries or table-export SQL. This plan adds one shared validation boundary so
the TUI, manifest builder, Browser helpers, and CSV orchestrator reject values
that are not plain Impala identifiers or safe filename stems.

## Current state

- `dispatch/screens/new_job.py` builds the CSV path directly from form input:
  - `L497-L506`: reads `schema`, `table`, splits `ExistingTable`, then sets `csv_path = str(self.launch_cwd / f"{table}.csv")`.
- `dispatch/manifest.py` repeats the fallback path and builds fully qualified names:
  - `L261-L265`: `full_table = f"{schema}.{table}" ...`; `csv_path = destination.get("csv_path") or str(launch_cwd / f"{table or 'dispatch_export'}.csv")`.
  - `L325-L330`: `ExistingTable` export passes `--table-name full_table --output-file csv_path`.
- `dispatch/impala.py` interpolates identifiers into metadata SQL:
  - `L39-L61`: `SHOW TABLES IN {schema} LIKE '{pattern}'`, `DESCRIBE {full_table}`, `DROP TABLE IF EXISTS {full_table}`.
- `dispatch/sql.py` constructs DDL and HDFS locations:
  - `L53-L63`: `DROP/CREATE TABLE {schema}.{table_name}` and `LOCATION '/das/{prefix}/enc/{user}/{table_name}'`.
- `scr/download_to_csv.py` builds export SQL from argv:
  - `L101-L105`: `query_to_run = f"select * from {args.table_name};"`.
- Product vocabulary to preserve:
  - `CONTEXT.md` defines `Source`, `Destination`, and `Job`; do not rename them.
  - ADR-0003 requires CSV files to be uncompressed and land in the launch-time CWD.
  - ADR-0005 says `scr/` changes must be narrow and mock-validated.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py tests/test_runner_integration.py -q` | exit 0 |
| Package syntax | `/workspace/.venv/bin/python -m compileall dispatch scr` | exit 0 |
| CLI smoke | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m dispatch --help` | exit 0 and prints help |

## Scope

**In scope**:
- `dispatch/sql.py`
- `dispatch/impala.py`
- `dispatch/manifest.py`
- `dispatch/screens/new_job.py`
- `scr/download_to_csv.py`
- `scr/monthly_query_processor.py` only for argv-boundary validation
- tests under `tests/`

**Out of scope**:
- Broad `scr/` refactors, retry-loop changes, email format changes, queue-list changes.
- Sanitizing user-authored SQL file contents; a `SqlFile` is intentionally arbitrary SQL.
- Changing CSV location semantics away from launch-time CWD.

## Git workflow

- Use a branch name like `cursor/harden-launch-identifiers-d0e6` if you are asked to execute this in this Cloud environment.
- Commit message style in recent history is concise imperative, e.g. `fix(impala): strip SHOW TABLES print_header row; record node03 validation`.

## Steps

### Step 1: Add shared identifier/path validators

Create validation helpers in `dispatch/sql.py`:
- `IDENTIFIER_RE`: allow `[A-Za-z_][A-Za-z0-9_]*`.
- `FULL_TABLE_RE`: allow `schema.table`, exactly one dot, both parts valid identifiers.
- `validate_identifier(value, label) -> str | None`.
- `validate_full_table(value, label="table") -> str | None`.
- `safe_csv_path(launch_cwd: Path, table: str) -> Path`: reject empty table, path separators, `..`, and values that fail identifier validation; resolve the resulting path and require it is inside `launch_cwd.resolve()`.

Keep validation conservative. If a legitimate corporate naming rule is broader, STOP and report instead of guessing.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py -q` -> existing tests pass.

### Step 2: Enforce validation in the New Job screen

Update `_validation_issues()` and `_validate()` in `dispatch/screens/new_job.py`:
- Validate `schema` when a destination needs a table.
- Validate `table-name` when a destination needs a table.
- Validate `existing-table` as a full table name for `ExistingTable`.
- Validate the computed CSV path for `Csv` and `Table+Csv`.

Update `_source_destination()` to call `sql.safe_csv_path(self.launch_cwd, table)` and store its string result. Do not use string concatenation for CSV paths.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py -q` -> pass.

### Step 3: Re-check boundaries in manifest and metadata helpers

In `dispatch/manifest.py`, validate `schema`, `table`, `full_table`, and fallback `csv_path` before constructing argv. In `dispatch/impala.py`, validate `schema` for `show_tables`, `full_table` for `describe_table` and `drop_table`, and escape or reject single quotes in `pattern`.

Validation here is defense-in-depth for hand-edited manifests and direct helper calls; keep error messages clear.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py -q` -> pass.

### Step 4: Add orchestrator-entry validation for CSV exports

In `scr/download_to_csv.py`, validate `--table-name` before building `select * from ...`. Keep this stdlib-only. Do not import `dispatch.sql` from `scr/`; either duplicate a tiny regex or add a stdlib-only helper under `scr/_common.py` if it stays narrow.

Only validate `--table-name`. Do not inspect SQL from `--query-file`, because user-authored SQL is intentional.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_runner_integration.py -q` -> pass.

### Step 5: Add regression tests

Add table-driven tests that assert:
- Safe identifiers pass: `aa_enc.dispatch_smoke_1`.
- `schema.table.extra`, `schema.bad-name`, `schema.t;drop`, and empty names fail.
- A `table-name` containing `/`, `\`, or `..` cannot produce a CSV path outside `launch_cwd`.
- `manifest.build_orchestrator_calls()` rejects unsafe `ExistingTable` and unsafe table destinations.
- `impala.show_tables/describe/drop` reject unsafe identifiers before calling `impala-shell`.

Use existing `tests/test_pure_logic.py` patterns for manifest helpers. If async tests are needed for `impala.py`, follow existing pytest-asyncio style in the suite.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py tests/test_runner_integration.py -q` -> all pass and new cases fail without the implementation.

## Test plan

- Unit tests for new validators in `tests/test_pure_logic.py` or a new `tests/test_sql.py`.
- Manifest argv tests beside `TestBuildOrchestratorCalls`.
- Runner integration smoke for a normal safe `SqlFile -> Csv` job to prove valid launches still work.
- Optional focused TUI pilot only if existing New Job validation tests already cover field messages; otherwise keep this plan at helper/manifest level.

## Done criteria

- [ ] Unsafe identifiers and path traversal table names are rejected before manifest creation.
- [ ] `manifest.build_orchestrator_calls()` never creates a CSV output path outside `launch_cwd`.
- [ ] `scr/download_to_csv.py --table-name` rejects invalid full table identifiers.
- [ ] Focused tests and compileall commands in this plan exit 0.
- [ ] No broad `scr/` behavior changed beyond validating `--table-name`.
- [ ] `plans/README.md` row for Plan 001 is updated.

## STOP conditions

Stop and report if:
- Production requires table names outside `[A-Za-z_][A-Za-z0-9_]*`.
- The fix requires changing `scr/` public flag names, email formats, retry timing, or queue lists.
- Tests reveal existing manifests in fixtures rely on unsafe names.
- Any in-scope file changed since `a50c81d` and the excerpts no longer match.

## Maintenance notes

- Future Browser actions and `ExistingTable -> Csv` shortcuts must reuse the same validators.
- Reviewers should scrutinize false positives: too-strict validation is safer than SQL/path injection, but can block legitimate naming conventions if the regex is wrong.
- If broader corporate identifier support is needed, record it in `CONTEXT.md` or an ADR before expanding the allowlist.
