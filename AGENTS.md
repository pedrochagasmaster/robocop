# Dispatch Agent Guide

Dispatch is a server-side Textual TUI for launching and supervising Impala
jobs. Product code lives in `dispatch/`; production orchestrators live in
`scr/`; local infrastructure substitutes live in `mocks/`.

Follow [CONTRIBUTING.md](CONTRIBUTING.md). Work from current GitHub `main`, use a
short-lived branch, run `python -m pytest`, and finish with a GitHub pull
request.

On Windows, `tools/dev/local_check.ps1` is the authoritative release-validation
gate. Invoke it with Windows PowerShell 5.1; parallel pytest is a developer
stress check and does not replace the committed local check.

Agents may create branches, commit, push a branch, and open a pull request when
requested. Agents must not merge, change branch protection, create release
tags, push Bitbucket, or deploy without explicit Release Operator instruction.

Do not reintroduce the legacy Windows GUI. Keep `scr/` standard-library-only
and follow [ADR-0005](docs/adr/0005-scr-modification-policy.md). Do not commit
generated artifacts or secrets.

For Textual work, use `.agents/skills/dispatch-textual-tui/SKILL.md`. Release
Operators use [docs/release-workflow.md](docs/release-workflow.md).

## Cursor Cloud specific instructions

Dependencies live in a project virtualenv at `.venv` (gitignored). The startup
update script recreates it and runs the editable install; use `.venv/bin/python`
(or activate `.venv`) for all commands, e.g. `.venv/bin/python -m pytest`,
`.venv/bin/ruff`, `.venv/bin/mypy`. Standard lint/test/build commands are the
ones in [CONTRIBUTING.md](CONTRIBUTING.md) and `.github/workflows/ci.yml`.

Running the TUI needs the mock layer plus SQL files in the launch directory:
`source mocks/dev-env.sh` (starts a mock SMTP server and puts mock
`kinit`/`klist`/`impala-shell` on `PATH`), then run `python -m dispatch` from a
directory that contains at least one `.sql` file — the New Job form is empty
without one. `mocks/dev-env.sh` requires bash (it uses `BASH_SOURCE`). Data root
defaults to `/tmp/ads_storage/$USER`; job manifests and captured emails live
under there and in `mocks/sent_emails/`.

The notebook API (`dispatch/notebook.py`) needs no extra setup for its tests,
but exercising `demos/notebook_job_api.ipynb` does: `.venv/bin/python -m pip
install jupyterlab` (not a project dependency), then run it headlessly with
`jupyter nbconvert --to notebook --execute` from a directory inside a shell
that has sourced `mocks/dev-env.sh`. Set `DISPATCH_MOCK_DELAY` to a few seconds
to make live monitoring visible; the mocks are otherwise instant.

Known non-environment failures: the two
`tests/test_install_onboarding.py::test_install_creates_runtime_artifacts_with_mocked_edge_tools`
cases fail on a clean checkout because they assert the generated launcher
contains `robocop`, which `install.sh` does not emit. This is a repo-level
test/script mismatch, not a setup problem.
