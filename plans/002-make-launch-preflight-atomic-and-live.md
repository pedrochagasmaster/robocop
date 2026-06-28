# Plan 002: Make launch preflight live and enforce the Job cap at creation

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat a50c81d..HEAD -- dispatch/app.py dispatch/jobs.py dispatch/manifest.py dispatch/screens/new_job.py tests docs/adr/0001-jobs-as-on-disk-manifests.md`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: plans/001-harden-launch-identifiers-and-csv-paths.md
- **Category**: bug
- **Planned at**: commit `a50c81d`, 2026-06-28

## Why this matters

The product invariant says Dispatch must refuse missing/expiring Kerberos
tickets and more than two simultaneous `Running` Jobs. Today the New Job screen
uses a local Kerberos TTL snapshot, checks the cap before the user confirms,
and creates the manifest later without an atomic cap check. Two sessions, a
long confirmation pause, or a slow runner can exceed the cap or launch with an
expired ticket. This plan treats `Pending` as accepted work for launch-slot
purposes; Step 5 updates ADR-0001 so the documented invariant remains
unambiguous.

## Current state

- `dispatch/app.py` owns the app-wide Kerberos TTL:
  - `L94-L95`: `await self.refresh_kerberos()` then `self.set_interval(60.0, self.refresh_kerberos)`.
  - `L132-L134`: `self.kerberos_ttl = await kerberos.ticket_ttl_seconds()`.
- `dispatch/screens/new_job.py` uses a local snapshot:
  - `L155-L156`: `self.kerberos_ttl = await kerberos.ticket_ttl_seconds()`.
  - `L423-L426`: `_refresh_kerberos()` only disables the launch button from `self.kerberos_ttl`.
  - `L461-L474`: `_validate()` checks `jobs.can_launch()` and local `self.kerberos_ttl`.
  - `L577-L601`: `_validate()` runs before confirmation; after confirm, code calls `manifest.create_job()` and `process.launch_runner()` with no second preflight.
- `dispatch/jobs.py` counts only `Running`:
  - `L59-L64`: `running_jobs()` filters state `Running`; `can_launch()` returns `len(running_jobs) < RUNNING_CAP`.
- `dispatch/manifest.py` creates a `Pending` manifest before the runner flips it to `Running`:
  - `L195-L210`: state is `"Pending"` then `write(...)`.
- ADR-0001 currently says:
  - `L41-L42`: concurrency enforcement is just a count over `manifest.state == "Running"` and "no locks needed".
- Key excerpts for drift comparison:
  - `dispatch/screens/dashboard.py:129-133`: Dashboard mirrors app TTL with
    `self.watch(self.app, "kerberos_ttl", self._on_kerberos_change, init=True)`.
  - `dispatch/screens/new_job.py:577-601`: `_launch_flow()` validates, awaits
    `_confirm_launch(...)`, then creates the Job and launches the runner without
    a second validation.
  - `dispatch/jobs.py:59-64`: `running_jobs()` filters only `state == "Running"`;
    `can_launch()` checks `len(running_jobs(root)) < RUNNING_CAP`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py tests/test_new_features.py tests/test_runner_integration.py -q` | exit 0 |
| Full local gate | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests tools/prod_tui/tests -q` | exit 0 |
| Package syntax | `/workspace/.venv/bin/python -m compileall dispatch scr` | exit 0 |

## Suggested executor toolkit

- Read `.agents/skills/dispatch-textual-tui/SKILL.md` before editing
  `dispatch/screens/new_job.py`; launch validation must stay async-safe and
  must not block the Textual event loop.

## Scope

**In scope**:
- `dispatch/screens/new_job.py`
- `dispatch/jobs.py`
- `dispatch/manifest.py`
- `dispatch/app.py` only if needed for app-level TTL watcher plumbing
- `docs/adr/0001-jobs-as-on-disk-manifests.md` if lock/cap semantics change
- tests under `tests/`

**Out of scope**:
- Reworking the runner lifecycle or stale PID reconciliation; that is Plan 003.
- Any change to legal `(Source, Destination)` combinations.
- Queueing jobs instead of hard-refusing them.

## Git workflow

- Use a branch name like `cursor/live-launch-preflight-d0e6` if executing in this Cloud environment.
- Use one commit for the behavioral change and tests; update ADR text in the same commit if you add locking.

## Steps

### Step 1: Mirror app Kerberos TTL in New Job

In `NewJobScreen.on_mount`, follow the pattern already used by `DashboardScreen`:
- Watch `self.app.kerberos_ttl` with `init=True`.
- In the watcher, copy the value into `self.kerberos_ttl`, call `_refresh_kerberos()`, `_inline_validate()`, and `_update_validation_summary()`.
- Remove the direct `kerberos.ticket_ttl_seconds()` call from `NewJobScreen.on_mount`;
  the app-level TTL refresh is the single source of truth.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_new_features.py -q` -> pass.

### Step 2: Re-run preflight after confirmation

In `_launch_flow()`, after `confirmed` is true and immediately before
`manifest.create_job(...)`:
- Refresh Kerberos once with `await self.app.refresh_kerberos()` if the app is a `DispatchApp`.
- Run `_validate()` again.
- If validation fails, show and notify the error; do not create a manifest.

This catches a ticket expiring or a second session launching while the confirm
dialog was open.

Add or adjust a focused New Job test in `tests/test_phase1_safety.py`:
- Open a prefilled New Job screen.
- Monkeypatch the confirmation callback so the form waits long enough to flip
  `app.kerberos_ttl` to `299` before confirm returns true.
- Assert no new `manifest.json` is created and the screen shows/notifies a TTL
  error.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_phase1_safety.py -k 'confirm' -q` -> exit 0.

### Step 3: Treat `Pending` as occupying a launch slot

Add a helper in `dispatch/jobs.py`, for example `launch_slot_jobs(root=None)`,
that counts both `Pending` and `Running`. Change `can_launch()` to use it.

Rationale: a `Pending` Job is already accepted work and will shortly become
`Running`. Counting it prevents a slow runner from allowing a burst past the
two-Job cap.

**Verify**: add a unit test with two `Pending` manifests and assert `can_launch()` is false.

### Step 4: Enforce the cap under a filesystem lock at manifest creation

Add a small creation helper around `manifest.create_job()` rather than putting
TUI-only policy into the raw manifest writer. A suggested shape:
- In `dispatch/jobs.py`, add `create_job_if_slot_available(...)`.
- Acquire `config.jobs_dir() / ".dispatch-launch.lock"` using `fcntl.flock` on POSIX.
- Re-count `Pending` + `Running` while holding the lock.
- If at cap, raise a clear domain exception.
- Otherwise call `manifest.create_job(...)`.

Use this helper from `NewJobScreen._launch_flow()`. Keep `manifest.create_job()`
available for tests and low-level callers, but prefer the cap-enforcing helper
for real launches.

Add `tests/test_pure_logic.py::test_create_job_if_slot_available_serializes_one_remaining_slot`
or a similarly named test. Use two Python threads with one existing
`Running`/`Pending` slot consumed; assert exactly one of the two calls returns a
new job directory and exactly one new `manifest.json` appears.

**Verify**: `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py -k 'slot_available' -q` -> exit 0.

### Step 5: Update ADR-0001 narrowly

If you add the lock, update the ADR sentence that says "no locks needed".
Keep the manifest design intact: the TUI still supervises via files and the
runner still owns durable execution. The new lock is only for the launch-time
acceptance decision.

**Verify**: `git diff -- docs/adr/0001-jobs-as-on-disk-manifests.md` shows only the concurrency paragraph changed.

## Test plan

- Unit tests for `can_launch()` with `Pending`, `Running`, and terminal states.
- A race test for the new cap-enforcing creation helper.
- A New Job pilot or focused screen test for app TTL watcher behavior if existing test infrastructure can inspect the launch button/validation summary.
- Regression test for confirm-then-expire: no manifest is created after the second `_validate()` fails.

## Done criteria

- [ ] `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_pure_logic.py tests/test_phase1_safety.py tests/test_new_features.py tests/test_runner_integration.py -q` exits 0.
- [ ] `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests tools/prod_tui/tests -q` exits 0.
- [ ] New Job validation test proves `app.kerberos_ttl=299` disables/refuses launch without creating a manifest.
- [ ] Race test proves two concurrent launches cannot both consume one remaining slot.
- [ ] `rg 'manifest\\.create_job\\(' dispatch/screens/new_job.py` returns no direct call; real launches go through `jobs.create_job_if_slot_available(...)`.
- [ ] ADR text no longer claims no locking is needed.
- [ ] `plans/README.md` row for Plan 002 is updated.

## STOP conditions

Stop and report if:
- The target Edge Node filesystem does not support the lock primitive you plan to use.
- `fcntl.flock` is advisory/no-op on the target `/ads_storage` filesystem; do not ship a silent no-op lock.
- The fix would queue jobs instead of hard-refusing them.
- You need to change the manifest schema version to enforce the cap.
- A second validation would require blocking the Textual event loop.
- Any in-scope file changed since `a50c81d` and the excerpts no longer match.
- A step's verification fails twice after a reasonable fix attempt.

## Maintenance notes

- This plan closes the launch-acceptance race only. Stale `Running` manifests
  after crashes still require Plan 003.
- Reviewers should check that every failure path clearly tells the user why
  launch was refused and that the confirm dialog cannot create stale manifests.
