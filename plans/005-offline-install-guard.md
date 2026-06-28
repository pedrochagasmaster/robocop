# Plan 005: Guard the offline install — fail fast when `vendor/` is empty

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 8b4241e..HEAD -- install.sh docs/edge-node-first-time-setup.md tests/test_install_onboarding.py`
> If any of those changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: deps
- **Planned at**: commit `8b4241e`, 2026-06-27

## Why this matters

The edge-node install model is **offline**: `install.sh` pip-installs from a
`vendor/` wheelhouse with `--no-index`. But `vendor/` is currently empty in
this checkout (0 `.whl` files), and `install.sh` silently falls back to PyPI
when `vendor/` has no wheels. On an air-gapped edge node, this produces a late
`pip install` network failure that looks like an installer bug rather than
"wheelhouse missing". On a dev machine with PyPI, it hides the fact that the
wheelhouse is incomplete, so a `git clone` + `install.sh` on the edge node
breaks with no upstream signal.

Two coupled fixes: (a) make `install.sh` fail fast with a clear message when
`vendor/` is empty *unless* an explicit online-install opt-in is set, and (b)
align the docs' wheel-refresh recipe with the platform-correct one already
used by `deploy_and_install.ps1` so future wheelhouse rebuilds target Linux.

## Current state

`install.sh:40-45` — the silent fallback:

```
40: if [ -n "$(find "$ROOT_DIR/vendor" -maxdepth 1 -name '*.whl' -print -quit 2>/dev/null)" ]; then
41:   "$DISPATCH_HOME/venv/bin/pip" install --no-index --find-links="$ROOT_DIR/vendor" -r "$ROOT_DIR/requirements.txt"
42: else
43:   "$DISPATCH_HOME/venv/bin/pip" install --index-url "${DISPATCH_PIP_INDEX_URL:-https://pypi.org/simple}" \
44:     -r "$ROOT_DIR/requirements.txt"
45: fi
```

`docs/edge-node-first-time-setup.md:14-17` — canonical setup expects
`ls vendor/*.whl`.

`docs/edge-node-first-time-setup.md:22-23` — doc refresh recipe uses host
platform (wrong for Linux edge nodes):

```
python -m pip download -r requirements.txt -d vendor
```

`deploy_and_install.ps1:39-40` — the correct platform-targeted recipe:

```
--platform manylinux2014_x86_64 --python-version 3.10 --abi cp310 --only-binary=:all:
```

`requirements.txt` — 10 pinned packages (textual + transitives) for
`textual==8.2.5`.

**Repo conventions**: `install.sh` is POSIX `sh` (not bash) — it prints a
harmless `Bad substitution` under `dash` but completes (per AGENTS.md). Keep
fixes POSIX-compatible. Env-var opt-ins follow the `DISPATCH_*` convention
(`DISPATCH_PYTHON_BIN`, `DISPATCH_PIP_INDEX_URL`).

## Commands you will need

| Purpose   | Command                          | Expected on success |
|-----------|----------------------------------|---------------------|
| Compile   | `python -m compileall dispatch scr` | exit 0              |
| Tests     | `python -m pytest tests -q`      | all pass            |

`install.sh` itself cannot be exercised in CI without a Linux edge node + real
`klist`/`impala-shell`, so verification is via the unit test in
`tests/test_install_onboarding.py` (which already asserts installer behavior).

## Scope

**In scope**:
- `install.sh`
- `docs/edge-node-first-time-setup.md`
- `tests/test_install_onboarding.py`

**Out of scope**:
- `deploy_and_install.ps1` — already correct; this plan only *references* it
  from the docs.
- `requirements.txt` / `pyproject.toml` pin unification — that is the
  separate deps-drift concern (Plan 014 covers config; pin unification is
  out of scope here).
- Actually rebuilding the `vendor/` wheelhouse — that is an operator action,
  not a code change. This plan makes the install *detect* the missing
  wheelhouse and tell the operator what to do.

## Git workflow

- Branch: `advisor/005-offline-install-guard`
- Commit per step; message style: `fix(install): fail fast when vendor wheelhouse is empty`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Make `install.sh` fail fast when `vendor/` is empty unless online is opted in

Replace the `if/else` at `install.sh:40-45` with:

```sh
if [ -n "$(find "$ROOT_DIR/vendor" -maxdepth 1 -name '*.whl' -print -quit 2>/dev/null)" ]; then
  "$DISPATCH_HOME/venv/bin/pip" install --no-index --find-links="$ROOT_DIR/vendor" -r "$ROOT_DIR/requirements.txt"
elif [ "${DISPATCH_ALLOW_ONLINE_PIP:-0}" = "1" ]; then
  echo "WARNING: vendor/ has no wheels; falling back to PyPI (DISPATCH_ALLOW_ONLINE_PIP=1)." >&2
  "$DISPATCH_HOME/venv/bin/pip" install --index-url "${DISPATCH_PIP_INDEX_URL:-https://pypi.org/simple}" \
    -r "$ROOT_DIR/requirements.txt"
else
  echo "vendor/ has no wheels and DISPATCH_ALLOW_ONLINE_PIP is not set." >&2
  echo "Rebuild the wheelhouse for Linux before installing on an edge node:" >&2
  echo "  pip download -r requirements.txt -d vendor \\" >&2
  echo "    --platform manylinux2014_x86_64 --python-version 3.10 --abi cp310 --only-binary=:all:" >&2
  echo "Or set DISPATCH_ALLOW_ONLINE_PIP=1 for a dev install with PyPI access." >&2
  exit 1
fi
```

**Verify**: `sh -n install.sh` → exit 0 (syntax check; the `-n` flag is
POSIX and works under `dash`/`sh` on any OS with a POSIX shell). On Windows,
run `python -c "import ast; ast.parse(open('install.sh').read())"` is not
applicable (it's shell); use `git bash -c 'sh -n install.sh'` if available,
otherwise rely on the unit test in Step 3.

### Step 2: Align the docs' wheel-refresh recipe with `deploy_and_install.ps1`

In `docs/edge-node-first-time-setup.md:22-23`, replace the host-platform
`pip download` with the platform-targeted recipe and a pointer to
`deploy_and_install.ps1`:

```markdown
To rebuild the wheelhouse for a Linux edge node from a Windows/macOS dev
machine, use platform-targeted download (this is what
`deploy_and_install.ps1` does):

    pip download -r requirements.txt -d vendor \
      --platform manylinux2014_x86_64 --python-version 3.10 --abi cp310 --only-binary=:all:

Do **not** run a bare `pip download -r requirements.txt -d vendor` on a
non-Linux host — it produces host-platform wheels that `pip install
--no-index` will reject on the edge node.
```

### Step 3: Add a unit test asserting the guard fires

In `tests/test_install_onboarding.py`, add a test that runs the relevant
branch of `install.sh` logic (or shells out to `sh install.sh` with a stub
`DATA_ROOT` and empty `vendor/`, asserting a non-zero exit and the
"DISPATCH_ALLOW_ONLINE_PIP" message on stderr). Mirror the existing installer
test style in that file (`:96-97` asserts the config.json schema).

If the existing tests shell out to `install.sh`, match that. If they unit-test
helper functions, extract the guard into a testable function. Prefer the
shell-out approach if the file already does it.

**Verify**: `python -m pytest tests/test_install_onboarding.py -q` → all
pass, including the new guard test.

## Test plan

- New test: `test_install_fails_when_vendor_empty_and_no_online_opt_in`
  asserting `install.sh` exits non-zero and prints the
  `DISPATCH_ALLOW_ONLINE_PIP` guidance when `vendor/` is empty.
- Structural pattern: mirror the existing installer tests in
  `tests/test_install_onboarding.py`.
- Verification: `python -m pytest tests/test_install_onboarding.py -q` → all
  pass.

## Done criteria

- [ ] `sh -n install.sh` exits 0 (POSIX syntax valid)
- [ ] `python -m pytest tests/test_install_onboarding.py -q` exits 0; the new
      guard test passes
- [ ] `grep -n "DISPATCH_ALLOW_ONLINE_PIP" install.sh` returns matches
- [ ] `grep -n "manylinux2014_x86_64" docs/edge-node-first-time-setup.md`
      returns a match (the doc recipe was corrected)
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:
- `install.sh:40-45` no longer matches the excerpt (the install logic was
  restructured — re-check before patching).
- `tests/test_install_onboarding.py` does not shell out to `install.sh` and
  has no extracted helper to test (the test approach must adapt — STOP and
  report rather than invent a new test harness).
- `deploy_and_install.ps1:39-40` no longer uses the
  `manylinux2014_x86_64`/`cp310` flags (the canonical recipe changed — align
  the docs to whatever it now uses).

## Maintenance notes

- The `DISPATCH_ALLOW_ONLINE_PIP=1` opt-in is for dev machines only. Edge
  nodes should always install from `vendor/`. The docs should make this
  explicit (Step 2 does).
- When the `vendor/` wheelhouse is actually rebuilt (operator action), the
  `manylinux2014_x86_64` / `cp310` flags must match the edge node's Python.
  If the edge node moves to Python 3.11 or 3.12, update both the docs recipe
  and `deploy_and_install.ps1` together.
- Reviewer: confirm the guard message names the exact command to run, so an
  operator hitting it can copy-paste the fix.
- Related: `tools/prod_tui/deploy.py:19-27` and `drift.py:26-38` omit
  `vendor/` from install-trigger/drift paths (DEPS-05). That is a separate
  harness-side improvement; file it independently if desired.
