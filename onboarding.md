# Dispatch Onboarding

Welcome to Dispatch. After the Release Operator deploys the shared tree and
activates its runtime, each user only needs lightweight onboarding.

## Onboard

Run onboarding directly from the deployed tree:

```bash
/ads_storage/dispatch/onboard.sh
```

Do not run it with `source`. Onboarding creates or repairs your private state
under `/ads_storage/$USER/.dispatch`, writes the `dispatch` launcher to
`~/.local/bin/dispatch`, and updates your shell profile. It needs no dependency
bundle or personal venv and never runs pip.

If onboarding says:

```bash
To use dispatch in this shell now:
  export PATH="$HOME/.local/bin:$PATH"
```

copy and run that one command. Otherwise, open a new SSH session.

## Launch Your First Job

Go to the directory that contains your SQL files and start Dispatch:

```bash
cd /path/to/your/sql/files
dispatch
```

If the TUI opens, setup is complete.

For scripts and automation, the same `dispatch` launcher also provides a
non-interactive Job CLI (identical via `python -m dispatch`):

```bash
cd /path/to/your/sql/files
dispatch job launch --source SqlFile --destination Csv --sql query.sql --table report --yes
dispatch job list
dispatch job wait JOB_ID --json
```

See [README.md](README.md) for the full flag list, JSON examples, and exit codes.
The TUI remains the interactive supervision cockpit; the CLI never opens it.

## Quick Checks

If `dispatch` is not found:

```bash
export PATH="$HOME/.local/bin:$PATH"
which dispatch
```

If `which dispatch` still prints nothing, rerun onboarding and keep the full
output for support:

```bash
/ads_storage/dispatch/onboard.sh
```

If Dispatch opens but reports Kerberos problems, run `kinit`, confirm the ticket
with `klist`, then launch `dispatch` again.

If onboarding reports that the shared runtime is missing or invalid, stop and
contact the Release Operator. Do not run pip. Existing jobs, configuration, and
old personal venvs can remain in place; rerunning onboarding safely replaces a
stale launcher without deleting them.
