# Plan 004: Bound manifest refresh work for SSH-scale supervision

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat a50c81d..HEAD -- dispatch/jobs.py dispatch/screens/dashboard.py dispatch/screens/new_job.py dispatch/screens/job_detail.py dispatch/screens/history.py tests`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: plans/003-reconcile-stale-and-pending-jobs.md
- **Category**: perf
- **Planned at**: commit `a50c81d`, 2026-06-28

## Why this matters

Dispatch runs over SSH/VPN and reads manifests from edge-node storage. The
dashboard refreshes every two seconds, New Job validation runs on every
keystroke, and both paths currently scan all lifetime job directories. As
analysts accumulate historical Jobs, UI latency and terminal repaint churn grow
with total history rather than with the active seven-day supervision window.

## Current state

- `dispatch/jobs.py` scans every job directory:
  - `L38-L46`: `base.glob("*/manifest.json")` and loads each manifest.
  - `L67-L74`: `active_jobs()` filters to running/recent only after loading all manifests.
  - `L77-L84`: `history_jobs()` also loads all manifests.
- `dispatch/screens/dashboard.py` runs the hot path every two seconds:
  - `L162-L165`: `_refresh_jobs_async()` calls `await asyncio.to_thread(jobs.active_jobs)`.
  - `L182-L185`: then reads a log tail for the detail pane.
  - `L401-L416`: completion notifications are tied to this dashboard refresh path.
- `dispatch/screens/new_job.py` scans during typing:
  - `L270-L272`: every input change updates validation summary.
  - `L335-L336`: validation summary calls `jobs.can_launch()`.
- Timers are not paused on hidden screens:
  - `dispatch/app.py:L229-L231` keeps Dashboard on the stack.
  - `dispatch/screens/dashboard.py:L135` sets an interval; there is no `on_hide` pause.
- TUI skill rules say:
  - `dispatch-textual-tui/SKILL.md:L246-L250`: do not re-read every manifest on every paint; keep dashboard refresh work bounded and cancellable.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests/test_cockpit.py tests/test_new_features.py tests/test_pure_logic.py -q` | exit 0 |
| Full local gate | `source mocks/dev-env.sh && /workspace/.venv/bin/python -m pytest tests tools/prod_tui/tests -q` | exit 0 |
| Package syntax | `/workspace/.venv/bin/python -m compileall dispatch scr` | exit 0 |

## Scope

**In scope**:
- `dispatch/jobs.py`
- `dispatch/screens/dashboard.py`
- `dispatch/screens/new_job.py`
- `dispatch/screens/job_detail.py` only for overlap/thread-cache cleanup
- `dispatch/screens/history.py` only if the same listing helper changes its behavior
- tests under `tests/`

**Out of scope**:
- Changing the manifest directory layout unless a smaller fix cannot bound work.
- Deleting or archiving historical Job manifests.
- New external services, daemons, databases, or indexes outside the manifest tree.

## Git workflow

- Use a branch name like `cursor/bound-manifest-refresh-d0e6` if executing in this Cloud environment.
- Keep behavior and tests in one commit; avoid mixing UI redesign with performance plumbing.

## Steps

### Step 1: Add bounded listing helpers

In `dispatch/jobs.py`, add purpose-specific helpers:
- `launch_slot_jobs()` should stop after it knows the cap is reached.
- `active_jobs()` should avoid parsing ancient terminal manifests when their mtime/finished time proves they are outside the seven-day active window.
- Keep `history_jobs()` able to load old terminal manifests because history needs them.

If finished time cannot be known without parsing, use the existing manifest cache but add an early-stop path for cap checks first. Do not introduce a sidecar index in the first pass unless measurement proves it is needed.

**Verify**: unit tests seed many terminal manifests and assert `can_launch()` does not load more than necessary. Use monkeypatching around `manifest.load` if needed.

### Step 2: Debounce New Job validation summary

Change `NewJobScreen.on_input_changed()` so expensive validation summary work is debounced. Keep immediate field-level feedback for cheap checks. A small Textual timer, around 150-250 ms, is enough.

Keep final `_validate()` synchronous/authoritative on launch; Plan 002 ensures the final preflight cannot rely on stale debounce state.

**Verify**: focused test that typing still updates the summary after a pause and launch validation still blocks invalid input.

### Step 3: Prevent overlapping refreshes

Add `_refresh_in_flight` guards or use Textual workers with `exclusive=True` for:
- `DashboardScreen._refresh_jobs_async()`
- `JobDetailScreen._refresh_detail_async()`

If a tick fires while the previous refresh is still running, skip it and let the next interval catch up.

**Verify**: async test with a slow monkeypatched `jobs.active_jobs` proves only one refresh body runs at a time.

### Step 4: Pause dashboard polling when hidden

Store the dashboard interval/timer handle if Textual exposes one for this version, or gate `_refresh_jobs_async()` with `if self.app.screen is not self: return`.

Because dashboard currently owns job-completion notifications, choose one:
- Keep a slow app-level transition watcher, or
- accept that cross-screen notifications remain a separate direction item and document that dashboard notifications resume on return.

Do not leave both dashboard polling and job-detail polling active at full speed for the same selected Job.

**Verify**: pilot test navigates from Overview to New Job and asserts dashboard refresh body is not called repeatedly.

### Step 5: Move worker-thread cache mutations to the main loop

In `JobDetailScreen._refresh_detail_async()`, the `_read()` function currently mutates `self._manifest_mtime` and `self._manifest_item` from a thread. Change `_read()` to return immutable cache data and update those fields in `_apply_detail_snapshot()` on the main loop.

**Verify**: existing job-detail tests pass; add a regression test if there is one for log refresh.

## Test plan

- Unit tests for bounded `jobs.can_launch()` behavior with large seeded histories.
- Pilot or async tests for debounced New Job validation and dashboard hidden-screen pause.
- Existing cockpit tests to prove dashboard row updates and state-transition notifications still work.
- Full suite because performance changes touch shared refresh/listing behavior.

## Done criteria

- [ ] Launch cap checks no longer parse every historical manifest.
- [ ] New Job typing no longer performs a full jobs-dir scan per character.
- [ ] Dashboard and Job Detail refreshes cannot overlap under slow I/O.
- [ ] Dashboard full-speed polling does not continue behind other top-level screens.
- [ ] Thread workers do not mutate `JobDetailScreen` cache fields directly.
- [ ] Focused tests and full local gate exit 0.
- [ ] `plans/README.md` row for Plan 004 is updated.

## STOP conditions

Stop and report if:
- Bounding active scans requires changing manifest schema or directory layout.
- Textual 8.2.5 lacks a safe timer cancellation/gating API and no simple screen check works.
- Performance tests become timing-flaky rather than deterministic through monkeypatching.
- Any in-scope file changed since `a50c81d` and the excerpts no longer match.

## Maintenance notes

- Keep correctness ahead of speed: terminal Jobs must not disappear from history and Running Jobs must not be missed.
- Reviewers should check that optimization does not make completion notifications stale or duplicate.
- If this plan is insufficient for users with thousands of Jobs, the follow-up is a manifest index/active directory design, not more UI-level caching.
