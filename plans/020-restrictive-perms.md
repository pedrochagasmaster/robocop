# Plan 020: Create the per-user jobs tree with restrictive permissions on the shared mount

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- dispatch/manifest.py dispatch/config.py install.sh`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

`/ads_storage/<user>/.dispatch/jobs/` holds job manifests containing SQL
text, recipient emails, table names, and CSV output paths. The tree is
created with default mkdir permissions (0o777 minus umask) on a **shared
multi-user mount**. On a typical edge node with a permissive umask (022),
this leaves the tree world-readable — other users on the node can list and
read a user's job manifests and infer what queries they run and who they
email. Restricting the per-user `.dispatch` tree to `0o700` (owner-only)
closes this without affecting any functionality.

## Current state

`dispatch/manifest.py:191` — `job_dir.mkdir(parents=True)` with default mode:

```
191:     job_dir.mkdir(parents=True)
```

`install.sh:24` — `mkdir -p "$DISPATCH_HOME/jobs"` with default mode.

`dispatch/config.py:21-26` — `dispatch_home`/`jobs_dir` derive the path but
do not create it or set perms.

`dispatch/__init__.py:18-19` — `setup_logging` creates the log path parent
with `mkdir(parents=True, exist_ok=True)` (default mode).

**Repo conventions**: Python `Path.mkdir` accepts a `mode` argument
(default 0o777); passing `mode=0o700` restricts to owner. The `install.sh`
POSIX `mkdir -p` can be followed by `chmod 700`. The per-user `.dispatch`
tree is owned by the user (created on install or first run), so 0o700 is
safe and correct.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `dispatch/manifest.py` — `job_dir.mkdir(parents=True, mode=0o700)`.
- `dispatch/__init__.py` — `setup_logging`'s `mkdir(..., mode=0o700)`.
- `install.sh` — `mkdir -p` followed by `chmod 700` for `DISPATCH_HOME` and
  `jobs`.
- `tests/test_install_onboarding.py` — add a permission assertion if the
  test can run on a POSIX FS (Windows' chmod is a no-op; gate accordingly).

**Out of scope**:
- `dispatch/config.py` — it only derives paths; no creation. The creation
  sites are `manifest.py`, `__init__.py`, and `install.sh`.
- Changing perms on `vendor/`, the shared `/ads_storage/dispatch/` tree, or
  the per-user venv — those are shared/installer-owned and out of scope.
- A one-time `chmod` migration for existing edge installs — operator action;
  note in maintenance notes.

## Git workflow

- Branch: `advisor/020-restrictive-perms`
- Commit per step; message style: `security(config): create per-user .dispatch tree with 0o700 perms`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Restrict `job_dir.mkdir` in `dispatch/manifest.py`

At `manifest.py:191`, change:

```python
    job_dir.mkdir(parents=True, mode=0o700)
```

The `jobs_dir` parent (`/ads_storage/<user>/.dispatch/jobs`) must also be
0o700; since `mkdir(parents=True, mode=0o700)` creates parents with the same
mode, this handles the parent too. If the parent already exists with broader
perms (from a prior install), `mkdir` won't change it — `install.sh` Step 3
handles that.

### Step 2: Restrict `setup_logging`'s mkdir in `dispatch/__init__.py`

At `__init__.py:19`, change:

```python
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
```

### Step 3: Restrict `install.sh`'s mkdir

At `install.sh:24`, change:

```sh
mkdir -p "$DISPATCH_HOME/jobs"
chmod 700 "$DISPATCH_HOME" "$DISPATCH_HOME/jobs"
```

Add the `chmod` after the `mkdir` so existing dirs (from a prior install with
default perms) are also tightened on re-run.

**Verify**: `python -m compileall dispatch scr` → exit 0;
`sh -n install.sh` → exit 0.

### Step 4: Add a permission test (POSIX-only)

In `tests/test_install_onboarding.py` or `tests/test_pure_logic.py`, add a
test gated to POSIX (`@pytest.mark.skipif(os.name == "nt")`) that creates a
job via `manifest.create_job` in a tmp dir and asserts the job dir and the
`.dispatch` parent have `0o700` mode:

```python
import os, stat
@pytest.mark.skipif(os.name == "nt", reason="chmod is a no-op on Windows")
def test_job_dir_has_restrictive_perms(tmp_path, mock_env):
    from dispatch import manifest
    source = {"type": "SqlFile", "sql_path_at_launch": "/x.sql"}
    dest = {"type": "Csv", "csv_path": str(tmp_path / "x.csv")}
    job_dir, _ = manifest.create_job(
        source=source, destination=dest, params={"to_email": "", "subject": "t"},
        launch_cwd=tmp_path, sql_text="SELECT 1",
    )
    mode = stat.S_IMODE(job_dir.stat().st_mode)
    assert mode == 0o700, f"job dir mode is {oct(mode)}, expected 0o700"
```

**Verify**: `python -m pytest tests -q -k "restrictive_perms"` → passes on
POSIX; skips on Windows.

## Test plan

- New test: `test_job_dir_has_restrictive_perms` (POSIX-only) asserting
  `0o700` on the created job dir.
- Verification: `python -m pytest tests -q` → all pass (skipped on Windows).

## Done criteria

- [ ] `python -m compileall dispatch` exits 0
- [ ] `sh -n install.sh` exits 0
- [ ] `grep -n "mode=0o700" dispatch/manifest.py dispatch/__init__.py` returns matches
- [ ] `grep -n "chmod 700" install.sh` returns a match
- [ ] `python -m pytest tests -q` exits 0; the restrictive-perms test passes (or skips on Windows)
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `manifest.py:191` no longer matches `job_dir.mkdir(parents=True)` (the
  creation call moved — re-check before patching).
- A test or the runtime breaks because `0o700` on the jobs dir prevents the
  detached runner (which runs as the same user) from writing — it should
  not (the user owns the tree), but if a test runs as a different user,
  STOP and report.
- `install.sh`'s `chmod 700` fails on the edge node because
  `$DISPATCH_HOME` is on a mount that doesn't honor Unix perms (e.g. a
  Windows SMB mount) — STOP and report; the chmod may need to be
  best-effort (`chmod 700 ... 2>/dev/null || true`) but report first.

## Maintenance notes

- Existing edge installs created before this change have default perms on
  their `.dispatch` tree. Re-running `install.sh` (idempotent per README)
  will tighten them via the new `chmod 700`. Document this in the release
  notes so operators know to re-run `install.sh` once.
- The per-user venv (`$DISPATCH_HOME/venv`) is NOT tightened by this plan
  (it's created by `python -m venv` which sets its own perms). A separate
  `chmod 700` on the venv is optional and out of scope.
- The shared `/ads_storage/dispatch/` tree (the install source) is
  intentionally shared across users; do NOT restrict it.
- Reviewer: confirm the POSIX test asserts the mode on both the job dir and
  (optionally) the `.dispatch` parent.
