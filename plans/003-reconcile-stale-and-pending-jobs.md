# Plan 003: Reconcile stale Running and orphan Pending Jobs

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat a50c81d..HEAD -- dispatch/jobs.py dispatch/process.py dispatch/screens/job_detail.py dispatch/screens/new_job.py dispatch/runner.py tests`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: plans/002-make-launch-preflight-atomic-and-live.md
- **Category**: bug
- **Planned at**: commit `a50c81d`, 2026-06-28

## Why this matters

Dispatch relies on manifests as the durable Job record, but a process can die
without updating its manifest. A `Running` manifest with no live runner can
permanently consume the two-Job cap, and a `Pending` manifest left after runner
spawn failure is displayed but not cancellable. This plan adds conservative
reconciliation so the TUI can recover from edge-node restarts, SIGKILL, missing
`nohup`/`setsid`, and dead PIDs without manual JSON edits.

## Current state

- `dispatch/jobs.py` has no liveness reconciliation:
  - `L38-L56`: `list_manifests()` loads JSON only.
  - `L59-L64`: `running_jobs()` and `can_launch()` trust `state == "Running"`.
- `dispatch/runner.py` writes terminal state only on ordinary paths:
  - `L45-L80`: SIGTERM handler sets `Cancelled`.
  - `L117-L132`: normal orchestrator completion sets `Succeeded`/`Failed`; unhandled errors set `Failed`.
  - No path handles SIGKILL, host reboot, or runner disappearance.
- `dispatch/process.py` cancellation has no error handling:
  - `L44-L45`: `os.killpg(pid, signal.SIGTERM)`.
- `dispatch/screens/job_detail.py` offers Cancel for `Pending` but ignores it:
  - `L252-L253`: cancel button visible for `Running` or `Pending`.
  - `L432-L443`: `_cancel_flow()` only acts when state is `Running` and `pid`.
- `dispatch/screens/new_job.py` creates before launching:
  - `L594-L601`: `manifest.create_job(...)` then `await process.launch_runner(job_dir)` with no rollback on spawn failure.
- Expected state after Plan 002:
  - Real launches should call `jobs.create_job_if_slot_available(...)` rather
    than `manifest.create_job(...)` directly.
  - Launch-slot counting should treat `Pending` and `Running` Jobs as occupying
    capacity.
  - If those symbols do not exist, stop and execute Plan 002 first.
- Key excerpts for drift comparison:
  - `dispatch/process.py:44-45`: `cancel_process_group(pid)` calls
    `os.killpg(pid, signal.SIGTERM)` with no error handling.
  - `dispatch/screens/job_detail.py:432-443`: `_cancel_flow()` only confirms and
    calls `process.cancel_process_group(pid)` when `item["state"] == "Running"`
    and `pid` is present.
  - `dispatch/runner.py:117-132`: normal runner paths set terminal state, but
    SIGKILL/host reboot cannot execute those paths.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused lifecycle tests | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_runner_integration.py tests/test_process.py tests/test_new_features.py -q` | exit 0 |
| Full local gate | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests tools/prod_tui/tests -q` | exit 0 |
| Package syntax | `/workspace/.venv/bin/python -m compileall dispatch scr` | exit 0 |

## Suggested executor toolkit

- Read `.agents/skills/dispatch-textual-tui/SKILL.md` before editing
  `dispatch/screens/job_detail.py` or `dispatch/screens/new_job.py`; cancel and
  launch error handling must stay async-safe and worker-friendly.

## Scope

**In scope**:
- `dispatch/jobs.py`
- `dispatch/process.py`
- `dispatch/screens/job_detail.py`
- `dispatch/screens/new_job.py`
- tests under `tests/`

**Out of scope**:
- Changing `JobState` values or manifest `schema_version`.
- Killing or reaping orchestrator grandchildren beyond the existing process-group model.
- Retrying failed Jobs or resuming partial `Table+Csv` steps.

## Git workflow

- Use a branch name like `cursor/reconcile-stale-jobs-d0e6` if executing in this Cloud environment.
- Keep this as a single logical commit because the UI and helper behavior must agree.

## Steps

### Step 1: Add a conservative PID liveness helper

In `dispatch/jobs.py`, add helpers such as:
- `pid_is_alive(pid: int) -> bool` using `os.kill(pid, 0)` on POSIX.
- `reconcile_manifest(path: Path) -> JobManifest | None`.

Rules:
- Only reconcile `Running` with a non-null `pid` when `pid_is_alive(pid)` is false.
- Mark it `Failed`, set `exit_code=-1`, set `finished_at=manifest.now_utc()`.
- Append one `[dispatch] stale runner pid ...` line to `run.log` only if the
  log file exists or can be opened safely. If no log exists, skip the log write;
  do not add a manifest sidecar in this plan.
- Do not declare `Pending` stale solely by age in the first implementation unless product agrees on an age threshold.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_jobs_reconcile.py -k 'stale_running' -q` -> exit 0.

### Step 2: Reconcile during hot reads without hiding corrupt manifests

Add `jobs.reconciled_list_manifests(root=None)` and use it from:
- `running_jobs`
- `can_launch`
- `launch_slot_jobs` from Plan 002
- `active_jobs`

Do not mutate inside bare `list_manifests()`; keep it as the low-level read
primitive for callers that explicitly want raw manifests.

The dashboard, history, and launch cap paths should see reconciled state before counting or rendering.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_jobs_reconcile.py -k 'running_jobs_reconcile' -q` -> exit 0.

### Step 3: Handle dead PIDs in cancel

Update `dispatch/process.cancel_process_group(pid)` to either:
- catch `ProcessLookupError` and return a structured result, or
- let callers catch it with a clear branch.

Update `JobDetailScreen._cancel_flow()`:
- If `Running` and PID is dead, reconcile and notify "Job process is no longer running; manifest marked Failed".
- If `PermissionError` occurs, notify an error and do not mutate the manifest.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_jobs_reconcile.py -k 'cancel_dead_pid' -q` -> exit 0 and asserts manifest `state == "Failed"` with no uncaught exception.

### Step 4: Make Pending cancel explicit

For `Pending` Jobs:
- If there is no `pid`, prompt with "Remove Pending Job" rather than "Cancel Job".
- On confirm, mark the manifest `Cancelled` with `exit_code=0` and `finished_at`.
- Never delete the job directory in this plan; preserving the manifest keeps the
  audit trail intact.
- If `Pending` has a `pid`, send SIGTERM to that process group.
- Always notify on no-op paths.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_jobs_reconcile.py -k 'pending_cancel' -q` -> exit 0.

### Step 5: Roll back or fail manifest on runner spawn failure

After `NewJobScreen._launch_flow()` successfully creates a Job through
`jobs.create_job_if_slot_available(...)`, wrap `await process.launch_runner(job_dir)`:
- On `OSError` or subprocess creation failure, mark the manifest `Failed` with `exit_code=-1` and `finished_at`.
- Notify the user with the spawn error.
- Do not leave a bare `Pending` Job.

Do not delete the job directory; preserving `job.sql` and manifest makes the failure diagnosable.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_new_features.py -k 'launch_runner_failure' -q` -> exit 0.

## Test plan

- Unit tests in `tests/test_pure_logic.py` or a new `tests/test_jobs_reconcile.py` for stale PID reconciliation.
- Existing `tests/test_runner_integration.py` cancellation test remains green.
- New Job screen test for launch-runner failure path if existing pilot tests can call `_launch_flow`; otherwise test a factored helper.
- Job Detail test for Pending cancellation.

## Done criteria

- [ ] `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_jobs_reconcile.py tests/test_runner_integration.py tests/test_process.py tests/test_new_features.py -q` exits 0.
- [ ] `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests tools/prod_tui/tests -q` exits 0.
- [ ] Dead `Running` PID manifests reconcile to `Failed` before `running_jobs()`/`can_launch()` count them.
- [ ] Pending/no-pid Jobs transition to `Cancelled` through Job Detail; no job directory is deleted.
- [ ] Runner spawn failure leaves a diagnosable `Failed` manifest, not `Pending`.
- [ ] `rg 'manifest\\.create_job\\(' dispatch/screens/new_job.py` still returns no direct call after Plan 002.
- [ ] `plans/README.md` row for Plan 003 is updated.

## STOP conditions

Stop and report if:
- The only viable reconciliation requires adding a new `JobState`.
- PID reuse cannot be mitigated enough with current manifest fields.
- Tests need to kill real unrelated processes to prove behavior.
- The fix requires changing runner process-group creation semantics.
- Any in-scope file changed since `a50c81d` and the excerpts no longer match.
- Plan 002 has not landed and `jobs.create_job_if_slot_available(...)` is absent.
- A step's verification fails twice after a reasonable fix attempt.

## Maintenance notes

- Reconciliation should be noisy enough for support: append a small `[dispatch]` log line when a stale manifest is marked terminal.
- Reviewers should check that the code never kills by process name and never targets anything except the stored PID/process group.
- A future resume feature should treat reconciled `Failed` Jobs the same as any other failed Job.
- Stale `Pending` Jobs without a PID are handled only by the explicit manual
  cancel path in this plan; automatic age-based Pending reconciliation is
  intentionally deferred until product chooses an age threshold.
