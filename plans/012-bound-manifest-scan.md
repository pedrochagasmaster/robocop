# Plan 012: Bound the dashboard manifest scan — only read Running + recent job dirs

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/jobs.py dispatch/screens/dashboard.py dispatch/screens/history.py`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: plans/004-cache-lock.md (the cache must be thread-safe before
  restructuring its access pattern)
- **Category**: perf
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

`jobs.list_manifests` glob-sorts every `*/manifest.json` under the jobs dir
and stat's each one — on every dashboard 2s tick. `active_jobs` and
`history_jobs` then load all of them and filter by the 7-day window in Python.
History is never deleted (per `CONTEXT.md`), so as a user accumulates months
of jobs, the cost of every dashboard refresh grows unbounded — over NFS on
the edge-node SSH chain, this is the dominant supervision tax for long-tenure
users.

The mtime cache (Plan 004) skips JSON re-parse but not the glob + per-file
stat. This plan bounds the scan: the dashboard only needs *Running* jobs +
jobs finished within the 7-day window, so read only candidate dirs whose
`finished_at` (encoded in the job id's UTC timestamp prefix) falls in the
window, plus any dir whose manifest might still be Running.

## Current state

`dispatch/jobs.py:38-56` — glob all, sort, load all:

```
38: def list_manifests(root: Path | None = None) -> list[manifest.JobManifest]:
39:     base = root or config.jobs_dir()
40:     if not base.exists():
41:         return []
42:     paths = sorted(base.glob("*/manifest.json"), reverse=True)
43:     loaded: list[manifest.JobManifest] = []
44:     for path in paths:
45:         try:
46:             loaded.append(_load_manifest_cached(path))
47:         except Exception as exc:
48:             logger.warning("Skipping corrupt manifest %s: %s", path, exc)
49:             continue
...
56:     return loaded
```

`dispatch/jobs.py:67-84` — `active_jobs`/`history_jobs` load all then filter
by `finished_at` and the 7-day `ACTIVE_WINDOW`.

`dispatch/manifest.py:70-73` — the job id encodes a UTC timestamp:
`new_job_id()` returns `f"{timestamp}_{token}"` where timestamp is
`%Y%m%dT%H%M%SZ`.

`dispatch/screens/dashboard.py:135` — 2s interval; `:165`
`await asyncio.to_thread(jobs.active_jobs)`.

`dispatch/screens/history.py:81` — mount loads `history_jobs`.

**Repo conventions**: `dispatch/manifest.py` owns the id format; `dispatch/jobs.py`
owns queries. The timestamp prefix in the id is the natural partition key —
no schema change needed, just a dir-name parse.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `dispatch/jobs.py` — add an `active_jobs`/`history_jobs` fast path that
  filters dirs by id-timestamp prefix before loading manifests.

**Out of scope**:
- `dispatch/manifest.py` — no schema change. The id already encodes the
  timestamp.
- `dispatch/screens/dashboard.py` / `history.py` — the screens call
  `active_jobs`/`history_jobs`; the optimization is inside those functions.
- A persistent index file — out of scope; the dir-name parse is cheap and
  sufficient. An index file would be a larger change with its own
  consistency concerns.

## Git workflow

- Branch: `advisor/012-bound-manifest-scan`
- Commit per step; message style: `perf(jobs): filter job dirs by id timestamp before loading manifests`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add a helper to parse the job-id timestamp

In `dispatch/jobs.py`, add:

```python
from datetime import datetime, timedelta, timezone

def _job_id_timestamp(job_id: str) -> datetime | None:
    """Parse the UTC timestamp prefix from a job id like
    ``20260509T164500Z_a1b2c3``. Returns None if the prefix is not parseable
    (e.g. a hand-edited dir name), so callers fall back to loading the
    manifest."""
    prefix = job_id.split("_", 1)[0]
    try:
        return datetime.strptime(prefix, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
```

### Step 2: Filter dirs by timestamp in `active_jobs` and `history_jobs`

Add a helper that lists job dirs but skips ones provably outside a window:

```python
def _candidate_dirs(base: Path, *, since: datetime | None) -> list[Path]:
    """List job dirs under ``base``, skipping ones whose id-timestamp is
    provably older than ``since``. Dirs with an unparseable id are always
    included (fall back to loading)."""
    if not base.exists():
        return []
    candidates = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        if since is not None:
            ts = _job_id_timestamp(entry.name)
            if ts is not None and ts < since:
                continue
        manifest_path = entry / "manifest.json"
        if manifest_path.exists():
            candidates.append(manifest_path)
    # Reverse-sort by name (id timestamp prefix) so newest come first, matching
    # the existing list_manifests ordering.
    candidates.sort(key=lambda p: p.parent.name, reverse=True)
    return candidates
```

Change `active_jobs` to only read dirs whose id-timestamp is within the
7-day window (with a small margin for clock skew) OR whose state might still
be Running (we can't know without loading, but a dir newer than the window is
a candidate; an older dir could still be Running only if the runner never
wrote a terminal state — Plan 002's reaper handles that, so we can safely
skip old dirs):

```python
def active_jobs(root: Path | None = None) -> list[manifest.JobManifest]:
    base = root or config.jobs_dir()
    # Read dirs from the last 7 days plus a 1-hour skew margin. Running jobs
    # are always within this window (a job older than 7 days that is still
    # Running is stale and reaped by reconcile_running in Plan 002).
    since = datetime.now(timezone.utc) - ACTIVE_WINDOW - timedelta(hours=1)
    paths = _candidate_dirs(base, since=since)
    now = datetime.now(timezone.utc)
    result = []
    for path in paths:
        try:
            item = _load_manifest_cached(path)
        except Exception as exc:
            logger.warning("Skipping corrupt manifest %s: %s", path, exc)
            continue
        finished = parse_time(item["finished_at"])
        if item["state"] == "Running" or finished is None or now - finished <= ACTIVE_WINDOW:
            result.append(item)
    return result
```

Change `history_jobs` symmetrically to read dirs *older* than 7 days:

```python
def history_jobs(root: Path | None = None) -> list[manifest.JobManifest]:
    base = root or config.jobs_dir()
    cutoff = datetime.now(timezone.utc) - ACTIVE_WINDOW
    # History: dirs older than 7 days. Include all older dirs regardless of
    # state (a Succeeded job from 3 months ago is history).
    all_paths = _candidate_dirs(base, since=None)
    result = []
    for path in all_paths:
        try:
            item = _load_manifest_cached(path)
        except Exception as exc:
            logger.warning("Skipping corrupt manifest %s: %s", path, exc)
            continue
        finished = parse_time(item["finished_at"])
        if finished is not None and datetime.now(timezone.utc) - finished > ACTIVE_WINDOW:
            result.append(item)
    return result
```

Note: `history_jobs` still scans all dirs (history grows forever), but it
only *loads* (parses JSON for) the ones that pass the `finished_at` filter —
the mtime cache makes this cheap on repeat calls. A future partition-by-month
scheme could bound history too; out of scope here.

Leave `list_manifests` unchanged (it's still used by `running_jobs`/`can_launch`
and by tests). The dashboard's `active_jobs` and History's `history_jobs` now
use the bounded `_candidate_dirs` path.

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 3: Add tests

In `tests/test_pure_logic.py`, add tests that:

1. Seed jobs with ids spanning: 1 day ago, 6 days ago, 10 days ago, 100 days
   ago.
2. Assert `active_jobs` returns only the 1-day and 6-day jobs (plus any
   Running).
3. Assert `history_jobs` returns only the 10-day and 100-day jobs.
4. Assert a Running job with an old id is still included in `active_jobs`
   (the reaper from Plan 002 would normally reap it, but if Plan 002 is not
   landed, `active_jobs` must still surface it — verify the `state ==
   "Running"` short-circuit in the filter).

Mirror the existing `JobsQueries` fixture style in the same file.

**Verify**: `python -m pytest tests/test_pure_logic.py -q` → all pass.

## Test plan

- New tests: `test_active_jobs_skips_old_dirs`, `test_history_jobs_skips_recent_dirs`
  in `tests/test_pure_logic.py`.
- Structural pattern: existing `JobsQueries` class with synthetic manifests.
- Verification: `python -m pytest tests -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests -q` exits 0; the two new tests pass
- [ ] `grep -n "_candidate_dirs" dispatch/jobs.py` returns the helper plus
      call sites in `active_jobs` and `history_jobs`
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- The job-id format in `dispatch/manifest.py:70-73` is no longer
  `%Y%m%dT%H%M%SZ_<token>` (the timestamp parse would fail — re-check and
  adapt `_job_id_timestamp`).
- A test seeds a manifest with a hand-edited id (no timestamp prefix) and the
  new code drops it — the `_job_id_timestamp returns None` fallback must
  include such dirs; verify the fallback works and STOP if it doesn't.
- `running_jobs`/`can_launch` break because they still call `list_manifests`
  (full scan) — they are intentionally unchanged (the cap check must see all
  Running jobs, and Plan 002's reaper runs there). Do NOT route them through
  `_candidate_dirs` without also running the reaper.
- Plan 004 (cache lock) has not landed — this plan's `_candidate_dirs` still
  uses `_load_manifest_cached`, which needs the lock. STOP if 004 is not done.

## Maintenance notes

- The 1-hour skew margin in `active_jobs` is defensive against clock skew
  between the TUI's clock and the manifest's `finished_at`. If jobs run
  longer than an hour, the margin is irrelevant (the id timestamp is at
  creation time, not finish). Keep the margin small.
- `history_jobs` still scans all dirs. A future partition-by-month scheme
  (dirs grouped under `jobs/2026/05/`) would bound it further, but that's a
  larger schema change — file separately.
- The `_candidate_dirs` `iterdir()` + `is_dir()` is still O(dirs) for the
  stat, but it avoids the JSON parse for out-of-window dirs, which is the
  expensive part on NFS.
- Reviewer: confirm a job created 8 days ago that is still Running (Plan 002
  not landed) still appears in the dashboard — if not, the filter is too
  aggressive and must include all Running regardless of age.
