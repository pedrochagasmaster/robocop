# Plan 004: Lock the module-level manifest cache against concurrent `to_thread` callers

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/jobs.py`
> If `dispatch/jobs.py` changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

`jobs._manifest_cache` is a module-level dict mutated by `list_manifests`
without a lock. Two callers run concurrently off the event loop via
`asyncio.to_thread`: the dashboard 2s tick (`dashboard.py:165`) and the
History screen mount (`history.py:81`). Pushed Textual screens stay mounted,
so both fire at once. The cleanup block at `jobs.py:52-55` iterates then
deletes from the same dict — under a second concurrent `to_thread` call this
raises `RuntimeError: dictionary changed size during iteration` or a `KeyError`
on a double-delete. The exception propagates into `dashboard.py:195`'s
`except Exception` ("Job refresh failed" toast) or breaks History mount
entirely.

## Current state

`dispatch/jobs.py:16` — module-level cache, no lock:

```
16: _manifest_cache: dict[Path, tuple[float, manifest.JobManifest]] = {}
```

`dispatch/jobs.py:25-35` — `_load_manifest_cached` reads/writes the cache:

```
25: def _load_manifest_cached(path: Path) -> manifest.JobManifest:
26:     try:
27:         mtime = path.stat().st_mtime
28:     except OSError as exc:
29:         raise ValueError(str(exc)) from exc
30:     cached = _manifest_cache.get(path)
31:     if cached is not None and cached[0] == mtime:
32:         return cached[1]
33:     loaded = manifest.load(path)
34:     _manifest_cache[path] = (mtime, loaded)
35:     return loaded
```

`dispatch/jobs.py:50-55` — iterate-then-delete cleanup:

```
50:     if len(_manifest_cache) > len(paths):
51:         live = set(paths)
52:         for stale in [cached for cached in _manifest_cache if cached not in live]:
53:             del _manifest_cache[stale]
```

Callers:
- `dispatch/screens/dashboard.py:135,165` — `set_interval(2.0, ...)` →
  `await asyncio.to_thread(jobs.active_jobs)`.
- `dispatch/screens/history.py:81` — `await asyncio.to_thread(jobs.history_jobs)`
  on mount.

Both run `list_manifests` → `_load_manifest_cached` + the cleanup block, on
the default `ThreadPoolExecutor`, concurrently.

**Repo conventions**: `dispatch/` uses `asyncio.to_thread` for off-loop I/O
and `run_worker(..., exclusive=True)` for Textual worker cancellation. A
plain `threading.Lock` is the standard CPython fix for shared module state
mutated by `to_thread` callers; it does not interact with the asyncio loop
(the lock is held only briefly inside the thread).

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `dispatch/jobs.py`

**Out of scope**:
- `dispatch/screens/dashboard.py`, `dispatch/screens/history.py` — the screens
  are correct to use `asyncio.to_thread`; the bug is the unsynchronized cache,
  not the call sites.
- Plan 012 (bound the manifest scan) — separate; this plan only makes the
  existing cache thread-safe.

## Git workflow

- Branch: `advisor/004-cache-lock`
- Commit per step; message style: `fix(jobs): lock manifest cache against concurrent to_thread callers`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add a module-level lock and guard the cache sections

At the top of `dispatch/jobs.py`, add `import threading` and a lock:

```python
import threading

_cache_lock = threading.Lock()
```

Change `_load_manifest_cached` to hold the lock around the get/set:

```python
def _load_manifest_cached(path: Path) -> manifest.JobManifest:
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    with _cache_lock:
        cached = _manifest_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        loaded = manifest.load(path)
        _manifest_cache[path] = (mtime, loaded)
        return loaded
```

Note: `path.stat()` stays *outside* the lock (it's the slow syscall; holding
the lock across it would serialize all reads). Only the dict get/set is
guarded. There's a benign TOCTOU window (two threads could load the same path
twice) but that's idempotent and cheap compared to the deadlock risk of
locking across `stat`.

Change the cleanup block in `list_manifests` to rebuild atomically instead of
iterate-then-delete:

```python
    if len(_manifest_cache) > len(paths):
        live = set(paths)
        with _cache_lock:
            # Rebuild a fresh dict instead of mutate-while-iterating; safe
            # under concurrent callers and avoids "dictionary changed size
            # during iteration".
            _manifest_cache = {
                p: v for p, v in _manifest_cache.items() if p in live
            }
```

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 2: Add a threading test

In `tests/test_pure_logic.py`, add a test that hammers `list_manifests` from
multiple threads against a populated tmp_path and asserts no `RuntimeError` or
`KeyError`:

```python
def test_manifest_cache_is_thread_safe(tmp_path):
    import threading
    from dispatch import jobs, manifest as m
    root = tmp_path / "jobs"
    root.mkdir(parents=True)
    for i in range(20):
        jd = root / f"2026010{i}T000000Z_{i:06d}"
        jd.mkdir()
        m.write(jd / "manifest.json", {
            "schema_version": 1, "id": jd.name, "tool": "dispatch",
            "user": "t", "source": {"type": "SqlFile", "sql_path_at_launch": "/x"},
            "destination": {"type": "Csv", "csv_path": "/x.csv"},
            "params": {}, "orchestrator_calls": [{"script": "x", "argv": ["x"]}],
            "state": "Succeeded", "pid": None, "started_at": None,
            "finished_at": m.now_utc(), "exit_code": 0,
        })
    errors = []
    def hammer():
        try:
            for _ in range(50):
                jobs.list_manifests(root)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent list_manifests raised: {errors}"
```

**Verify**: `python -m pytest tests/test_pure_logic.py -q` → all pass,
including the new thread-safety test.

## Test plan

- New test: `test_manifest_cache_is_thread_safe` in
  `tests/test_pure_logic.py`.
- Structural pattern: mirror the existing manifest-seeding helpers in the
  same file's `JobsQueries` class.
- Verification: `python -m pytest tests/test_pure_logic.py -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests/test_pure_logic.py -q` exits 0; the new
      thread-safety test passes
- [ ] `python -m pytest tests -q` exits 0 (no regressions)
- [ ] `grep -n "_cache_lock" dispatch/jobs.py` returns the declaration plus
      at least two `with _cache_lock:` blocks
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `dispatch/jobs.py` no longer has a module-level `_manifest_cache` at line 16
  (the cache was removed or moved — re-check the structure before adding a
  lock to nothing).
- The cleanup block at `:50-55` is no longer present (Plan 012 may have
  removed it — coordinate before proceeding).
- `threading` is already imported for an unrelated reason and the lock name
  `_cache_lock` collides (rename to `_manifest_cache_lock`).

## Maintenance notes

- The lock is held only around dict get/set, not across `stat()` or
  `manifest.load()`. This means two threads can load the same manifest
  simultaneously once (benign — the result is identical and the last write
  wins). Do NOT "fix" this by holding the lock across `load` — that would
  serialize all reads and defeat the point of `to_thread`.
- If Plan 012 (bound the scan) restructures the cache to be per-root, the lock
  must move with it; coordinate the merge.
- Reviewer: confirm `stat()` is outside the lock and the dict rebuild in the
  cleanup block is atomic.
