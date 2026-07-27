# Dispatch TUI module map

Use this map to find the owner of a behavior before editing. Read the production
module and its closest tests together.

## Shell and shared chrome

| Concern | Production owner | Primary tests |
|---|---|---|
| Startup, launch CWD, global bindings, routing, command palette, version warning, minimum-size warning | `dispatch/app.py` | `tests/test_cockpit.py`, `tests/test_ui_ux_audit_implementation.py` |
| Shared stylesheet and `.action-bar` layout | `dispatch/app.tcss` | `tests/test_ui_snapshots.py`, `tests/test_ui_ux_closure.py` |
| Sidebar, Kerberos chip, collapse below width 100 | `dispatch/screens/sidebar.py` | `tests/test_qa_fixes.py`, `tests/test_ui_ux_closure.py` |
| Global help modal | `dispatch/screens/help.py` | `tests/test_ui_ux_closure.py`, `tools/dev/ui_captures.py` |

Shared widgets currently live beside screens, especially in
`dispatch/screens/sidebar.py`. There is no `dispatch/widgets/` package.

## Screens

| User surface | Production owner | Supporting modules | Primary tests |
|---|---|---|---|
| Overview supervision cockpit | `dispatch/screens/dashboard.py` | `dispatch/jobs.py`, `dispatch/errors.py`, `dispatch/formatting.py` | `tests/test_cockpit.py`, `tests/test_qa_fixes.py` |
| New Job and launch validation | `dispatch/screens/new_job.py` | `dispatch/job_ops.py`, `dispatch/manifest.py`, `dispatch/jobs.py`, `dispatch/sql.py`, `dispatch/kerberos.py`, `dispatch/runner.py` | `tests/test_cockpit.py`, `tests/test_production_polish.py`, `tests/test_prefill_seam.py`, `tests/test_qa_fixes.py`, `tests/test_job_cli.py` |
| Non-interactive Job CLI | `dispatch/cli_job.py`, `dispatch/__main__.py` | `dispatch/job_ops.py` | `tests/test_job_cli.py` |
| SQL/job preview | `dispatch/screens/preview.py` | `dispatch/sql.py`, `dispatch/manifest.py` | `tests/test_qa_fixes.py`, `tests/test_ui_ux_closure.py` |
| Confirmation dialogs | `dispatch/screens/confirm.py` | Calling screen | `tests/test_ui_ux_closure.py` |
| Job logs, follow/search, cancel | `dispatch/screens/job_detail.py` | `dispatch/job_ops.py`, `dispatch/jobs.py`, `dispatch/errors.py`, `dispatch/formatting.py` | `tests/test_cockpit.py`, `tests/test_qa_fixes.py`, `tests/test_job_cli.py` |
| Older completed jobs | `dispatch/screens/history.py` | `dispatch/jobs.py`, `dispatch/formatting.py` | `tests/test_ui_ux_closure.py`, `tests/test_production_polish.py` |
| Impala metadata browser and DROP confirmation | `dispatch/screens/browser.py` | `dispatch/impala.py` | `tests/test_ui_ux_closure.py`, `tests/test_production_polish.py` |

## Domain and process owners

| Concern | Owner |
|---|---|
| Manifest schema, legal source/destination cells, persistence | `dispatch/manifest.py` |
| Job queries, reconciliation, launch-slot locking and cap | `dispatch/jobs.py` |
| Shared launch/cancel/list/show/logs/wait (TUI + CLI) | `dispatch/job_ops.py` |
| Non-interactive Job CLI adapter | `dispatch/cli_job.py` |
| SQL parsing, templates, destination naming | `dispatch/sql.py` |
| Kerberos TTL parsing and checks | `dispatch/kerberos.py` |
| Async and interactive subprocess gateways | `dispatch/process.py` |
| Detached job launch and orchestrator argv | `dispatch/runner.py` |
| Impala metadata commands | `dispatch/impala.py` |
| Error classification | `dispatch/errors.py` |
| State, elapsed-time, Kerberos, and log presentation | `dispatch/formatting.py` |
| Data root, config, installed version paths | `dispatch/config.py` |

There is no `dispatch/models.py`. Job types and legal combinations live in
`dispatch/manifest.py`; lifecycle and launch-slot behavior live in
`dispatch/jobs.py`.

## High-risk routes

- For any `scr/` edit, read `docs/adr/0005-scr-modification-policy.md` and test
  every file under `mocks/scenarios/`. Do not broaden the public CLI, email
  contract, queue list, or retry policy.
- For durability or detached-process changes, read ADR-0001 plus
  `dispatch/process.py`, `dispatch/runner.py`, `dispatch/manifest.py`, and
  `dispatch/jobs.py` before editing.
- For CSV destinations or `Table+Csv`, read ADR-0003 plus `dispatch/sql.py`,
  `dispatch/manifest.py`, and `dispatch/runner.py`.
- For mock behavior, read ADR-0004, `mocks/dev-env.sh`, `mocks/bin/`, and the
  selected scenario JSON.
