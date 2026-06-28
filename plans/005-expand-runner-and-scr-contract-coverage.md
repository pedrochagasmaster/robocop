# Plan 005: Expand runner and scr contract coverage before deeper orchestrator work

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat a50c81d..HEAD -- tests/test_runner_integration.py tests/test_pure_logic.py tests/test_mock_contract.py scr/_common.py mocks/scenarios dispatch/manifest.py docs/adr/0004-mock-layer-for-offline-dev.md docs/adr/0005-scr-modification-policy.md`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `a50c81d`, 2026-06-28

## Why this matters

ADR-0004 says the risky bugs live at the runner/orchestrator boundary, and
ADR-0005 requires strong mock proof before `scr/` changes. The current runner
integration suite exercises `SqlFile -> Csv`, but not table creation,
`Table+Csv` decomposition, `SqlTemplate -> Table`, or most error-classifier
branches. This plan builds the safety net needed before bounded retry changes,
query redaction, monthly cleanup, or other production-sensitive orchestrator
work.

## Current state

- `tests/test_runner_integration.py` has one factory:
  - `L37-L48`: `_create_csv_job()` always builds `SqlFile -> Csv`.
  - `L72-L137`: happy/error scenarios all spawn that same CSV job shape.
- `tests/test_pure_logic.py` covers argv shape but not subprocess execution:
  - `L245-L275`: asserts legal cells map to expected orchestrator scripts.
- `scr/_common.py` has untested core behavior:
  - `L12`: `FATAL_ERRORS = {"TABLE_NOT_FOUND", "SYNTAX_ERROR", "DUPLICATE_COLUMN", "AUTH_ERROR", "GENERIC_ERROR"}`.
  - `L35-L76`: `classificar_erro_impala()` maps stderr to categories.
  - `L79-L100`: `cycle_through_pools()` loops until success unless `max_cycles` is set.
- Mock scenarios exist for a subset:
  - `mocks/scenarios/` includes `happy_path`, `syntax_error`, `auth_error`, `table_not_found`, `memory_exceeded`, `all_queues_full`, and `slow`.
  - There are no scenarios for several classifier categories such as timeout, connection, backpressure, duplicate column, disk/space errors.
- Legal-cell manifest examples already exist at the unit level:
  - `tests/test_pure_logic.py:245-275` calls `manifest.build_orchestrator_calls(...)`
    for `SqlFile -> Csv`, `SqlFile -> Table`, `SqlFile -> Table+Csv`,
    `SqlTemplate -> Table`, and `ExistingTable -> Csv`.
- Category literals in `scr/_common.py` that must be characterized exactly:
  - Fatal set: `TABLE_NOT_FOUND`, `SYNTAX_ERROR`, `DUPLICATE_COLUMN`,
    `AUTH_ERROR`, `GENERIC_ERROR`.
  - Retriable/non-fatal classifier returns include `MEMORY_EXCEEDED`,
    `TIMEOUT`, `QUEUE_FULL`, `CONNECTION_ERROR`, `BACKPRESSURE`,
    `HOST_RESOLUTION_ERROR`, `HOST_UNREACHABLE`, `DISK_FULL`,
    `MEMORY_AVAILABLE`, and `SPACE_LIMIT`.
- ADR requirements:
  - ADR-0004 `L64-L70`: fake `impala-shell` argv drift is an integration bug; new classifications require matching scenarios.
  - ADR-0005 `L47-L53`: `scr/` changes require all mock scenarios and side-by-side behavioral proof.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Runner contract tests | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_runner_integration.py -q` | exit 0 |
| Mock/classifier tests | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_mock_contract.py tests/test_pure_logic.py -q` | exit 0 |
| Full local gate | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests tools/prod_tui/tests -q` | exit 0 |
| Package syntax | `/workspace/.venv/bin/python -m compileall dispatch scr` | exit 0 |

## Scope

**In scope**:
- `tests/test_runner_integration.py`
- `tests/test_pure_logic.py` or a new `tests/test_scr_common.py`
- `tests/test_mock_contract.py`
- `mocks/scenarios/*.json` only for adding classifier scenarios
- `mocks/bin/impala-shell` only if new scenario plumbing requires it

**Out of scope**:
- Changing production orchestrator behavior in `scr/`.
- Changing retry limits, classifier category meanings, or email contents.
- Adding slow/flaky real-cluster tests.

## Git workflow

- Use a branch name like `cursor/expand-runner-contract-tests-d0e6` if executing in this Cloud environment.
- This is a tests-only plan except for mock fixture additions.

## Steps

### Step 1: Add manifest factories for every legal runner path

In `tests/test_runner_integration.py`, add helpers mirroring `_create_csv_job()`:
- `_create_sqlfile_table_job()`
- `_create_sqlfile_table_plus_csv_job()`
- `_create_sqltemplate_table_job()`
- `_create_existingtable_csv_job()`

Use safe simple SQL and table names. Keep `MAILHOST=127.0.0.1:9` from the fixture so email attempts fail fast.

Factory shapes:
- `SqlFile -> Table`: `source={"type": "SqlFile", "sql_path_at_launch": str(sql_file)}`,
  `destination={"type": "Table", "schema": "aa_enc", "table_name": "dispatch_smoke_table"}`,
  `sql_text="SELECT 1 AS smoke_test_value"`.
- `SqlFile -> Table+Csv`: same source; destination type `Table+Csv` with
  `csv_path=str(tmp_path / "dispatch_smoke_table.csv")`.
- `SqlTemplate -> Table`: SQL text contains both `{date_inicio}` and
  `{date_fim}`; params include `start_date="01/01/2026"` and
  `end_date="01/31/2026"`.
- `ExistingTable -> Csv`: `source={"type": "ExistingTable", "table_name": "aa_enc.dispatch_smoke_existing"}`,
  destination type `Csv`, schema/table matching the full table, and explicit
  `csv_path`.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_runner_integration.py -q` -> exit 0 after factories are added.

### Step 2: Exercise legal cells through the real runner

Add runner subprocess tests:
- `SqlFile -> Table` reaches `Succeeded` under `happy_path` and uses `Query_Impala_Parametrized.py`.
- `SqlFile -> Table+Csv` reaches `Succeeded`, has two orchestrator calls in order, and writes a plain CSV in `launch_cwd`.
- `ExistingTable -> Csv` reaches `Succeeded` and uses `--table-name`, not `--query-file`.
- `SqlTemplate -> Table` should run under `happy_path` for a one-month range.
  If current mock behavior cannot support the full monthly join, add a single
  `pytest.mark.skip` named `test_sqltemplate_table_runner_contract` with a TODO
  issue URL in the skip reason; do not silently omit the legal cell.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_runner_integration.py -q` -> exit 0.

### Step 3: Add direct classifier characterization tests

Create `tests/test_scr_common.py` or add a section to `tests/test_pure_logic.py`.
Import `scr/_common.py` safely by adding `scr/` to `sys.path` inside the test,
matching how orchestrators import it.

Cover at least:
- each `FATAL_ERRORS` member maps to fatal behavior,
- `MEMORY_EXCEEDED`, `TIMEOUT`, `QUEUE_FULL`, `CONNECTION_ERROR`,
  `BACKPRESSURE`, `HOST_RESOLUTION_ERROR`, `HOST_UNREACHABLE`, `DISK_FULL`,
  `MEMORY_AVAILABLE`, and `SPACE_LIMIT`,
- unmatched stderr maps to `GENERIC_ERROR` exactly as current behavior.

These are characterization tests; do not change behavior in this plan.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_scr_common.py -q` -> pass.

### Step 4: Add missing mock scenario files for classifier branches

For each category not represented in `mocks/scenarios/`, add a minimal JSON
scenario following existing files. Keep delays at zero/default for CI. Use this
matrix unless the live scenario schema proves it impossible, in which case STOP
and report the specific schema mismatch:

| Category | Scenario file | Status |
|---|---|---|
| `SYNTAX_ERROR` | `syntax_error.json` | existing |
| `AUTH_ERROR` | `auth_error.json` | existing |
| `TABLE_NOT_FOUND` | `table_not_found.json` | existing |
| `MEMORY_EXCEEDED` | `memory_exceeded.json` | existing |
| `QUEUE_FULL` | `all_queues_full.json` | existing, test only with bounded/mock-contract path |
| `TIMEOUT` | `timeout.json` | add |
| `CONNECTION_ERROR` | `connection_error.json` | add |
| `BACKPRESSURE` | `backpressure.json` | add |
| `HOST_RESOLUTION_ERROR` | `host_resolution_error.json` | add |
| `HOST_UNREACHABLE` | `host_unreachable.json` | add |
| `DISK_FULL` | `disk_full.json` | add |
| `SPACE_LIMIT` | `space_limit.json` | add |
| `DUPLICATE_COLUMN` | `duplicate_column.json` | add |
| `GENERIC_ERROR` | `generic_error.json` | add |

Update `tests/test_mock_contract.py` so every scenario can be loaded and produces the expected stderr/category path.

**Verify**: mock/classifier test command exits 0.

### Step 5: Document the coverage matrix in tests

Add a concise comment/table in `tests/test_runner_integration.py` listing legal cells and scenarios covered. This prevents future regressions where new legal cells or categories are added without a runner/mock test.

**Verify**: full local gate exits 0.

## Test plan

- New runner integration tests for legal cells.
- New direct classifier tests for `scr/_common.py`.
- Mock scenario contract tests for each classifier scenario file.
- Full suite, because this plan touches shared fixtures and scenario files.

## Done criteria

- [ ] `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_runner_integration.py -q` exits 0.
- [ ] `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_mock_contract.py tests/test_scr_common.py -q` exits 0.
- [ ] `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests tools/prod_tui/tests -q` exits 0.
- [ ] Runner integration covers all five legal `(Source, Destination)` cells, or the single `SqlTemplate -> Table` test is explicitly skipped with a GitHub issue URL.
- [ ] `scr/_common.py` classifier behavior is characterized without changing production behavior.
- [ ] Mock scenarios exist for every classifier category in the matrix above, except any gap with a TODO issue URL.
- [ ] No production `scr/` behavior changed.
- [ ] `plans/README.md` row for Plan 005 is updated.

## STOP conditions

Stop and report if:
- A legal cell cannot be run locally because the mock `impala-shell` lacks required argv support.
- Adding scenario coverage requires changing production orchestrator behavior.
- A test would need to wait for the intentionally infinite `all_queues_full` loop.
- Any in-scope file changed since `a50c81d` and the excerpts no longer match.
- A step's verification fails twice after a reasonable fix attempt.

## Maintenance notes

- This plan is a prerequisite for high-risk `scr/` improvements such as bounded retries, SQL redaction, monthly cleanup, or classifier changes.
- Reviewers should reject any diff that "fixes" tests by weakening the mock contract rather than representing real orchestrator argv/stderr behavior.
