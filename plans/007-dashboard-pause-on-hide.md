# Plan 007: Pause dashboard refresh when the screen is not visible

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/screens/dashboard.py`
> If `dispatch/screens/dashboard.py` changed since this plan was written,
> compare the "Current state" excerpts against the live code before
> proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: perf
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

`DashboardScreen.on_mount` starts `set_interval(2.0, self._refresh_jobs_async)`
(`dashboard.py:135`) and there is no `on_hide`/`on_unmount` anywhere in
`dispatch/`. The app pushes Job Detail / New Job / History / Browser on top
of the dashboard (`app.py:199-227`) while the dashboard stays mounted, so the
2s manifest scan + log-tail reads keep running *under* whatever screen the
user is actually looking at. Over SSH/VPN this is duplicate supervision I/O
with no user-visible benefit, and it compounds with the Job Detail screen's
own 1s refresh when the user is viewing a job.

## Current state

`dispatch/screens/dashboard.py:120-136` — `on_mount` starts the interval; no
`on_hide`/`on_show`:

```
120:     async def on_mount(self) -> None:
...
134:         await self._refresh_jobs_async()
135:         self.set_interval(2.0, self._refresh_jobs_async)
136:         table.focus()
```

A repo-wide grep for `on_hide`/`on_unmount` in `dispatch/` returns zero
matches (confirmed during audit). The dashboard interval therefore never
pauses.

`dispatch/screens/job_detail.py:129-137` — Job Detail has its own 1s
interval, so when the user is in Job Detail the dashboard's 2s tick is pure
waste.

**Repo conventions**: Textual `Screen` exposes `on_show`/`on_hide` lifecycle
hooks (the skill's lifecycle rules name them for "screen visibility refresh or
pause/resume behavior"). The dashboard already uses `set_interval` (a Textual
timer) which can be stopped/resumed via `self.set_interval`'s return value or
by gating the refresh on a visibility flag.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `dispatch/screens/dashboard.py`

**Out of scope**:
- `dispatch/screens/job_detail.py` — its 1s interval is correct while Job
  Detail is the active screen. It should *also* pause on hide eventually, but
  that is a separate change; this plan only addresses the dashboard, which is
  the always-mounted background poller.
- `dispatch/app.py` — the screen-stack model is correct; do not change it.

## Git workflow

- Branch: `advisor/007-dashboard-pause-on-hide`
- Commit per step; message style: `perf(dashboard): pause refresh when screen is hidden`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Track visibility and skip refresh when hidden

In `DashboardScreen.__init__` (around `dashboard.py:64-88`), add:

```python
self._visible = True
```

In `on_mount`, capture the interval timer handle so it can be stopped/resumed.
Textual's `set_interval` returns a `Timer` object; store it:

```python
        await self._refresh_jobs_async()
        self._refresh_timer = self.set_interval(2.0, self._refresh_jobs_async)
        table.focus()
```

Initialize `self._refresh_timer = None` in `__init__` if the linter prefers it
defined before `on_mount`.

Add `on_show`/`on_hide` to pause/resume:

```python
    def on_show(self) -> None:
        self._visible = True
        if self._refresh_timer is not None and not self._refresh_timer.running:
            self._refresh_timer.resume()

    def on_hide(self) -> None:
        self._visible = False
        if self._refresh_timer is not None and self._refresh_timer.running:
            self._refresh_timer.stop()
```

### Step 2: Guard `_refresh_jobs_async` against running while hidden

At the top of `_refresh_jobs_async` (`dashboard.py:162`), add an early return
so a tick that was already in-flight when the screen hid does not mutate UI
from a hidden state:

```python
    async def _refresh_jobs_async(self) -> None:
        if not self._visible:
            return
        try:
            ...
```

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 3: Add a test asserting the dashboard pauses when hidden

In `tests/test_ui_ux_closure.py` or `tests/test_cockpit.py` (whichever
already pilots the dashboard — check both), add a test that:

1. Mounts `DashboardScreen`.
2. Pushes another screen on top (e.g. `HistoryScreen`).
3. Waits > 2s (or monkeypatches `jobs.active_jobs` to a counting stub).
4. Asserts `jobs.active_jobs` was NOT called while the dashboard was hidden.

Use a counting stub (`call_count = 0; def stub(): call_count += 1; return []`)
monkeypatched onto `dispatch.jobs.active_jobs` and `time.sleep`/`asyncio.sleep`
to fast-forward, OR use Textual's `run_test` pilot with a mock clock if the
file already does. Match the file's existing time-handling convention.

**Verify**: `python -m pytest tests -q -k "dashboard"` → all pass.

## Test plan

- New test: `test_dashboard_refresh_pauses_when_hidden` asserting
  `jobs.active_jobs` is not called while another screen is on top.
- Structural pattern: existing dashboard pilot tests in
  `tests/test_cockpit.py` or `tests/test_ui_ux_closure.py`.
- Verification: `python -m pytest tests -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests -q` exits 0; the new pause test passes
- [ ] `grep -n "def on_hide" dispatch/screens/dashboard.py` returns a match
- [ ] `grep -n "if not self._visible" dispatch/screens/dashboard.py` returns
      a match (the guard was added)
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `DashboardScreen.on_mount` at `:120-136` does not match the excerpt (the
  mount structure drifted — re-check before inserting the timer handle).
- Textual 8.2.5's `set_interval` does not return a `Timer` with
  `.running`/`.stop()`/`.resume()` (verify by checking the installed
  `textual` package's `Timer` API; if the methods differ, adapt — Textual
  `Timer` has `stop()` and `resume()` in 8.x, but confirm).
- The existing dashboard tests do not use a clock/sleep mechanism that allows
  fast-forwarding (the test approach must adapt — STOP and report rather than
  introduce a real 2s+ wait that slows the suite).

## Maintenance notes

- The `_visible` guard in `_refresh_jobs_async` is belt-and-suspenders: the
  timer is stopped on hide, but a tick that was already dispatched could
  still land. The guard makes the skip explicit.
- Job Detail (`job_detail.py:137`) has the same always-ticking pattern and
  should get the same treatment in a follow-up. File it separately.
- Reviewer: confirm the dashboard resumes refreshing when the user pops back
  to it (the `on_show` resume path).
- If a future app-level "one refresh worker shared by visible screens" design
  lands (per the perf audit's PERF-03 fix sketch), this per-screen gating can
  be removed in favor of the app-level gate.
