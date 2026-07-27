# Notebook API

`dispatch.notebook` runs and monitors Jobs from a Jupyter notebook (or any
Python script) on the Edge Node. It wraps the `dispatch job` CLI: same
validation, Kerberos checks, Advisor gates, capacity limits, manifests, and
detached runners, reached through Python objects instead of argv and exit
codes. See [ADR-0008](adr/0008-notebook-api-wraps-the-cli.md) for why it is a
wrapper rather than a second entry point into the domain layer.

This page is the reference. If you are new to running Dispatch from a notebook,
work through the tutorial first:
[`demos/notebook_job_api.ipynb`](../demos/notebook_job_api.ipynb) covers the
same ground in order, with runnable cells.

## Quick start

```python
from dispatch.notebook import Dispatch

d = Dispatch(cwd="~/sql")            # where your .sql files live

# SQL written here, loaded straight into a DataFrame
df = d.sql("SELECT dt, count(*) AS c FROM aa_enc.events GROUP BY dt").to_df()

# or a Job from one of your files, watched while it runs
job = d.launch(source="SqlFile", destination="Csv", sql="monthly_report.sql", table="report")
job.watch()                          # live state + log tail, refreshed in place
job.succeeded                        # True
job.to_df()                          # the same Result loading works here
```

Every one of those calls is a full Job: same validation, Kerberos check,
Advisor gate, two-Job cap, manifest, run log, and detached runner as the TUI.
There is no faster unaudited path ([ADR-0011](adr/0011-no-unaudited-interactive-query-tier.md)),
so expect minutes, not milliseconds.

## Session

```python
d = Dispatch(cwd=None, command=None, env=None, timeout=None, workspace=None)
```

| Argument | Meaning |
|---|---|
| `cwd` | Directory the CLI runs in: relative `sql` paths resolve here and CSVs for Jobs you name land here (ADR-0003). Defaults to the notebook's working directory. |
| `command` | CLI invocation. Defaults to `$DISPATCH_CLI` when set, else `python -m dispatch` using the kernel's interpreter. |
| `env` | Extra environment variables for the CLI, e.g. `{"DISPATCH_MOCK_SCENARIO": "slow"}` in development. |
| `timeout` | Seconds any single CLI command may take before `OperationalError`. |
| `workspace` | Notebook workspace for Inline SQL and Results. Defaults to `<data_root>/.dispatch/notebook`. |

If the kernel cannot import `dispatch`, point the library at the installed
launcher instead: `export DISPATCH_CLI=~/.local/bin/dispatch`.

## Running SQL written in the notebook

```python
job = d.sql(
    "SELECT * FROM aa_enc.events WHERE dt = '2026-07-01'",
    destination="Csv",       # Csv (default), Table, or Table+Csv
    table=None,              # required for Table destinations; names the table
    queue="auto",
    wait_for_slot=300,       # wait up to 5 min for a launch slot instead of failing
)
```

`sql()` saves the text as Inline SQL in the Notebook workspace and launches it
as an ordinary `SqlFile` Job. It submits immediately and returns the `Job`, so
`d.sql(...)` alone starts work and `d.sql(...).to_df()` runs and fetches in one
line. There is no lazy relation to chain onto, because Dispatch has no query
planner to defer to ([ADR-0009](adr/0009-notebook-queries-are-eagerly-submitted-jobs.md)).

Materialise a table instead of fetching rows:

```python
d.sql("SELECT ...", destination="Table", table="weekly_agg").wait()
```

Run a monthly template over a date range:

```python
d.sql(
    "SELECT ... WHERE dt BETWEEN '{date_inicio}' AND '{date_fim}'",
    source="SqlTemplate",
    destination="Table",
    table="monthly_out",
    start_date="2026-01-01",
    end_date="2026-03-31",
)
```

Read an existing table without writing SQL:

```python
d.table("aa_enc.events")             # whole table: ExistingTable -> Csv
d.table("aa_enc.events", limit=1000) # SELECT * FROM aa_enc.events LIMIT 1000
```

Without `limit` the orchestrator exports the whole table, so prefer `limit` for
a peek. The limited form is generated SQL and the Advisor analyses it, which
means an unfiltered peek at a monitored schema (`core`, `gco`, `mrs`) can be
refused until you pass `acknowledge_advisor=True`.

## Loading Results

A **Result** is the CSV a Job's `Csv` Destination wrote. Any Job with a CSV
Destination has one, whether it came from a notebook, the CLI, or the TUI.

| Call | Behavior |
|---|---|
| `job.to_df()` | The Result as a `pandas.DataFrame` (`to_pandas()` is the same call). |
| `job.rows()` | The Result as `list[dict[str, str]]`, with every line's field count checked. |
| `job.columns` | Column names. |
| `job.result_path` | Where the Result is on disk, or `None` for a `Table`-only Job. |
| `job.to_csv(path)` | Copy the Result to a path or directory you name. |

These wait for the Job when it is still running, and raise `JobUnsuccessful`
when it Failed or was Cancelled. `to_df()` takes `pandas.read_csv` keywords:

```python
job.to_df(dtype={"account_id": "string"}, parse_dates=["dt"], nrows=10_000)
```

### The export is not quoted

`scr/download_to_csv.py` exports with impala-shell's `--delimited` mode, which
writes a header and comma-separated fields but never quotes or escapes them. A
string value containing a comma or a newline therefore produces an ambiguous
CSV, and pandas does not complain: a long line silently becomes an index
column, a short one is padded with `NaN`.

Dispatch refuses to guess. Reads disable quote handling, and `to_df()` scans the
file first, raising `ResultParseError` with the offending line number. Pass
`strict=False` to skip that scan on a very large Result and accept pandas'
behavior. Bad lines are never skipped. If your string columns can contain the
delimiter, clean it in SQL (`regexp_replace(col, ',', ' ')`) rather than
post-processing the DataFrame ([ADR-0010](adr/0010-notebook-results-are-strict-reads-of-the-job-csv.md)).

CSV also carries no types, so dtypes are whatever pandas infers; `dtype=` is
there when that is not good enough.

### pandas is optional

Runtime dependencies are `textual` and `sqlglot`. If the shared runtime has no
pandas, `to_df()` explains what to ask the Release Operator for, and
`rows()`/`columns` keep working with the standard library alone.

## The Notebook workspace

Inline SQL and the Results of `sql()`/`table()` go to a directory per query
under `<data_root>/.dispatch/notebook/`, not to `cwd`, so a notebook never
litters the directory your `.sql` files live in.

```python
d.workspace                      # PosixPath('/ads_storage/eid/.dispatch/notebook')
job.result_path                  # .../notebook/nb_9f2c1a_3d0b/nb_9f2c1a.csv
d.cleanup()                      # remove query directories older than 7 days
d.cleanup(older_than_days=0)     # remove all of them
```

Results are durable until you clean them up: a detached Job's artifacts are
meant to outlive the kernel. Use `job.to_csv(path)` for anything you want to
keep by name.

## Capacity

Only two Jobs may be Pending or Running at once. A third launch raises
`OperationalError` immediately; `wait_for_slot=<seconds>` on `sql()`, `table()`,
or `launch()` retries until a slot frees or the deadline passes. Only capacity
refusals are retried — a Kerberos or validation refusal surfaces at once.

## Launching

```python
job = d.launch(
    source="SqlFile",          # SqlFile | SqlTemplate | ExistingTable
    destination="Csv",         # Table | Csv | Table+Csv
    sql="query.sql",           # path, absolute or relative to cwd
    existing_table=None,       # 'schema.table' for ExistingTable -> Csv
    schema="aa_enc",
    table="report",            # table suffix; Table destinations get the EID prefix
    start_date="2026-01-01",   # SqlTemplate range; date objects also accepted
    end_date="2026-03-31",
    email="analyst@example.com",
    subject="Monthly report",
    queue="auto",              # 'auto', a pool name, or a list of pools
    acknowledge_advisor=False,
)
```

Only `source` and `destination` are required; omitted arguments keep the CLI's
defaults. Calling `launch()` is the confirmation the CLI spells `--yes`, and
the two-Job concurrency cap still applies.

The Advisor gate is preserved. SQL with error-severity findings raises
`UsageError` naming the rules; re-run the same call with
`acknowledge_advisor=True` to launch it as written:

```python
try:
    job = d.launch(source="SqlFile", destination="Table", sql="wide.sql", table="wide")
except UsageError as exc:
    print(exc)   # Advisor reported error-severity findings (R09). …
    job = d.launch(
        source="SqlFile", destination="Table", sql="wide.sql", table="wide",
        acknowledge_advisor=True,
    )
```

## Monitoring

| Call | Behavior |
|---|---|
| `job.watch()` | Refresh state and the log tail in place until the Job is terminal. |
| `job.wait(timeout=600)` | Block until terminal; raises `WaitTimeout` if the deadline passes first. |
| `job.refresh()` | Re-read the reconciled manifest once. |
| `job.logs(lines=100)` | The last N log lines as text. |
| `job.print_logs(follow=True)` | Stream the log until the Job is terminal. |
| `job.stream_logs()` | The same stream as an iterator of lines. |
| `job.cancel()` | Cancel a Pending or Running Job. |

Runners are detached, so interrupting the kernel during `watch()` or `wait()`
stops watching, not the Job. Call `d.job(job_id).watch()` to pick it back up,
including from a different notebook.

Job state is data: `wait()` returns Failed and Cancelled Jobs like successful
ones. Read `job.succeeded`, `job.failed`, `job.cancelled`, `job.state`,
`job.exit_code`, `job.elapsed_seconds`, `job.csv_path`, `job.params`, and
`job.manifest`. Only refusals raise.

## Listing

```python
d.jobs()                    # every Job, rendered as a table in Jupyter
d.jobs(state="Running")     # filtered
d.job("20260727T120000Z_ab12cd")

import pandas as pd
pd.DataFrame(d.jobs().to_dicts())
```

## Errors

Refused commands raise a `DispatchError`, which carries `exit_code`, `stderr`,
and `argv`:

| Exception | Raised when | CLI exit code |
|---|---|---|
| `UsageError` | Invalid inputs, failed launch validation, unacknowledged Advisor errors | 2 |
| `UnknownJobError` | Unknown, malformed, or unsafe Job ID | 3 |
| `OperationalError` | Kerberos, capacity, handoff, cancellation, unusable CLI, command timeout | 4 |
| `JobUnsuccessful` | Reading the Result of a Job that Failed or was Cancelled | 1 |
| `WaitTimeout` | `wait()` / `watch()` deadline passed; also a `TimeoutError` | — |

Results that cannot be read raise a `ResultError` instead, because nothing was
refused — the Job ran, and its export is the problem:

| Exception | Raised when |
|---|---|
| `MissingResultError` | The Job has no CSV Destination, or its Result file is gone |
| `ResultParseError` | A line's field count disagrees with the header, or pandas cannot parse the export |

## Platform

Launching needs a POSIX host: `dispatch job launch` hands the Job to
`nohup setsid python -m dispatch.runner`, which is how a Job survives a
disconnected terminal. That covers the Edge Node and Linux development. On
Windows the library imports and every read-only call works, but a launch fails
at handoff, so the launch-dependent tests are skipped there.

## Local development

The library needs the same mock layer as the TUI:

```bash
source mocks/dev-env.sh
jupyter lab
```

`demos/notebook_job_api.ipynb` is the tutorial: every cell runs against the
mocks, start to finish, in about twenty seconds. Set `DISPATCH_MOCK_DELAY` to a
few seconds before starting Jupyter if you want Jobs slow enough to watch.
