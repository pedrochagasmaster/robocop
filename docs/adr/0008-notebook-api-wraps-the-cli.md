# The notebook API wraps the CLI instead of importing the domain layer

`dispatch/notebook.py` lets an **Analyst** launch and monitor **Jobs** from a
Jupyter notebook on the **Edge Node**. Every operation runs
`dispatch job … --json` in a subprocess and parses the one JSON document the
CLI writes to stdout. The module contains no Job behavior of its own: no
validation, no capacity admission, no manifest writes, no runner handoff.

```
notebook.Dispatch ──subprocess──> dispatch job … --json ──> job_ops ──> manifest / runner
```

Exit codes become exceptions (`2` → `UsageError`, `3` → `UnknownJobError`,
`4` → `OperationalError`), the launch confirmation the CLI spells `--yes` is
the act of calling `launch()`, and the Advisor error gate stays intact:
the first `launch()` of SQL with error-severity **Findings** raises, and
`acknowledge_advisor=True` is the notebook spelling of
`--acknowledge-advisor`.

`Job.wait()` polls `dispatch job show --json` rather than blocking inside
`dispatch job wait`. Polling keeps the kernel interruptible, lets `watch()`
redraw state and the log tail between polls, and costs one short-lived process
per poll (~0.25 s of CPU, no telemetry event).

## Considered alternatives

- **Import `dispatch.job_ops` directly.** Rejected as the primary seam. It
  would run capacity admission, manifest writes, and runner handoff inside the
  kernel process, so an interrupted or restarted kernel could abandon a
  half-admitted launch, and long-lived kernels would hold the launch-slot
  ledger's expectations about short-lived processes. It would also make the
  notebook surface depend on internal function signatures rather than on the
  documented CLI contract, and Kerberos/Advisor/telemetry behavior would have
  to be re-wired by hand in the adapter.
- **Tell analysts to use `!dispatch job …` shell escapes.** Rejected as
  sufficient: it works, but every notebook then re-implements JSON parsing,
  exit-code handling, polling, and log tailing. That duplicated glue is exactly
  what this module removes.
- **A separate distribution (`dispatch-notebook` on an internal index).**
  Rejected: there is no internal package index in the deployment story
  (ADR-0007 ships one shared runtime), and a wrapper versioned separately from
  the CLI it wraps would drift.
- **Return raw dicts instead of `Job` objects.** Rejected: monitoring is the
  point, and `job.watch()` / `job.wait()` / `job.logs()` need somewhere to
  live. `Job.to_dict()` and `JobList.to_dicts()` keep the raw payload
  available for `pandas`.

## Consequences

- The CLI's JSON payloads and exit codes are now a public contract with a
  second consumer. `tests/test_notebook_api.py` builds the real
  `dispatch job` argparse parser and feeds it the argv the library emits, so
  renaming a flag fails in CI rather than in a notebook.
- `dispatch job logs` has no `--json` mode, so `Job.logs()` returns text. Log
  output is orchestrator prose; it is not parsed.
- The library is stdlib-only plus an optional IPython import. Without IPython
  it still works: rich HTML output degrades to printed status lines, so the
  same code runs in scripts and cron.
- `Dispatch(cwd=…)` is the CLI's invocation directory, which is what decides
  where CSV results land (ADR-0003). Changing `cwd` between calls is not
  supported; construct another `Dispatch`.
- A Job that ran and failed is data, not an exception: `wait()` returns
  `Failed` and `Cancelled` Jobs. Only refusals raise.
