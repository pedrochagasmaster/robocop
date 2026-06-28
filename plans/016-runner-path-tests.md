# Plan 016: Runner integration tests for Table / Table+Csv / SqlTemplate / ExistingTable paths

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- tests/test_runner_integration.py dispatch/manifest.py dispatch/runner.py`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: plans/002-stale-running-reaper.md (the reaper test asserts
  recovery after `kill -9`; land 002 first so this plan can add that
  assertion)
- **Category**: tests
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

`tests/test_runner_integration.py` exercises the detached runner end-to-end
only for `SqlFile → Csv`. The other four legal launch cells — `SqlFile →
Table`, `SqlFile → Table+Csv`, `SqlTemplate → Table`, `ExistingTable → Csv`
— have **zero** integration coverage through the real `dispatch.runner` +
mock `impala-shell` boundary. The argv shape is unit-tested in
`test_pure_logic.py`, but the runner's sequential execution, the
Table+Csv decomposition (create table, then export), the partial-failure
mode (table created, CSV export fails), and the SqlTemplate monthly
substitution on disk are all unverified at the boundary the mock layer
(ADR-0004) exists to support.

## Current state

`tests/test_runner_integration.py:37-48` — `_create_csv_job()` hardcodes
`destination={"type": "Csv", ...}`; all tests in `TestRunnerLifecycle` use it.

`dispatch/manifest.py:269-332` — `build_orchestrator_calls` produces:
- `SqlFile → Table`: `Query_Impala_Parametrized.py --sql-file ... --table-name ...`
- `SqlFile → Table+Csv`: `Query_Impala_Parametrized.py` then
  `download_to_csv.py --table-name ... --output-file ...`
- `SqlTemplate → Table`: `monthly_query_processor.py --sql-file ...
  --schema ... --table-name ... --start-date ... --end-date ...`
- `ExistingTable → Csv`: `download_to_csv.py --table-name ... --output-file ...`

`dispatch/runner.py:117-128` — runs `orchestrator_calls` sequentially; stops
on first non-zero exit with `state="Failed"`.

`tests/conftest.py` — `mock_env` fixture sets `DISPATCH_SCR_DIR`,
`DISPATCH_MOCK_SCENARIO`, `MAILHOST`, etc. so the runner can spawn the real
`scr/*.py` against the mock `impala-shell`.

**Repo conventions**: `test_runner_integration.py` uses `mock_env`, spawns
`dispatch.runner` as a subprocess, polls the manifest until a terminal state,
and asserts state + exit_code + run.log content. The mock `impala-shell`
honors `DISPATCH_MOCK_SCENARIO` (`happy_path` succeeds; `syntax_error`/
`auth_error` fail fast; `memory_exceeded` is transient then succeeds;
`all_queues_full` cycles forever).

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Tests     | `python -m pytest tests/test_runner_integration.py -q` | all pass |

The runner tests are POSIX-only where they use real signals; the happy-path
lifecycle tests run on Windows too (they spawn Python, not `setsid`
directly — verify by reading the existing test for `skipif` markers).

## Scope

**In scope**:
- `tests/test_runner_integration.py` — add job factories and lifecycle tests
  for the four untested cells.

**Out of scope**:
- `dispatch/manifest.py`, `dispatch/runner.py` — no code changes; this plan
  adds tests only.
- `tests/test_pure_logic.py` — argv-shape tests already exist; this plan is
  runner-integration only.
- The reaper (`kill -9`) test — add one assertion here if Plan 002 landed;
  the reaper itself is 002's scope.

## Git workflow

- Branch: `advisor/016-runner-path-tests`
- Commit per step; message style: `test(runner): cover Table/Table+Csv/SqlTemplate/ExistingTable paths`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add job factories mirroring `_create_csv_job`

Read `_create_csv_job` at `test_runner_integration.py:37-48` and clone it for
each cell. Each factory writes a manifest with the right `source`/
`destination`/`orchestrator_calls` and a `job.sql` (or no SQL for
ExistingTable) into a tmp job dir. Use `manifest.create_job` (the real
builder) rather than hand-writing `orchestrator_calls`, so the test exercises
the real call construction.

```python
def _create_table_job(tmp_path, mock_env, sql_text="SELECT 1"):
    from dispatch import manifest
    source = {"type": "SqlFile", "sql_path_at_launch": str(tmp_path / "q.sql")}
    (tmp_path / "q.sql").write_text(sql_text, encoding="utf-8")
    dest = {"type": "Table", "schema": "aa_enc", "table_name": "test_tbl"}
    job_dir, _ = manifest.create_job(
        source=source, destination=dest, params={"to_email": "", "subject": "t"},
        launch_cwd=tmp_path, sql_text=sql_text,
    )
    return job_dir

def _create_table_csv_job(tmp_path, mock_env, sql_text="SELECT 1"):
    # like _create_table_job but dest = {"type": "Table+Csv", ...}

def _create_template_job(tmp_path, mock_env):
    sql_text = "SELECT * FROM t WHERE d BETWEEN {date_inicio} AND {date_fim}"
    # dest = {"type": "Table", "schema": "aa_enc", "table_name": "monthly"}
    # params includes start_date/end_date in orchestrator MM/DD/YYYY form

def _create_existing_table_job(tmp_path, mock_env, table="aa_enc.existing"):
    # source = {"type": "ExistingTable", "table_name": table}
    # dest = {"type": "Csv", "csv_path": str(tmp_path / "out.csv")}
```

The factories must use the `mock_env` fixture's `DISPATCH_DATA_ROOT` as the
jobs dir (the runner reads manifests from there). Match how `_create_csv_job`
does it.

### Step 2: Add happy-path lifecycle tests for each cell

For each factory, add a test that spawns the runner and asserts `Succeeded`:

```python
def test_table_job_reaches_succeeded(tmp_path, mock_env):
    job_dir = _create_table_job(tmp_path, mock_env)
    # spawn dispatch.runner --job-dir job_dir, poll manifest until terminal
    # (reuse the existing _run_runner / _poll_until_terminal helpers)
    final = _poll_until_terminal(job_dir)
    assert final["state"] == "Succeeded"
    assert final["exit_code"] == 0
```

Mirror the existing `TestRunnerLifecycle` happy-path test's spawn + poll
helpers. Do not duplicate them — reuse `_run_runner`/`_poll_until_terminal`
(or whatever the file names them) from the Csv tests.

### Step 3: Add Table+Csv partial-failure tests

The highest-value tests are the partial-failure modes unique to Table+Csv:

1. **Full success**: both calls succeed → `Succeeded`, CSV exists at
   `destination.csv_path`.
2. **Fail on call 1** (table create fails, e.g. `syntax_error` scenario):
   → `Failed`, CSV does NOT exist, manifest `exit_code` is the first call's
   non-zero rc.
3. **Fail on call 2** (table created, CSV export fails): the table exists in
   Impala (mock records it), but the manifest is `Failed`. This is the
   partial-success mode ADR-0003 calls out as the future "resume" target
   (DIRECTION-01). Assert `state == "Failed"` and that the first call's
   success is visible in `run.log`.

For the fail-on-call-2 case, the mock `impala-shell` scenario must make the
first `Query_Impala_Parametrized.py` call succeed and the second
`download_to_csv.py` call fail. Check `mocks/scenarios/` for a scenario that
distinguishes by call; if none exists, set `DISPATCH_MOCK_SCENARIO` to a
failing one and accept that call 1 also fails (then the test asserts
`Failed` with the first-call error). A truly call-2-only failure may require
a new scenario file — if so, STOP and report (adding a scenario is a mock
contract change, in scope but needs care).

### Step 4: Add a reaper-recovery test (if Plan 002 landed)

If `dispatch/jobs.py` has `reconcile_running` (Plan 002), add a test:

1. Spawn a runner with `all_queues_full` (or `slow`) so it stays Running.
2. `kill -9` the runner pid (read from `run.pid`).
3. Call `jobs.running_jobs(root)` (which triggers the reaper).
4. Assert the manifest transitions to `Failed` and `can_launch` recovers.

This test is POSIX-only (`kill -9` semantics); gate with
`@pytest.mark.skipif(os.name == "nt")` like the existing cancel test at
`:208-261`.

**Verify**: `python -m pytest tests/test_runner_integration.py -q` → all pass.

## Test plan

- New tests (in `tests/test_runner_integration.py`):
  - `test_table_job_reaches_succeeded`
  - `test_table_csv_job_reaches_succeeded`
  - `test_table_csv_job_fails_on_table_create`
  - `test_table_csv_job_fails_on_csv_export` (partial-success mode)
  - `test_template_job_reaches_succeeded`
  - `test_existing_table_job_reaches_succeeded`
  - `test_reaper_recovers_after_runner_kill` (if Plan 002 landed; POSIX-only)
- Structural pattern: the existing `TestRunnerLifecycle` Csv tests — reuse
  the spawn/poll helpers.
- Verification: `python -m pytest tests/test_runner_integration.py -q` → all
  pass.

## Done criteria

- [ ] `python -m pytest tests/test_runner_integration.py -q` exits 0; all
      new tests pass
- [ ] `python -m pytest tests -q` exits 0 (no regressions)
- [ ] `grep -n "_create_table_job\|_create_table_csv_job\|_create_template_job\|_create_existing_table_job" tests/test_runner_integration.py` returns the four factories
- [ ] No files outside `tests/test_runner_integration.py` are modified,
  unless Step 3 required a new mock scenario (report if so)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `_create_csv_job` at `:37-48` does not exist or was renamed (the factory
  pattern moved — find it before cloning).
- The existing spawn/poll helpers (`_run_runner`/`_poll_until_terminal` or
  equivalent) are not present (the test harness was restructured — find the
  new helpers).
- Step 3's call-2-only failure requires a new `mocks/scenarios/*.json` file
  that distinguishes by orchestrator script — STOP and report; adding a
  scenario is a mock contract change. If you add one, mirror the existing
  scenario JSON shape exactly and document it in `mocks/` per ADR-0004.
- The reaper test (Step 4) cannot be written because Plan 002 has not landed
  — skip Step 4 and note it in the report; do NOT block the rest of the plan
  on it.
- The SqlTemplate test requires `monthly_query_processor.py` to succeed
  against the mock and it doesn't — investigate the mock scenario the
  monthly script needs; STOP and report rather than modifying `scr/`.

## Maintenance notes

- These tests are the regression net for the four untested legal cells. When
  a new legal cell is added (per Plan 013's sibling, the legal-cell
  consolidation), add a factory + lifecycle test here.
- The Table+Csv partial-failure test (Step 3 case 3) is the characterization
  test for the future "resume" feature (DIRECTION-01). When resume lands,
  that test should be extended to assert the resume path re-runs only call 2.
- The reaper test (Step 4) should move to `test_pure_logic.py` or stay here
  depending on whether it needs the real runner; if it stays here, keep it
  POSIX-gated.
- Reviewer: confirm the tests use the `mock_env` fixture (not a hardcoded
  `DISPATCH_DATA_ROOT`) so they're isolated per test.
