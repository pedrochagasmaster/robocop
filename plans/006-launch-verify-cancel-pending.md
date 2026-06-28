# Plan 006: Verify runner spawn and handle Cancel on Pending jobs

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/process.py dispatch/screens/new_job.py dispatch/screens/job_detail.py`
> If any of those changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: none (Plan 002 reaper is complementary but not required)
- **Category**: bug
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

Two coupled bugs in the launch/cancel path leave the user stuck with no
recovery:

1. `process.launch_runner` is fire-and-forget — it `await`s
   `create_subprocess_exec` only long enough to get a pid, then returns
   without confirming the runner actually started. `NewJobScreen._launch_flow`
   unconditionally shows a "✓ Launched" toast. If the runner fails to spawn
   (broken venv after an upgrade, `setsid` missing, `dispatch.runner` import
   error), the manifest stays `state="Pending"`, `pid=None`, and the user is
   told it launched.

2. `JobDetailScreen` shows the Cancel button for `Pending` jobs
   (`job_detail.py:253`) and `check_action("cancel")` returns `True` for
   `Pending` (`:72-73`), but `_cancel_flow` only acts when
   `state == "Running" and pid` (`:432-444`). For a Pending job, pressing
   Cancel silently does nothing — no confirmation, no notification, no state
   change. The user clicks Cancel and nothing happens.

Together: a failed spawn creates a stuck Pending job, and the obvious recovery
(Cancel) is a silent no-op.

## Current state

`dispatch/process.py:28-41` — fire-and-forget:

```
28: async def launch_runner(job_dir: Path) -> int:
29:     proc = await asyncio.create_subprocess_exec(
30:         "nohup", "setsid", sys.executable, "-m", "dispatch.runner",
31:         "--job-dir", str(job_dir),
32:         stdin=asyncio.subprocess.DEVNULL,
33:         stdout=asyncio.subprocess.DEVNULL,
34:         stderr=asyncio.subprocess.DEVNULL,
35:     )
36:     return proc.pid
```

`dispatch/screens/new_job.py:594-605` — unconditional success toast:

```
594:         job_dir, _job_manifest = manifest.create_job(...)
601:         await process.launch_runner(job_dir)
602:         logger.info("Launched Job %s ...", job_dir.name)
603:         self._save_form_defaults()
604:         self.notify(f"\u2713 Launched Job {job_dir.name}", severity="information")
605:         self._show_message(f"\u2713 Launched Job {job_dir.name}", "success")
```

`dispatch/screens/job_detail.py:432-444` — cancel only acts on Running:

```
432:     async def _cancel_flow(self) -> None:
433:         try:
434:             item = manifest.load(self.job_dir / "manifest.json")
435:         except Exception:
436:             return
437:         pid = item.get("pid")
438:         if item["state"] == "Running" and pid:
439:             confirmed = await self._confirm_cancel(item["id"], pid)
440:             if not confirmed:
441:                 return
442:             process.cancel_process_group(pid)
443:             self.notify(f"Cancellation requested for Job {item['id']}", severity="warning")
444:             self._set_static("#job-status-line", "[yellow]Cancellation requested\u2026[/]")
```

`dispatch/screens/job_detail.py:70-76` — `check_action` allows cancel for
Pending; `:253` shows the button for Pending.

**Repo conventions**: launch runs in a Textual worker
(`new_job.py:569-575`, `run_worker(..., exclusive=True)`). Confirmations use
`ConfirmScreen` + a future bridge (`new_job.py:607-641`,
`job_detail.py:446-466`). Manifest mutations go through `manifest.update`.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `dispatch/screens/new_job.py` — add post-launch verification in
  `_launch_flow`.
- `dispatch/screens/job_detail.py` — handle Pending in `_cancel_flow`.
- `tests/test_phase1_safety.py` and/or `tests/test_qa_fixes.py` — add tests
  for both behaviors.

**Out of scope**:
- `dispatch/process.py` — `launch_runner` stays fire-and-forget at the
  primitive level (the runner is detached by design; ADR-0001). The
  verification happens in the caller, not the primitive.
- `dispatch/runner.py` — the runner is correct; it writes `Running` + `pid`
  on start. The bug is when it *never starts*.
- Plan 002 (reaper) — handles stale *Running* jobs; this plan handles
  *Pending* jobs that never reached Running. They are complementary.

## Git workflow

- Branch: `advisor/006-launch-verify-cancel-pending`
- Commit per step; message style: `fix(launch): verify runner spawn and handle cancel on Pending`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add post-launch verification in `_launch_flow`

After `await process.launch_runner(job_dir)` at `new_job.py:601`, add a short
verification wait that checks the manifest transitioned to `Running` (or at
least has a `pid`). Cap the wait at ~1.5s and do it inside the existing
`launch-flow` worker so it does not block the message pump:

```python
        await process.launch_runner(job_dir)
        # Verify the runner actually started: it writes state=Running + pid
        # within ~0.5s. If after 1.5s the manifest is still Pending with no
        # pid, the spawn failed (broken venv, setsid missing, import error)
        # and we must not claim success.
        spawned = False
        for _ in range(6):
            await asyncio.sleep(0.25)
            try:
                check = manifest.load(job_dir / "manifest.json")
            except Exception:
                break
            if check.get("pid") or check.get("state") != "Pending":
                spawned = True
                break
        if not spawned:
            try:
                manifest.update(
                    job_dir / "manifest.json",
                    state="Failed",
                    finished_at=manifest.now_utc(),
                    exit_code=None,
                )
            except Exception:
                pass
            self.notify(
                "Launch failed: runner did not start. Check dispatch.log.",
                severity="error",
            )
            self._show_message("Launch failed: runner did not start.", "error")
            return
        logger.info("Launched Job %s source=%s dest=%s", job_dir.name, source["type"], destination["type"])
        self._save_form_defaults()
        self.notify(f"\u2713 Launched Job {job_dir.name}", severity="information")
        self._show_message(f"\u2713 Launched Job {job_dir.name}", "success")
```

`manifest` is imported in `new_job.py` at line 20. `asyncio` at line 6.

### Step 2: Handle Pending in `_cancel_flow`

In `dispatch/screens/job_detail.py:432-444`, extend `_cancel_flow` to branch
on Pending. A Pending job has no live runner to signal, so cancel just marks
the manifest `Cancelled`:

```python
    async def _cancel_flow(self) -> None:
        try:
            item = manifest.load(self.job_dir / "manifest.json")
        except Exception:
            return
        state = item.get("state")
        pid = item.get("pid")
        if state == "Running" and pid:
            confirmed = await self._confirm_cancel(item["id"], pid)
            if not confirmed:
                return
            process.cancel_process_group(pid)
            self.notify(f"Cancellation requested for Job {item['id']}", severity="warning")
            self._set_static("#job-status-line", "[yellow]Cancellation requested\u2026[/]")
        elif state == "Pending":
            confirmed = await self._confirm_cancel_pending(item["id"])
            if not confirmed:
                return
            manifest.update(
                self.job_dir / "manifest.json",
                state="Cancelled",
                finished_at=manifest.now_utc(),
                exit_code=None,
            )
            self.notify(f"Cancelled pending Job {item['id']}", severity="information")
            self._set_static("#job-status-line", "[dim]Cancelled[/]")
        else:
            self.notify("Only Running or Pending jobs can be cancelled.", severity="warning")
```

Add a `_confirm_cancel_pending` helper mirroring `_confirm_cancel` but with
Pending-appropriate copy (no pid to mention):

```python
    async def _confirm_cancel_pending(self, job_id: str) -> bool:
        loop_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        def on_result(result: bool | None) -> None:
            if not loop_future.done():
                loop_future.set_result(bool(result))
        self.app.push_screen(
            ConfirmScreen(
                "Cancel Pending Job",
                f"Cancel pending Job [cyan]{job_id}[/]?\n\n"
                "The runner has not started; this only marks the manifest Cancelled.",
                danger=True,
                confirm_label="Cancel Pending Job",
                cancel_label="Keep It",
            ),
            callback=on_result,
        )
        return await loop_future
```

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 3: Add tests

In `tests/test_phase1_safety.py` (which already has
`test_new_job_launch_requires_confirmation`), add a test that monkeypatches
`process.launch_runner` to do nothing (simulating a spawn that never writes
`Running`), presses Launch → confirm, and asserts:
- The manifest ends up `Failed` (not `Pending`).
- A failure notification is shown (not the "✓ Launched" success toast).

In `tests/test_qa_fixes.py` or `tests/test_ui_ux_closure.py`, add a test that:
- Seeds a `Pending` manifest (no pid).
- Opens `JobDetailScreen` for it.
- Presses `c` (cancel), confirms.
- Asserts the manifest transitions to `Cancelled`.

Mirror the existing confirm-screen test patterns; use `fake_launch_runner` if
the file provides it.

**Verify**: `python -m pytest tests -q` → all pass, including the two new
tests.

## Test plan

- New tests:
  1. `test_launch_marks_failed_when_runner_never_starts` in
     `tests/test_phase1_safety.py`.
  2. `test_cancel_pending_job_marks_cancelled` in
     `tests/test_qa_fixes.py` or `tests/test_ui_ux_closure.py`.
- Structural pattern: `tests/test_phase1_safety.py::test_new_job_launch_requires_confirmation`
  for the launch flow; `tests/test_ui_ux_closure.py` for the JobDetail cancel
  pilot style.
- Verification: `python -m pytest tests -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests -q` exits 0; both new tests pass
- [ ] `grep -n "elif state == \"Pending\"" dispatch/screens/job_detail.py`
      returns a match (the Pending branch was added)
- [ ] `grep -n "runner did not start" dispatch/screens/new_job.py` returns a
      match (the verification was added)
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `_launch_flow` at `new_job.py:577-605` does not match the excerpt (the flow
  was restructured — re-check before inserting the verification loop).
- `_cancel_flow` at `job_detail.py:432-444` was already extended to handle
  Pending (a partial fix exists — coordinate rather than duplicate).
- `manifest.update` rejects `state="Cancelled"` for a Pending job (the
  validator at `manifest.py:141` allows `Cancelled`, so this should work; if
  it raises, STOP).
- The 1.5s verification wait proves flaky in the test environment (the mock
  runner starts fast; if a real test times out, increase the loop count and
  report why).

## Maintenance notes

- The 1.5s verification wait is a tradeoff: long enough to catch a failed
  spawn, short enough not to feel like a hang. If the runner's startup
  becomes slower (e.g. a cold venv import), bump the loop count but keep it
  under ~3s.
- The Pending-cancel path marks the manifest `Cancelled` but does NOT delete
  the job dir — the user may want to inspect the (empty) run.log. A future
  "delete job" action is out of scope.
- Reviewer: confirm the failure toast does NOT fire on a successful launch
  (the `spawned` flag must be True for the happy path).
- Plan 002 (reaper) handles stale *Running* jobs; this plan handles
  *Pending* jobs. Together they close the "stuck job with no recovery" gap.
