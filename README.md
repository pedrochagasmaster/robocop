# Dispatch

Dispatch is a server-side TUI for launching Impala Jobs from the Hadoop Edge Node. Users `ssh` to the Edge Node, `cd` to the directory containing their SQL files, run `dispatch`, and launch Jobs that survive terminal disconnects.

## What changed in v1.0

- The legacy Windows GUI is removed.
- Jobs are described by on-disk manifests under `/ads_storage/<user>/.dispatch/jobs/`.
- The TUI supervises Jobs by reading manifests and logs; the detached runner owns Orchestrator script execution.
- CSV results are written uncompressed to the launch-time working directory.
- A local mock layer supports development without Hadoop, Kerberos, SMTP, or `/ads_storage/`.

## Install

On the Edge Node, from the deployed `/ads_storage/dispatch/` tree:

```bash
./install.sh
```

The installer is idempotent. Re-running it preserves `config.json` and `jobs/`, refreshes the per-user venv, updates `installed_version`, and keeps the `dispatch` shortcut pointed at the current install.

For the full first-time remote setup flow, including what to upload to the server and how `vendor/` is used, see [docs/edge-node-first-time-setup.md](docs/edge-node-first-time-setup.md).

For the short end-user setup flow after the shared tree is deployed, see
[onboarding.md](onboarding.md).

For local development on a non-Hadoop machine:

```bash
source mocks/dev-env.sh
DISPATCH_EMAIL=you@example.com DISPATCH_PYTHON_BIN=$(command -v python3) ./install.sh
dispatch
```

Contributors should use [CONTRIBUTING.md](CONTRIBUTING.md) for setup,
validation, commit, and release handoff.

Normal development ends with a GitHub pull request. Release Operators use
[docs/release-workflow.md](docs/release-workflow.md).

## Run

```bash
cd /path/to/sql/files
dispatch
```

Dispatch captures the launch-time CWD once. CSV destinations are resolved relative to that directory for the entire session.

## Usage telemetry

Dispatch records offline usage events (sessions, screens, Job launches, refusals)
so operators can see who is using it and how. Events are JSONL under each user's
`~/.dispatch/telemetry/` and, when writable, the shared rollup at
`/ads_storage/dispatch/telemetry/users/<user>.jsonl`. No network calls; opt out
with `DISPATCH_TELEMETRY=0`.

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

The TUI hard-refuses illegal cells, missing Kerberos tickets, tickets with less than five minutes remaining, and more than two simultaneously Running Jobs.

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

The mock layer covers local behavior. Before production merge, reviewers still need to smoke-test Textual over the corporate ssh chain, run `install.sh` against the real `/ads_storage/<user>/` mount, confirm Kerberos client output, compare M10 against production `impala-shell`, and deploy artefacts to `/ads_storage/dispatch/`.
