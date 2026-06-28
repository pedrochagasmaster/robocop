# Plan 001: Harden launch identifiers and CSV output paths

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat a50c81d..HEAD -- dispatch/screens/new_job.py dispatch/manifest.py dispatch/sql.py dispatch/impala.py scr/_common.py scr/download_to_csv.py scr/monthly_query_processor.py tests`
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
- `scr/monthly_query_processor.py` interpolates argv values into DDL:
  - `L101-L109`: builds `{args.schema}.{args.table_name}_temp_...` and an
    HDFS `LOCATION` using `args.user` and `args.table_name`.
- Key excerpts for drift comparison:
  - `dispatch/screens/new_job.py:497-506`:
    `schema = self._input_value("schema")`; `table = self._input_value("table-name")`;
    `existing = self._input_value("existing-table") or f"{schema}.{table}"`;
    `csv_path = str(self.launch_cwd / f"{table}.csv")`.
  - `dispatch/impala.py:39-61`: `show_tables`, `describe_table`, and
    `drop_table` pass formatted SQL strings to `query(...)`.
  - `scr/download_to_csv.py:101-105`: when `args.table_name` is present, it
    sets `query_to_run = f"select * from {args.table_name};"`.
- Product vocabulary to preserve:
  - `CONTEXT.md` defines `Source`, `Destination`, and `Job`; do not rename them.
  - ADR-0003 requires CSV files to be uncompressed and land in the launch-time CWD.
  - ADR-0005 says `scr/` changes must be narrow and mock-validated.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py tests/test_runner_integration.py -q` | exit 0 |
| Orchestrator boundary tests | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_scr_common.py tests/test_runner_integration.py -q` | exit 0 |
| Package syntax | `/workspace/.venv/bin/python -m compileall dispatch scr` | exit 0 |
| CLI smoke | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m dispatch --help` | exit 0 and prints help |

## Suggested executor toolkit

- Read `.agents/skills/dispatch-textual-tui/SKILL.md` before editing
  `dispatch/screens/new_job.py`; the form must remain keyboard-first and must
  not add blocking work to Textual callbacks.

## Scope

**In scope**:
- `dispatch/sql.py`
- `dispatch/impala.py`
- `dispatch/manifest.py`
- `dispatch/screens/new_job.py`
- `scr/_common.py` only for a tiny stdlib identifier validator
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

Add a focused test for `_source_destination()` or `_validation_issues()` so the
UI form path is covered without requiring a brittle terminal-output assertion.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_new_features.py tests/test_pure_logic.py -q` -> pass.

### Step 3: Re-check boundaries in manifest and metadata helpers

In `dispatch/manifest.py`, validate `schema`, `table`, `full_table`, and
fallback `csv_path` before constructing argv. In `dispatch/impala.py`, validate
`schema` for `show_tables`, `full_table` for `describe_table` and `drop_table`,
and reject `pattern` if it contains a single quote. Do not implement escaping in
this plan; rejection keeps the Browser contract simple and testable.

Validation here is defense-in-depth for hand-edited manifests and direct helper calls; keep error messages clear.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py -q` -> pass.

### Step 4: Add orchestrator-entry validation for CSV exports and monthly DDL

In `scr/_common.py`, add a tiny stdlib-only `validate_full_table(value: str) ->
bool` and `validate_identifier(value: str) -> bool`. Import them from:

- `scr/download_to_csv.py` to validate `--table-name` before building
  `select * from ...`.
- `scr/monthly_query_processor.py` to validate `--schema`, `--table-name`, and
  `--user` before building temp/final DDL and HDFS `LOCATION` strings.

Only validate `--table-name` for the CSV export table mode. Do not inspect SQL
from `--query-file`, because user-authored SQL is intentional.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_scr_common.py tests/test_runner_integration.py -q` -> pass.

### Step 5: Add regression tests

Add table-driven tests that assert:
- Safe identifiers pass: `aa_enc.dispatch_smoke_1`.
- `schema.table.extra`, `schema.bad-name`, `schema.t;drop`, and empty names fail.
- A `table-name` containing `/`, `\`, or `..` cannot produce a CSV path outside `launch_cwd`.
- `manifest.build_orchestrator_calls()` rejects unsafe `ExistingTable` and unsafe table destinations.
- `impala.show_tables/describe/drop` reject unsafe identifiers before calling `impala-shell`.

Use existing `tests/test_pure_logic.py` patterns for manifest helpers. If async tests are needed for `impala.py`, follow existing pytest-asyncio style in the suite.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py tests/test_new_features.py tests/test_scr_common.py tests/test_runner_integration.py -q` -> all pass.

## Test plan

- Unit tests for new validators in `tests/test_pure_logic.py` or a new `tests/test_sql.py`.
- Manifest argv tests beside `TestBuildOrchestratorCalls`.
- Runner integration smoke for a normal safe `SqlFile -> Csv` job to prove valid launches still work.
- Focused New Job test for `_source_destination()` or `_validation_issues()` so
  the form path rejects unsafe values before manifest creation.

## Done criteria

- [ ] `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py tests/test_new_features.py tests/test_scr_common.py tests/test_runner_integration.py -q` exits 0.
- [ ] `/workspace/.venv/bin/python -m compileall dispatch scr` exits 0.
- [ ] `rg 'f\"\\{table\\}\\.csv\"' dispatch` returns no matches.
- [ ] `manifest.build_orchestrator_calls()` tests prove CSV output paths stay inside `launch_cwd`.
- [ ] `scr/download_to_csv.py` and `scr/monthly_query_processor.py` reject invalid argv identifiers in tests.
- [ ] No broad `scr/` behavior changed beyond narrow argv-boundary validation.
- [ ] `plans/README.md` row for Plan 001 is updated.

## STOP conditions

Stop and report if:
- Production requires table names outside `[A-Za-z_][A-Za-z0-9_]*`.
- The fix requires changing `scr/` public flag names, email formats, retry timing, or queue lists.
- Tests reveal existing manifests in fixtures rely on unsafe names.
- Any in-scope file changed since `a50c81d` and the excerpts no longer match.
- A step's verification fails twice after a reasonable fix attempt.

## Maintenance notes

- Future Browser actions and `ExistingTable -> Csv` shortcuts must reuse the same validators.
- Reviewers should scrutinize false positives: too-strict validation is safer than SQL/path injection, but can block legitimate naming conventions if the regex is wrong.
- If broader corporate identifier support is needed, record it in `CONTEXT.md` or an ADR before expanding the allowlist.
