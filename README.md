# Dispatch

Dispatch is a server-side tool for launching Impala Jobs from the Hadoop Edge
Node. Users `ssh` to the Edge Node, `cd` to the directory containing their SQL
files, and either open the interactive Textual TUI (`dispatch`) or drive Jobs
non-interactively with `dispatch job …`. Detached runners own execution so Jobs
survive terminal disconnects.

## What changed in v1.0

- The legacy Windows GUI is removed.
- Jobs are described by on-disk manifests under `/ads_storage/<user>/.dispatch/jobs/`.
- The TUI and CLI supervise Jobs by reading manifests and logs; the detached runner owns Orchestrator script execution.
- CSV results are written uncompressed to the launch-time working directory.
- A local mock layer supports development without Hadoop, Kerberos, SMTP, or `/ads_storage/`.

## Install and onboard

Once per Edge Node, the Release Operator activates the verified dependency
bundle in the shared runtime:

```bash
./install.sh
```

The installer is non-interactive. It builds or reuses an immutable runtime
under `.venv/releases/<bundle-digest>/` and atomically activates `.venv/current`.
It does not change any analyst's files. Each analyst then runs:

```bash
/ads_storage/dispatch/onboard.sh
```

Onboarding creates or repairs private configuration, jobs, telemetry, and the
thin `~/.local/bin/dispatch` launcher. It never creates a venv or runs pip.

For the full first-time remote setup flow, including what to upload to the server and how `vendor/` is used, see [docs/edge-node-first-time-setup.md](docs/edge-node-first-time-setup.md).

For the short end-user setup flow after the shared tree is deployed, see
[onboarding.md](onboarding.md).

For local development on a non-Hadoop machine, use the project environment:

```bash
source mocks/dev-env.sh
python -m pip install -e ".[dev]"
python -m dispatch
```

Contributors should use [CONTRIBUTING.md](CONTRIBUTING.md) for setup,
validation, commit, and release handoff.

Normal development ends with a GitHub pull request. Release Operators use
[docs/release-workflow.md](docs/release-workflow.md).

## Run (interactive TUI)

```bash
cd /path/to/sql/files
dispatch
```

With no subcommand, `dispatch` (and `python -m dispatch`) open the Textual TUI.
Dispatch captures the launch-time CWD once. CSV destinations are resolved
relative to that directory for the entire session.

## Run (non-interactive CLI)

Automation and shell users can launch and supervise Jobs without opening the
TUI. The CLI reuses the same validation, Kerberos checks, Advisor gates,
capacity admission, manifest persistence, telemetry, and detached-runner
handoff as the TUI. It never instantiates Textual screens.

```bash
cd /path/to/sql/files

# Launch (requires --yes; add --acknowledge-advisor when Advisor reports errors)
dispatch job launch --source SqlFile --destination Csv --sql query.sql --table report --yes
dispatch job launch --source SqlTemplate --destination Table \
  --sql monthly.sql --schema aa_enc --table monthly_out \
  --start-date 2026-01-01 --end-date 2026-01-31 --yes
dispatch job launch --source ExistingTable --destination Csv \
  --existing-table aa_enc.events_existing --yes

# Supervise
dispatch job list
dispatch job list --state Running --json
dispatch job show JOB_ID --json
dispatch job logs JOB_ID --lines 100
dispatch job logs JOB_ID --follow
dispatch job wait JOB_ID --timeout 600 --json
dispatch job cancel JOB_ID --yes
```

`python -m dispatch job …` exposes the identical interface.

### Launch flags

| Flag | Meaning |
|---|---|
| `--source` | `SqlFile`, `SqlTemplate`, or `ExistingTable` |
| `--destination` | `Table`, `Csv`, or `Table+Csv` (must be a legal cell) |
| `--sql` | SQL file path; relative paths resolve against the invocation CWD |
| `--existing-table` | `schema.table` for ExistingTable → Csv |
| `--schema` | Destination schema (default `aa_enc`) |
| `--table` | Destination table suffix; the analyst EID prefix is applied |
| `--start-date` / `--end-date` | SqlTemplate date range (`YYYY-MM-DD`) |
| `--email` / `--subject` | Notification recipients and subject |
| `--queue` | Resource Pool selection: `auto` (default) or comma-separated Impala pools (`params.queue`) |
| `--yes` | Required confirmation substitute for the TUI launch dialog |
| `--acknowledge-advisor` | Required when Advisor reports error-severity findings |
| `--json` | One JSON document on stdout; diagnostics on stderr |

### JSON examples

```bash
dispatch job launch --source SqlFile --destination Csv --sql q.sql --table out --yes --json
# {"job_id": "20260727T120000Z_abc123", "state": "Pending", "pid": null}

dispatch job list --json
# {"jobs": [{"id": "...", "state": "Running", "source": "SqlFile", ...}]}

dispatch job wait JOB_ID --json
# {"job_id": "...", "state": "Succeeded", "exit_code": 0, "timed_out": false}
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Successful command (and `wait` when the Job Succeeded) |
| `1` | Job completed unsuccessfully or was Cancelled (`wait`) |
| `2` | Invalid invocation, missing `--yes` / Advisor ack, or launch validation failure |
| `3` | Unknown, malformed, or unsafe Job ID |
| `4` | Operational refusal/failure (Kerberos/capacity/timeout/handoff/cancel) |

Argparse usage errors also exit `2`.

### Safety

- Launches and cancellations never proceed silently: pass `--yes`.
- Advisor error findings additionally require `--acknowledge-advisor`.
- Job paths that are missing, corrupt, symlink-escaped, or outside the jobs root are rejected.

## Run (Python notebooks)

`dispatch.notebook` wraps the same CLI for analysts working in Jupyter on the
Edge Node. Each call shells out to `dispatch job … --json`, so notebooks
inherit the CLI's validation, Kerberos checks, Advisor gates, capacity limits,
and detached runners.

```python
from dispatch.notebook import Dispatch

d = Dispatch(cwd="~/sql")

# SQL written in the notebook, loaded into a DataFrame
df = d.sql("SELECT dt, count(*) AS c FROM aa_enc.events GROUP BY dt").to_df()
d.table("aa_enc.events", limit=1000).to_df()

# or a Job from a file, watched while it runs
job = d.launch(source="SqlFile", destination="Csv", sql="query.sql", table="report")
job.watch()                       # live state and log tail until the Job is terminal
job.succeeded                     # True
job.to_df()                       # the Result, as a DataFrame

d.jobs(state="Running")           # supervise everything else
```

Every call is a real Job — validation, Kerberos, Advisor gate, two-Job cap,
manifest, detached runner — so expect minutes, not milliseconds. Refusals raise
(`UsageError`, `UnknownJobError`, `OperationalError`); a Job that ran and failed
is returned like any other. Loading a Result needs pandas, which is optional:
`rows()` works with the standard library alone.

Tutorial: [demos/notebook_job_api.ipynb](demos/notebook_job_api.ipynb).
Full reference: [docs/notebook-api.md](docs/notebook-api.md). Design rationale:
[ADR-0008](docs/adr/0008-notebook-api-wraps-the-cli.md),
[ADR-0009](docs/adr/0009-notebook-queries-are-eagerly-submitted-jobs.md),
[ADR-0010](docs/adr/0010-notebook-results-are-strict-reads-of-the-job-csv.md),
[ADR-0011](docs/adr/0011-no-unaudited-interactive-query-tier.md).

Operators may set `DISPATCH_IMPALA_MONITOR_SEED_URL` to a validated Impala
coordinator base URL for explicit query-identity recovery. This optional seed
does not bypass host validation. HTTPS remains the default; plaintext HTTP is
still restricted to the existing development/mock opt-in.

## Usage telemetry

Dispatch records offline usage events (sessions, screens, Job launches, refusals)
so operators can see who is using it and how. Events are JSONL under each user's
`~/.dispatch/telemetry/` and, when writable, the shared rollup at
`/ads_storage/dispatch/telemetry/users/<user>.jsonl`. No network calls; opt out
with `DISPATCH_TELEMETRY=0`. Writes use a bounded background queue so telemetry
storage delays never block the TUI or Job lifecycle.

```bash
dispatch telemetry who --days 30
dispatch telemetry summary --days 30
```

## Jobs

A Job combines exactly one Source and one Destination.

| Source | Table | Csv | Table + Csv |
|---|---|---|---|
| `SqlFile` | yes | yes | yes |
| `SqlTemplate` | yes | no | no |
| `ExistingTable` | no | yes | no |

The TUI and CLI hard-refuse illegal cells, missing Kerberos tickets, tickets with less than five minutes remaining, and more than two simultaneously Running/Pending Jobs.

## Orchestrator scripts

Dispatch reuses the production-tested scripts in `scr/`:

- `Query_Impala_Parametrized.py`
- `download_to_csv.py`
- `monthly_query_processor.py`

The runner decomposes `Table + Csv` into table creation followed by a separate CSV export. It never uses the old combined create-and-compress path.

## Mock development

```bash
source mocks/dev-env.sh
export DISPATCH_MOCK_SCENARIO=happy_path
python -m dispatch
# or: python -m dispatch job launch --source SqlFile --destination Csv --sql demo.sql --yes
```

Available scenarios:

- `happy_path`
- `all_queues_full`
- `memory_exceeded`
- `syntax_error`
- `auth_error`
- `slow`

Captured emails are written to `mocks/sent_emails/` and are ignored by git.

## Validation limits

The mock layer covers local behavior. Before production rollout, Release
Operators still need to validate the shared runtime and one bundle-free analyst
onboarding on the real `/ads_storage` mount, confirm Kerberos client output,
compare M10 against production `impala-shell`, and deploy artefacts to
`/ads_storage/dispatch/`.
