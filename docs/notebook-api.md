# Notebook API

`dispatch.notebook` runs and monitors Jobs from a Jupyter notebook (or any
Python script) on the Edge Node. It wraps the `dispatch job` CLI: same
validation, Kerberos checks, Advisor gates, capacity limits, manifests, and
detached runners, reached through Python objects instead of argv and exit
codes. See [ADR-0008](adr/0008-notebook-api-wraps-the-cli.md) for why it is a
wrapper rather than a second entry point into the domain layer.

## Quick start

```python
from dispatch.notebook import Dispatch

d = Dispatch(cwd="~/sql")            # where the SQL lives and CSVs land

job = d.launch(
    source="SqlFile",
    destination="Csv",
    sql="monthly_report.sql",
    table="report",
)
job.watch()                          # live state + log tail, refreshed in place

job.succeeded                        # True
job.csv_path                         # '/home/eid/sql/report.csv'
```

Reading the result is ordinary pandas:

```python
import pandas as pd

df = pd.read_csv(job.csv_path)
```

## Session

```python
d = Dispatch(cwd=None, command=None, env=None, timeout=None)
```

| Argument | Meaning |
|---|---|
| `cwd` | Directory the CLI runs in: relative `sql` paths resolve here and CSV results are written here (ADR-0003). Defaults to the notebook's working directory. |
| `command` | CLI invocation. Defaults to `$DISPATCH_CLI` when set, else `python -m dispatch` using the kernel's interpreter. |
| `env` | Extra environment variables for the CLI, e.g. `{"DISPATCH_MOCK_SCENARIO": "slow"}` in development. |
| `timeout` | Seconds any single CLI command may take before `OperationalError`. |

If the kernel cannot import `dispatch`, point the library at the installed
launcher instead: `export DISPATCH_CLI=~/.local/bin/dispatch`.

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

| Exception | Raised when | CLI exit code |
|---|---|---|
| `UsageError` | Invalid inputs, failed launch validation, unacknowledged Advisor errors | 2 |
| `UnknownJobError` | Unknown, malformed, or unsafe Job ID | 3 |
| `OperationalError` | Kerberos, capacity, handoff, cancellation, unusable CLI, command timeout | 4 |
| `JobUnsuccessful` | A command reported the Job completed unsuccessfully | 1 |
| `WaitTimeout` | `wait()` / `watch()` deadline passed; also a `TimeoutError` | — |

All inherit from `DispatchError` and carry `exit_code`, `stderr`, and `argv`.

## Local development

The library needs the same mock layer as the TUI:

```bash
source mocks/dev-env.sh
jupyter lab
```

`demos/notebook_job_api.ipynb` is a runnable tour of the API against the mocks.
