# Plan 002: Reconcile stale "Running" manifests via PID-liveness check

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/jobs.py dispatch/runner.py dispatch/manifest.py`
> If any of those files changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

A Dispatch Job's manifest is written `state="Running"` with `pid=os.getpid()`
by the detached runner *before* the orchestrator spawns
(`dispatch/runner.py:110-115`). If the runner dies abnormally — `kill -9`,
edge-node reboot, SSH loss, OOM, or a Python crash before the `finally`/terminal
update runs — the manifest stays `Running` **forever**. There is no reader of
`run.pid` anywhere in the codebase (grep confirms zero readers).

`jobs.running_jobs` counts any `state == "Running"` manifest
(`dispatch/jobs.py:59-60`) and `can_launch` enforces `RUNNING_CAP = 2`
(`dispatch/jobs.py:63-64`). So a single orphaned Running manifest permanently
consumes one launch slot; two orphans make `can_launch()` always return `False`
and the user cannot launch anything without manually editing JSON on disk. The
dashboard also mis-reports the orphan as live in the status strip.

This is the highest-impact supervision failure mode in the product, and it has
zero regression signal today (Plan 016 adds the integration tests; this plan
adds the fix and its unit tests).

## Current state

`dispatch/jobs.py:16,25-56` — the manifest cache and `list_manifests`:

```16: _manifest_cache: dict[Path, tuple[float, manifest.JobManifest]] = {}
38: def list_manifests(root: Path | None = None) -> list[manifest.JobManifest]:
59-64:
def running_jobs(root: Path | None = None) -> list[manifest.JobManifest]:
    return [item for item in list_manifests(root) if item["state"] == "Running"]


def can_launch(root: Path | None = None) -> bool:
    return len(running_jobs(root)) < RUNNING_CAP
```

`dispatch/runner.py:108-115` — `run.pid` is written but nothing reads it:

```
108:         (job_dir / "run.pid").write_text(str(os.getpid()), encoding="utf-8")
110:         manifest_io.update(
111:             manifest_path,
112:             state="Running",
113:             started_at=manifest_io.now_utc(),
114:             pid=os.getpid(),
115:         )
```

`dispatch/manifest.py:105-109` — `update` is the sanctioned mutation helper:

```
105: def update(path: Path, **changes: Any) -> JobManifest:
106:     manifest = load(path)
107:     manifest.update(changes)
108:     write(path, manifest)
109:     return manifest
```

`dispatch/manifest.py:41-54` — `JobManifest` carries `pid: int | None` and
`started_at: str | None`, both populated before the orchestrator spawns.

**Repo conventions**: `dispatch/manifest.py` owns all manifest I/O; `dispatch/jobs.py`
owns manifest *queries* and lifecycle helpers. The TUI is read-only over the
jobs dir except through `manifest.update` (ADR-0001). Error handling in
`jobs.py` logs and skips (`jobs.py:47-49`). Match these — the reaper lives in
`jobs.py` and uses `manifest.update` to write the terminal state.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |
| Help      | `python -m dispatch --help`      | exit 0              |

## Scope

**In scope**:
- `dispatch/jobs.py` — add `reconcile_running()` and call it from
  `running_jobs()` (and hence `can_launch()`).
- `tests/test_pure_logic.py` — add unit tests for the reaper (this file already
  has the `JobsQueries` class with synthetic-Running manifest fixtures at
  `:431-436`).

**Out of scope**:
- `dispatch/runner.py` — the runner is stdlib-only and writes state correctly
  on normal exit; the bug is the *absence* of reconciliation in the TUI, not a
  runner defect. Do NOT change the runner.
- `dispatch/manifest.py` schema — no new fields. Reuse `pid`, `started_at`,
  and the existing `Failed` state. Do NOT add an `Orphaned` state (it would
  break the `JobState` Literal and every consumer).
- `dispatch/screens/dashboard.py` — the dashboard already calls
  `jobs.active_jobs` every 2s, which calls `list_manifests`, so the reaper
  fires passively. Do NOT add a separate dashboard reaper call in this plan.
- Plan 016 (runner integration tests for Table/Table+Csv/SqlTemplate) —
  separate plan; this plan's tests cover the reaper unit only.

## Git workflow

- Branch: `advisor/002-stale-running-reaper`
- Commit per step; message style: `fix(jobs): reconcile stale Running manifests via PID liveness`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add `reconcile_running()` to `dispatch/jobs.py`

Add a function that, for each `state == "Running"` manifest with a non-null
`pid`, probes liveness with `os.kill(pid, 0)`. On `ProcessLookupError`, mark
the job `Failed` with `finished_at` and a sentinel `exit_code` (use `None` —
`exit_code` is already `int | None`). On `PermissionError`, treat the process
as alive (it belongs to another user or root; do not reap). Wrap everything in
`try/except` and log — the reaper must never raise into the refresh path.

```python
import os
import logging

logger = logging.getLogger("dispatch.jobs")

# ... existing code ...

def _pid_is_alive(pid: int) -> bool:
    """True if ``pid`` currently exists. PermissionError means the process
    exists but belongs to another user — treat as alive (do not reap a
    foreign pid)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True

def reconcile_running(root: Path | None = None) -> None:
    """Mark stale ``Running`` manifests ``Failed`` when their runner pid is dead.

    Called from ``running_jobs`` so the launch cap and dashboard status can
    never be permanently blocked by a runner that died without writing a
    terminal state (kill -9, node reboot, OOM, SSH loss). Safe to call on
    every refresh: it only acts when the pid is provably gone.
    """
    for item in list_manifests(root):
        if item["state"] != "Running":
            continue
        pid = item.get("pid")
        if not pid:
            continue
        if not _pid_is_alive(int(pid)):
            path = config.jobs_dir() / item["id"] / "manifest.json"
            try:
                manifest.update(
                    path,
                    state="Failed",
                    finished_at=manifest.now_utc(),
                    exit_code=None,
                )
                logger.warning(
                    "Reaped stale Running job %s (pid %s no longer exists)",
                    item["id"], pid,
                )
            except Exception as exc:
                logger.warning("Failed to reap stale job %s: %s", item["id"], exc)
```

`manifest` is already imported in `jobs.py` (`from . import config, manifest`
at line 9). Add `import os` at the top if not present.

### Step 2: Call `reconcile_running()` from `running_jobs`

Change `running_jobs` so the cap is never blocked by a dead runner:

```python
def running_jobs(root: Path | None = None) -> list[manifest.JobManifest]:
    reconcile_running(root)
    return [item for item in list_manifests(root) if item["state"] == "Running"]
```

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 3: Add unit tests in `tests/test_pure_logic.py`

Find the existing `JobsQueries` class (around `:431-436`) that seeds synthetic
Running manifests. Add tests that:

1. Seed a Running manifest with a pid that is provably dead (use a very high
   pid like `999999` — `os.kill(999999, 0)` raises `ProcessLookupError` on
   any real OS).
2. Call `jobs.running_jobs(tmp_root)` and assert the manifest is now `Failed`
   (not `Running`).
3. Assert `jobs.can_launch(tmp_root)` returns `True` after reaping when the
   reaped job was the only Running one.
4. Seed a Running manifest with the *current* pid (`os.getpid()`) and assert
   it is NOT reaped (the reaper must not kill a live process).

```python
def test_reaper_marks_dead_running_job_failed(tmp_path):
    import os
    from dispatch import jobs, manifest as m
    root = tmp_path / "jobs"
    root.mkdir(parents=True)
    # write a Running manifest with a dead pid
    job_dir = root / "20260101T000000Z_aaaaaa"
    job_dir.mkdir()
    m.write(job_dir / "manifest.json", {
        "schema_version": 1, "id": "20260101T000000Z_aaaaaa", "tool": "dispatch",
        "user": "test", "source": {"type": "SqlFile", "sql_path_at_launch": "/x.sql"},
        "destination": {"type": "Csv", "csv_path": "/x.csv"},
        "params": {}, "orchestrator_calls": [{"script": "x", "argv": ["x"]}],
        "state": "Running", "pid": 999999, "started_at": m.now_utc(),
        "finished_at": None, "exit_code": None,
    })
    running = jobs.running_jobs(root)
    assert running == [], "dead-pid Running job should have been reaped"
    reloaded = m.load(job_dir / "manifest.json")
    assert reloaded["state"] == "Failed"
    assert reloaded["finished_at"] is not None

def test_reaper_leaves_live_running_job_alone(tmp_path):
    import os
    from dispatch import jobs, manifest as m
    root = tmp_path / "jobs"
    root.mkdir(parents=True)
    job_dir = root / "20260101T000000Z_bbbbbb"
    job_dir.mkdir()
    m.write(job_dir / "manifest.json", {
        "schema_version": 1, "id": "20260101T000000Z_bbbbbb", "tool": "dispatch",
        "user": "test", "source": {"type": "SqlFile", "sql_path_at_launch": "/x.sql"},
        "destination": {"type": "Csv", "csv_path": "/x.csv"},
        "params": {}, "orchestrator_calls": [{"script": "x", "argv": ["x"]}],
        "state": "Running", "pid": os.getpid(), "started_at": m.now_utc(),
        "finished_at": None, "exit_code": None,
    })
    running = jobs.running_jobs(root)
    assert len(running) == 1
    assert running[0]["state"] == "Running"
```

If the existing `JobsQueries` fixtures provide a helper to build a manifest,
prefer it over the inline dict above (match the file's conventions).

**Verify**: `python -m pytest tests/test_pure_logic.py -q` → all pass,
including the two new tests.

## Test plan

- New tests: `test_reaper_marks_dead_running_job_failed` and
  `test_reaper_leaves_live_running_job_alone` in `tests/test_pure_logic.py`.
- Structural pattern: the existing `JobsQueries` class in the same file
  (`:431-436` per the audit) seeds synthetic manifests under `tmp_path` —
  mirror its fixture style.
- Verification: `python -m pytest tests/test_pure_logic.py -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests/test_pure_logic.py -q` exits 0; both new
      reaper tests pass
- [ ] `python -m pytest tests -q` exits 0 (no regressions in the full suite)
- [ ] `grep -n "reconcile_running" dispatch/jobs.py` returns at least one
      match (the function) plus one call site in `running_jobs`
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `dispatch/jobs.py` does not import `manifest` at line 9 as the excerpts show
  (the import structure drifted — re-check before adding the reaper).
- The `JobsQueries` class or its synthetic-manifest helper is not present in
  `tests/test_pure_logic.py` (the test pattern moved — find it before writing
  the new tests).
- `manifest.update` rejects `exit_code=None` (the validator at
  `dispatch/manifest.py:112-144` does not type-check `exit_code`, so this
  should work; if it raises, STOP and report — the schema drifted).
- You discover `run.pid` *is* read somewhere (grep `run.pid` across `dispatch/`)
  — another change already added reconciliation; STOP rather than duplicate.

## Maintenance notes

- **PID recycling risk**: on a long-running edge node, a dead runner's pid can
  be recycled into an unrelated process. The reaper treats `PermissionError`
  as "alive" (foreign-user process) which covers the common case. A stronger
  guard would also check `started_at` against `/proc/{pid}` start time, but
  that is Linux-specific and out of scope here. If false-reaps occur in
  production, add a `started_at`-based guard in a follow-up.
- **Frequency**: `reconcile_running` runs on every `running_jobs` call — every
  dashboard 2s tick and every `can_launch` check. `os.kill(pid, 0)` is a
  cheap syscall, but if the jobs dir grows very large, Plan 012 (bound the
  scan) should ensure the reaper only probes `Running` candidates, not all
  manifests. The implementation above already `continue`s on non-Running, so
  it is O(running) not O(all) after `list_manifests`.
- Reviewer: confirm the reaper does not fire for `pid is None` (a Pending job
  that never started) — those are handled by Plan 006, not here.
- Plan 016 (runner integration tests) should add a reaper assertion: spawn a
  runner, `kill -9` it, and assert the dashboard/`can_launch` recovers.
