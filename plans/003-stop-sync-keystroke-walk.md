# Plan 003: Stop the synchronous manifest walk per New-Job keystroke

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/screens/new_job.py dispatch/jobs.py`
> If either file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: perf
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

Every keystroke in any New Job form field fires `on_input_changed` →
`_update_validation_summary` → `_validation_issues` → `jobs.can_launch()` →
`running_jobs()` → `list_manifests()`, which glob-sorts every `*/manifest.json`
under the jobs dir and stat's each one — **on the Textual event loop**. Over a
slow NFS mount on the edge-node SSH chain, this blocks the UI per character
typed. The validation summary only needs to know whether the 2-job cap is
reached, which changes at most once every few seconds — not per keystroke.

## Current state

`dispatch/screens/new_job.py:270-272` — every input change re-validates:

```
270: def on_input_changed(self, event: Input.Changed) -> None:
271:     self._inline_validate()
272:     self._update_validation_summary()
```

`dispatch/screens/new_job.py:335-336` — `_validation_issues` calls `can_launch`:

```
335:         if not jobs.can_launch():
336:             issues.append(f"At the {jobs.RUNNING_CAP}-Job concurrency cap")
```

`dispatch/screens/new_job.py:343-344` — `_update_validation_summary` calls
`_validation_issues`:

```
343:     def _update_validation_summary(self) -> None:
344:         issues = self._validation_issues()
```

`dispatch/jobs.py:59-64` — `can_launch` → `running_jobs` → `list_manifests`
(full glob + stat loop):

```
59: def running_jobs(root: Path | None = None) -> list[manifest.JobManifest]:
60:     return [item for item in list_manifests(root) if item["state"] == "Running"]
63: def can_launch(root: Path | None = None) -> bool:
64:     return len(running_jobs(root)) < RUNNING_CAP
```

The dashboard already refreshes `jobs.active_jobs` every 2s on a worker
(`dispatch/screens/dashboard.py:135,165`), and the app owns a Kerberos TTL
reactive that screens mirror. So a cached running-count is the established
pattern.

**Repo conventions**: the app shell (`dispatch/app.py:44`) holds app-wide
reactive state (`kerberos_ttl`) that screens mirror via `self.watch(self.app,
"kerberos_ttl", ...)` (`dashboard.py:132-133`). The New Job screen already
reads `self.kerberos_ttl` from a one-shot `kerberos.ticket_ttl_seconds()` call
at mount (`new_job.py:155`). The cleanest fix matches that pattern: cache the
running-count on the New Job screen with a short TTL, refreshed off-thread,
instead of calling `jobs.can_launch()` synchronously per keystroke.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `dispatch/screens/new_job.py`

**Out of scope**:
- `dispatch/jobs.py` — `can_launch` stays as-is (the dashboard and the launch
  flow call it correctly; only the per-keystroke path is wrong).
- `dispatch/screens/dashboard.py` — unrelated.
- The `_validate()` launch-time check (`new_job.py:469-470`) — keep it calling
  `jobs.can_launch()` directly. The launch path is a single user action, not
  per-keystroke; a fresh check there is correct and prevents a stale-cache
  launch over the cap.

## Git workflow

- Branch: `advisor/003-stop-sync-keystroke-walk`
- Commit per step; message style: `perf(new_job): cache running count instead of walking manifests per keystroke`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add a cached running-count with a short TTL on `NewJobScreen`

In `NewJobScreen.__init__` (around `new_job.py:40-51`), add:

```python
self._running_count_cache: tuple[float, int] | None = None  # (when, count)
```

Add a helper that returns the cached count if fresh, else refreshes off-thread
and returns the stale value (or 0 on first call):

```python
import time

_RUNNING_CACHE_TTL = 2.0  # seconds; matches the dashboard refresh cadence

def _running_count(self) -> int:
    """Cached count of Running jobs. Refreshed off-thread when stale so
    keystroke validation never walks the manifest tree on the event loop."""
    cached = self._running_count_cache
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _RUNNING_CACHE_TTL:
        return cached[1]
    # Stale or absent: kick an off-thread refresh and return the last known
    # value (0 if never known). The next keystroke within the TTL sees the
    # fresh value.
    self._running_count_cache = (now, cached[1] if cached else 0)
    self.run_worker(self._refresh_running_count, name="running-count",
                    group="running-count", exclusive=True)
    return cached[1] if cached else 0

async def _refresh_running_count(self) -> None:
    count = len(await asyncio.to_thread(jobs.running_jobs))
    self._running_count_cache = (time.monotonic(), count)
```

`asyncio` and `time` are already imported at the top of `new_job.py`
(`import asyncio` at line 6; add `import time` if missing). `jobs` is imported
at line 20.

### Step 2: Use `_running_count()` in `_validation_issues`, keep `_validate` on `can_launch`

Change `new_job.py:335-336`:

```python
if self._running_count() >= jobs.RUNNING_CAP:
    issues.append(f"At the {jobs.RUNNING_CAP}-Job concurrency cap")
```

Leave `_validate()` at `new_job.py:469-470` unchanged — it calls
`jobs.can_launch()` synchronously on the launch path, which is correct (a
fresh check before actually creating the job).

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 3: Add a test asserting the validation summary does not call `can_launch` per keystroke

In `tests/test_new_features.py` or `tests/test_qa_fixes.py` (whichever already
has New Job validation tests — check both first), add a test that:

1. Mounts `NewJobScreen` with the `mock_env_with_config` fixture.
2. Monkeypatches `dispatch.jobs.can_launch` to raise (so any synchronous call
   is detected).
3. Monkeypatches `dispatch.jobs.running_jobs` to return a fixed list (so the
   off-thread refresh does not raise).
4. Types into an Input and asserts no exception surfaces and the summary
   renders.

Mirror the existing `TestNewJobKerberosGating` or
`test_new_job_launch_requires_confirmation` patterns in
`tests/test_phase1_safety.py` for the pilot/mount style.

**Verify**: `python -m pytest tests -q -k "new_job"` → all pass.

## Test plan

- New test: `test_validation_summary_does_not_walk_manifests_per_keystroke`
  asserting `jobs.can_launch` is not called from the input-change path.
- Structural pattern: `tests/test_phase1_safety.py::test_new_job_launch_requires_confirmation`
  for the mount + pilot style; `tests/test_qa_fixes.py:218-233` for the
  `_validation_issues` assertion style.
- Verification: `python -m pytest tests -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests -q` exits 0; the new keystroke test passes
- [ ] `grep -n "jobs.can_launch()" dispatch/screens/new_job.py` returns
      exactly one match (in `_validate`, not in `_validation_issues`)
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `NewJobScreen.__init__` does not match the excerpt at `new_job.py:40-51`
  (the init structure drifted).
- `asyncio` is not importable at the top of `new_job.py` (unlikely — it's at
  line 6 — but if the import moved, re-check).
- The existing New Job test patterns referenced above are not found in
  `tests/test_phase1_safety.py` or `tests/test_qa_fixes.py` (the test
  scaffolding moved — find it before writing the new test).

## Maintenance notes

- The cache TTL is 2s to match the dashboard refresh. If the dashboard cadence
  changes (`dispatch/screens/dashboard.py:135`), consider updating the
  constant here too.
- The launch path (`_validate`) intentionally bypasses the cache and calls
  `jobs.can_launch()` fresh — this prevents launching over the cap because of
  a stale 2s-old count. Do NOT "optimize" `_validate` to use the cache.
- If Plan 002 (reaper) lands, `jobs.running_jobs` will reap stale entries on
  each call; the off-thread refresh here benefits from that automatically.
- Reviewer: confirm the `run_worker(..., exclusive=True)` prevents stacked
  refresh workers when typing fast.
