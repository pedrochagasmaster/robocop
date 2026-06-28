# Plan 011: Cap the Job Detail log-tail delta read per 1s tick

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/screens/job_detail.py`
> If `dispatch/screens/job_detail.py` changed since this plan was written,
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

`JobDetailScreen._refresh_detail_async` reads the log **from the last offset
to EOF** each 1s tick with no byte cap (`job_detail.py:169-172`). A chatty
orchestrator that appends megabytes between ticks causes a large blocking
`asyncio.to_thread` read, a memory spike in the `_tail_lines` deque, and a
RichLog write burst. The dashboard's detail tail is capped at 4 KB
(`dashboard.py:214-221`) and `errors.classify` at 64 KB (`errors.py:25-40`);
the Job Detail live tail is the only unbounded reader.

## Current state

`dispatch/screens/job_detail.py:157-175` — the unbounded read:

```
157:             new_lines: list[str] = []
...
162:             offset = self._tail_offset
166:             if reset:
167:                 offset = 0
169:             if size > offset:
170:                 with log_path.open("r", encoding="utf-8", errors="replace") as handle:
171:                     handle.seek(offset)
172:                     new_lines = [line.rstrip() for line in handle]
173:                     new_offset = handle.tell()
174:             else:
175:                 new_offset = offset
```

`dispatch/screens/job_detail.py:137` — fires every 1s.

`dispatch/screens/job_detail.py:31` — `LOG_VIEW_LINES = 200` bounds the
in-memory deque and the RichLog widget's `max_lines`, but the *read* is
unbounded before it's truncated to 200.

`dispatch/screens/dashboard.py:215-216` — the capped pattern to mirror:

```
215:                 if stat.st_size > DETAIL_TAIL_BYTES:
216:                     handle.seek(stat.st_size - DETAIL_TAIL_BYTES)
```

**Repo conventions**: bounded reads use a byte cap + `seek`; the dashboard
and `errors.py` already do this. Match them.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `dispatch/screens/job_detail.py`

**Out of scope**:
- `dispatch/screens/dashboard.py` — already capped.
- `dispatch/errors.py` — already capped.
- The 1s refresh cadence — leave as-is; the cap bounds the burst, the cadence
  is fine.

## Git workflow

- Branch: `advisor/011-log-delta-cap`
- Commit per step; message style: `perf(job_detail): cap log-tail delta read per tick`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add a per-tick byte cap and carry the offset forward

Near the top of `job_detail.py`, add a constant:

```python
# Maximum bytes read per 1s refresh tick. A chatty orchestrator can append
# more than this between ticks; the remainder is picked up on the next tick
# by carrying the offset forward. Bounds memory and RichLog write bursts.
LOG_READ_CHUNK_BYTES = 65536
```

Change the read block at `:169-173` to cap the delta and advance the offset
partially:

```python
            if size > offset:
                with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    chunk = handle.read(LOG_READ_CHUNK_BYTES)
                    new_offset = handle.tell()
                new_lines = [line.rstrip() for line in chunk.splitlines()]
            else:
                new_offset = offset
```

`splitlines()` handles a trailing partial line correctly (it does not produce
a phantom empty final line the way `for line in handle` would on a mid-line
read boundary). The offset advances to `handle.tell()` after the chunk read,
so the next tick reads the remainder — no data loss, just bounded bursts.

**Verify**: `python -m compileall dispatch` → exit 0.

### Step 2: Add a test asserting the read is capped

In `tests/test_ui_ux_closure.py` or `tests/test_qa_fixes.py` (whichever
already exercises `JobDetailScreen` — check both), add a test that:

1. Seeds a job dir with a `run.log` larger than `LOG_READ_CHUNK_BYTES`.
2. Mounts `JobDetailScreen`, triggers one refresh.
3. Asserts the number of lines appended in one tick is bounded by
   `LOG_READ_CHUNK_BYTES` worth of lines (not the whole file).
4. Triggers a second refresh and asserts the remainder is read (offset
   carried forward).

Mirror the existing JobDetail log-tail test patterns; use the `mock_env`
fixture.

**Verify**: `python -m pytest tests -q -k "job_detail"` → all pass.

## Test plan

- New test: `test_log_tail_read_is_capped_per_tick` in the file that already
  hosts JobDetail tests.
- Structural pattern: existing `JobDetailScreen` log-tail tests in
  `tests/test_ui_ux_closure.py`.
- Verification: `python -m pytest tests -q` → all pass.

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `python -m pytest tests -q` exits 0; the new capped-read test passes
- [ ] `grep -n "LOG_READ_CHUNK_BYTES" dispatch/screens/job_detail.py`
      returns the constant plus a use site
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `job_detail.py:169-173` no longer matches the excerpt (the read block was
  restructured — re-check before patching).
- `handle.read(LOG_READ_CHUNK_BYTES)` + `splitlines()` produces a different
  line count than the existing tests expect (the deque truncation at
  `LOG_VIEW_LINES = 200` should hide any difference, but if a test asserts
  exact line counts, it may need updating — STOP and report rather than
  silently changing assertions).
- The existing JobDetail tests feed a log file line-by-line and break on the
  `read()` vs `for line in handle` switch — if so, adapt the read to preserve
  the `for line in handle` style but break after `LOG_READ_CHUNK_BYTES`
  bytes; report which approach was taken.

## Maintenance notes

- The 64 KB cap matches `errors.py`'s `_TAIL_READ_BYTES`. If one changes,
  consider updating the other for consistency (they serve different purposes
  — error classification reads the tail once, this reads incrementally — so
  they need not be identical, but a shared constant in a future refactor is
  reasonable).
- The offset is carried forward, so a very chatty log eventually catches up
  across ticks. The `_tail_lines` deque (`maxlen=200`) and RichLog
  (`max_lines=200`) still bound the visible window; the cap only bounds the
  per-tick read burst.
- Reviewer: confirm a job that writes exactly at the chunk boundary does not
  lose or duplicate a line (the `splitlines()` approach handles this, but
  verify in the test).
