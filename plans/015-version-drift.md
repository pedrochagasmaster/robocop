# Plan 015: Fix version-source drift (`pyproject.toml` 1.0.0 vs `VERSION`/`version.py` 1.1.0)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- pyproject.toml VERSION dispatch/version.py`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

There are two version sources and they disagree:
- `VERSION` (root) and `dispatch/version.py:3` say `1.1.0`.
- `pyproject.toml:7` says `1.0.0`.

`install.sh:79` copies `VERSION` to `installed_version`, and
`DispatchApp._build_version_warning` (`app.py:170-177`) compares
`installed_version` against `__version__` (from `version.py`) at startup,
showing a "Version mismatch: run install.sh" warning if they differ. The
pyproject mismatch doesn't trigger that warning directly, but it means
`pip install -e .` reports `1.0.0` while the running app reports `1.1.0` —
confusing for any tooling that reads the package metadata, and a trap for
the next person who cuts a release by bumping only one source.

## Current state

`VERSION`: `1.1.0`
`dispatch/version.py:3`: `__version__ = "1.1.0"`
`pyproject.toml:7`: `version = "1.0.0"`

`install.sh:79`: `cp "$ROOT_DIR/VERSION" "$DISPATCH_HOME/installed_version"`
`dispatch/app.py:170-177`: compares `installed_version` to `__version__`.

**Repo conventions**: `VERSION` is the deploy artifact copied to edge nodes;
`dispatch/version.py` is the runtime source (must match `VERSION`);
`pyproject.toml` is the package metadata (must match both). The single source
of truth should be `VERSION` (it's what travels to the edge node), with
`version.py` and `pyproject.toml` mirroring it. There is no script that
syncs them today.

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

## Scope

**In scope**:
- `pyproject.toml` — bump `version` to `1.1.0` to match `VERSION`/`version.py`.

**Out of scope**:
- `VERSION` and `dispatch/version.py` — already agree at `1.1.0`; do not
  change them.
- Adding a sync script or single-source mechanism — that's a larger DX
  improvement; this plan only fixes the current drift. File separately if
  desired.
- Cutting a `1.1.0` release — this plan aligns the metadata; the actual
  release/tag is an operator action.

## Git workflow

- Branch: `advisor/015-version-drift`
- Commit per step; message style: `chore(dx): align pyproject version with VERSION (1.1.0)`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Bump `pyproject.toml` version to `1.1.0`

In `pyproject.toml:7`, change:

```toml
version = "1.0.0"
```

to:

```toml
version = "1.1.0"
```

**Verify**: `grep -n '^version' pyproject.toml` returns `1.1.0`;
`python -m compileall dispatch scr` → exit 0.

### Step 2: Add a regression test that all three sources agree

In `tests/test_install_onboarding.py` (or a new `tests/test_version.py` if
that's cleaner), add a test that reads all three sources and asserts equality:

```python
def test_version_sources_agree():
    """VERSION, dispatch.version.__version__, and pyproject.toml version
    must all match so the startup version-mismatch warning and pip metadata
    stay consistent."""
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from dispatch.version import __version__
    root = Path(__file__).resolve().parents[1]
    version_file = (root / "VERSION").read_text(encoding="utf-8").strip()
    assert version_file == __version__
    # Parse pyproject.toml without a tomllib dep on 3.10
    import re
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert m is not None, "pyproject.toml has no version field"
    assert m.group(1) == __version__
```

If the repo runs on Python 3.11+ only, use `tomllib` instead of the regex;
but the floor is 3.10, so the regex is safer.

**Verify**: `python -m pytest tests -q -k "version_sources_agree"` → passes.

## Test plan

- New test: `test_version_sources_agree` asserting `VERSION`,
  `dispatch/version.py:__version__`, and `pyproject.toml` version match.
- Verification: `python -m pytest tests -q` → all pass.

## Done criteria

- [ ] `grep -n '^version = "1.1.0"' pyproject.toml` returns a match
- [ ] `python -m pytest tests -q` exits 0; the new agreement test passes
- [ ] No files outside `pyproject.toml` and the new/updated test are modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `VERSION` or `dispatch/version.py` no longer say `1.1.0` (someone bumped
  them independently — align `pyproject.toml` to whatever they now say, and
  report the value you chose).
- A release process already syncs the three sources automatically (grep for
  a version-sync script) — if so, the drift is a process bug, not a source
  bug; STOP and report rather than hand-editing.

## Maintenance notes

- This plan fixes the drift but does not prevent recurrence. A future DX
  improvement: have `dispatch/version.py` read `VERSION` at import time
  (`Path(__file__).parent.parent / "VERSION"`) so there are only two sources
  (pyproject + VERSION), or add a pre-commit/CI check (the test in Step 2 is
  a lightweight guard). File separately.
- When cutting the next release, bump all three together (or two, if the
  `version.py`-reads-`VERSION` refactor lands).
- Reviewer: confirm `python -m dispatch --help` still works after the bump
  (the version is cosmetic, but verify nothing parses it numerically).
