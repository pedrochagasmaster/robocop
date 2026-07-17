# PR #57 Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PR #57 safe to merge by hardening MonthlyJob statement generation, enforcing a shared two-slot Dispatch capacity limit, restoring keyboard-accessible Browser selection, and supplying required validation evidence.

**Architecture:** A deep `dispatch/capacity.py` module owns the private file-locked ledger, stale recovery, FIFO launch intents, and typed errors. `dispatch/jobs.py` delegates atomic launch admission to that module; `dispatch/impala.py` uses short-lived metadata leases without reading ledger details. Monthly SQL normalization remains private to the standard-library-only orchestrator script.

**Tech Stack:** Python 3.10+, asyncio, POSIX `flock`/Windows `msvcrt`, Textual 8.2.5, pytest.

## Global Constraints

- Keep `scr/` standard-library-only and satisfy ADR-0005.
- Count all Dispatch-managed Impala commands and every `Pending`/`Running` job against two shared slots.
- Share capacity only between processes using the same configured data root.
- Metadata fails fast; launches wait at most 30 seconds behind metadata, never behind two active jobs.
- Ledger directories are `0700`; ledger and lock files are `0600`; malformed or unwritable state fails closed.
- Keep blocking filesystem work off the Textual event loop.
- Preserve Space as inactive; add `X` for keyboard selection while retaining mouse selection.
- Do not commit generated screenshots, logs, mock emails, or runtime state.

---

### Task 1: Harden MonthlyJob statement separators

**Files:**
- Modify: `scr/monthly_query_processor.py`
- Modify: `tests/test_monthly_query_processor.py`

**Interfaces:**
- Produces: `_strip_terminal_semicolon(sql: str) -> str`
- Preserves: `build_monthly_job_query(args, sql_template) -> tuple[str, list[str], str]`

- [ ] Add parameterized regressions for plain SQL, a trailing semicolon, a trailing `--` comment, a trailing block comment, and quoted semicolons. Each assertion must prove generated delimiters remain outside comments and every generated CREATE/DROP is separable.
- [ ] Run `.venv/bin/python -m pytest tests/test_monthly_query_processor.py -q`; confirm the line-comment case fails because the delimiter is inside the comment.
- [ ] Add a private comment/quote-aware scanner that removes only a final statement delimiter outside trailing comments. Render the generated delimiter on its own line:

```python
monthly_sql = _strip_terminal_semicolon(
    render_monthly_sql(sql_template, date_inicio_str, date_fim_str)
)
statements.append(
    f"""
        DROP TABLE IF EXISTS {temp_table_name};
        CREATE TABLE {temp_table_name}
        STORED AS parquet LOCATION '{location}'
        AS
        {monthly_sql}
        ;
    """
)
```

- [ ] Re-run the focused test and commit with `fix: harden monthly SQL separators`.

### Task 2: Build the shared capacity module

**Files:**
- Create: `dispatch/capacity.py`
- Create: `tests/test_capacity.py`

**Interfaces:**
- Produces: `CapacityBusy`, `CapacityTimeout`, `CapacityLedgerError`
- Produces: `MetadataLease.release() -> None`
- Produces: `try_acquire_metadata(operation: str, root: Path | None = None) -> MetadataLease`
- Produces: `admit_launch(create_pending: Callable[[], T], timeout: float = 30, root: Path | None = None) -> T`

- [ ] Write process-level tests using one temporary root. Prove two processes acquire at most two metadata leases, dead-PID leases are reclaimed, live leases are not reclaimed, FIFO launch intents are ordered, waiting launches suppress new stats leases, malformed JSON fails closed, and file modes are private.
- [ ] Run `.venv/bin/python -m pytest tests/test_capacity.py -q`; confirm failures are caused by the missing module/interface.
- [ ] Implement a versioned JSON ledger guarded by a dedicated lock file. All reads, stale reconciliation, admission decisions, and atomic replacements occur while holding the cross-platform lock.
- [ ] Represent metadata owners with PID, operation, and creation timestamp; represent launch intents with PID, sequence, and timestamp; represent job reservations with job ID and manifest path.
- [ ] Make `MetadataLease` idempotently remove only its own token. Reconcile job reservations from `Pending`/`Running` manifests and reclaim terminal, missing, expired-Pending, and dead-runner entries through existing manifest semantics.
- [ ] Re-run focused tests and commit with `feat: add shared Dispatch capacity ledger`.

### Task 3: Integrate jobs and Impala with shared capacity

**Files:**
- Modify: `dispatch/jobs.py`
- Modify: `dispatch/impala.py`
- Modify: `dispatch/screens/new_job.py`
- Modify: `dispatch/screens/browser.py`
- Modify: `tests/test_new_features.py`
- Modify: `tests/test_new_job_queue.py`

**Interfaces:**
- Consumes: capacity interfaces from Task 2
- Removes: `QueryLedger`, `query_ledger`, `reset_query_ledger_for_tests`, and monkeypatch-sensitive `_PRODUCTION_TABLE_STATS`

- [ ] Write failing tests proving `SHOW TABLES`, `SHOW TABLE STATS`, `DESCRIBE`, and `DROP` each lease a slot; stats return unknown only for `CapacityBusy`; ledger failures propagate; two active jobs reject launch immediately; metadata occupancy queues launch for at most 30 seconds; cancellation removes launch intent.
- [ ] Run the focused tests and confirm the process-local implementation violates cross-process and launch-wait expectations.
- [ ] Move launch admission and Pending-manifest creation behind `capacity.admit_launch`. The callback creates the manifest while capacity still owns the admission lock.
- [ ] Wrap metadata acquisition/release in `asyncio.to_thread()`. Do not poll metadata. Use cancellable 250ms async retries only for the bounded launch queue, with cleanup in `finally`.
- [ ] Remove inline imports and monkeypatch-aware production branches. Tests exercise the real lease path and mock the subprocess seam instead.
- [ ] Surface typed capacity errors through New Job and Browser status/notification paths.
- [ ] Re-run focused tests and commit with `feat: enforce shared query capacity`.

### Task 4: Restore Browser keyboard access and real interaction coverage

**Files:**
- Modify: `dispatch/screens/browser.py`
- Modify: `dispatch/screens/help.py`
- Modify: `tests/test_ui_ux_closure.py`

**Interfaces:**
- Produces: Browser binding `x -> toggle_check`
- Preserves: Space inactive, Enter Describe, A Select All, mouse checkbox selection

- [ ] Replace direct message-handler tests with Pilot interactions that click the actual Sel cell, press `x`, and click the Size header twice. Assert selection, visible `[X]`, drop enablement, and ascending/descending order.
- [ ] Run the focused Pilot tests; confirm `x` currently does not select a row.
- [ ] Add the `x` binding and `action_toggle_check`, update help/status copy, and retain `BrowserTable` mouse handling.
- [ ] Validate layouts at 80×24, 100×30, and 120×40.
- [ ] Re-run focused tests and commit with `fix: restore keyboard Browser selection`.

### Task 5: Clean the touched modules

**Files:**
- Modify: `dispatch/capacity.py`
- Modify: `dispatch/impala.py`
- Modify: `dispatch/jobs.py`
- Modify: `dispatch/screens/browser.py`
- Modify: `scr/monthly_query_processor.py`
- Modify: related tests

**Interfaces:**
- Preserves all interfaces and behavior established in Tasks 1–4.

- [ ] Remove duplicate admission/release shapes, test-only production seams, dead imports, and unclear touched-area names.
- [ ] Run all focused tests before and after cleanup and compare results.
- [ ] Commit with `refactor: clean capacity and Browser paths`.

### Task 6: Validate and document

**Files:**
- Update PR description/comment only; do not commit generated evidence.

- [ ] Run `.venv/bin/python -m compileall dispatch scr`.
- [ ] Run `.venv/bin/ruff check dispatch tests`.
- [ ] Run `.venv/bin/ruff check scr`.
- [ ] Run `.venv/bin/ruff format --check dispatch tests`.
- [ ] Run `.venv/bin/mypy dispatch/sql.py dispatch/jobs.py dispatch/manifest.py`.
- [ ] Run `.venv/bin/python -m pytest`.
- [ ] Run every scenario represented under `mocks/scenarios/` and capture temporary old/new generated-SQL diffs for the MonthlyJob inputs.
- [ ] Exercise the mocked TUI at 80×24, 100×30, and 120×40; record one concise walkthrough showing checkbox click, `X`, and Size-header sorting.
- [ ] Push the branch, create a draft PR against `main`, and include `[scr/]` safety/risk, exact validation results, generated-SQL equivalence evidence, release risk, and the ADR-0005 reviewer requirement.

## Self-Review

- Spec coverage: all nine review findings and every grilling decision map to Tasks 1–6.
- Placeholder scan: no deferred implementation or test placeholders remain.
- Type consistency: Task 2 owns capacity types; Tasks 3–5 consume that exact interface.
