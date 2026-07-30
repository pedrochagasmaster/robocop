# Contributing to Dispatch

Development ends with a reviewed GitHub pull request. Deployment is a separate
Release Operator responsibility.

## Contributor

Local developers and Cursor agents follow the same path:

```bash
git switch main
git pull --ff-only origin main
git switch -c <short-branch-name>
uv sync --extra dev --extra release
uv run pytest -n 4 --dist loadfile
```

`uv sync` creates or updates `.venv` from the committed `uv.lock`. The
`release` extra makes the pinned `edge_deploy` CLI available locally. Use
`uv lock --check` before committing dependency metadata changes.

On Windows, the authoritative release validation is the committed local check:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\dev\local_check.ps1
```

It runs the test suite serially with repository-local temporary directories.
Parallel pytest remains a useful developer stress check, but it is not a
substitute for the committed Windows gate.

For local TUI work, source `mocks/dev-env.sh`. For `scr/` changes, follow
[docs/adr/0005-scr-modification-policy.md](docs/adr/0005-scr-modification-policy.md).

Commit the focused change, push the branch to GitHub, and open a pull request
against `main`. Contributors without write access may use a fork.

Include the test result and release risk. Do not commit reports, screens, logs,
deploy bundles, credentials, passcodes, Kerberos passwords, or ad hoc server
files.

## Maintainer

Merge only after CI passes on Ubuntu Python 3.10 and 3.12, the Windows Python
3.12 local-check job passes, and one human Maintainer approves. Use squash
merge and delete the merged branch. Do not push directly to `main`.

Release work starts only after merge and is documented in
[docs/release-workflow.md](docs/release-workflow.md).
