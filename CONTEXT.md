# Dispatch

A tool for executing SQL queries against a remote Apache Impala/Hadoop cluster.
Originally a Windows GUI that prepared SSH commands (the **legacy GUI**); now
redesigned as a server-side TUI that runs on the Hadoop edge node itself,
invoked via the `dispatch` command. The legacy GUI is hard-deleted at v1.0.

## Language

**Source**:
The kind of SQL a **Job** is built from. One of `SqlFile` (a single `.sql` file
holding one `SELECT`), `SqlTemplate` (a `.sql` file holding
`{date_inicio}`/`{date_fim}` placeholders, executed once per month over a date
range), or `ExistingTable` (no SELECT — data is already materialised in Impala).
_Avoid_: query type, mode, workflow.

**Destination**:
Where a **Job's** result is materialised. One of `Table` (a new parquet Impala
table at `/das/<schema-prefix>/enc/<user>/<table>`), `Csv` (an uncompressed
`.csv` written to the user's launch-time working directory), or `Table + Csv`
(both, table first). See ADR-0003 for the CSV-output rules.
_Avoid_: output, target, sink.

**Job**:
One end-to-end execution: a `(Source, Destination)` pair plus the parameters
needed to run it (schema, table name, recipient email, SQL file path, …).
Composed of one or more calls to **Orchestrator scripts**.
_Avoid_: run, execution, task.

**Orchestrator script**:
One of the three production-tested Python scripts in `scr/`:
`Query_Impala_Parametrized.py`, `download_to_csv.py`,
`monthly_query_processor.py`. Owns the queue-cycling retry loop and email
notifications. Treated as a black box by the TUI.
_Avoid_: backend, runner, engine.

**Resource Pool**:
An Impala queue managing concurrent query resources. Orchestrators cycle
through a fixed list (`adhoc_fast`, `acs_small`, `adhoc_small`, `acs_large`,
`adhoc`) until one accepts the query.
_Avoid_: queue, fila.

**Edge Node**:
The gateway server providing access to the Hadoop cluster. Hosts the
orchestrator scripts and (in the new design) the TUI itself.

**Analyst**:
A person who runs Dispatch on an **Edge Node** to launch and supervise
**Jobs**. Owns private configuration, jobs, logs, and telemetry under
`/ads_storage/<user>/.dispatch/`; never installs dependencies or runs pip.
_Avoid_: user (ambiguous with the Unix account), end user, operator.

**Release Operator**:
The person who deploys Dispatch to an **Edge Node** and manages the shared
runtime: delivering verified dependency bundles, running `install.sh`, and
activating or rolling back runtimes (see ADR-0007). Distinct from an
**Analyst**, who only runs `onboard.sh` and the TUI.
_Avoid_: admin, deployer, maintainer.

**Advisor**:
The pre-launch static analysis feature that checks a **Job's** SQL against
the Impala optimization manual and reports **Findings**. Flag-only: it never
modifies or gates what SQL is launched beyond the error-confirm modal. See
[docs/query-optimization-advisor-spec.md](docs/query-optimization-advisor-spec.md).
_Avoid_: linter, optimizer, rewriter.

**Finding**:
One **Advisor** detection — a rule id, guideline reference, severity
(`error` / `warning` / `info`), diagnostic detection line, and typed
remediation line.
_Avoid_: violation, issue, error (reserved for the severity tier).

**Job state**:
The lifecycle stage of a **Job**. One of `Running` (orchestrator process is
alive), `Succeeded` (orchestrator exited 0), `Failed` (orchestrator exited
non-zero, or hit a fatal classified error such as `SYNTAX_ERROR`,
`TABLE_NOT_FOUND`), or `Cancelled` (user killed it from the TUI). `Pending`
exists only briefly between launch click and the runner spawning the
orchestrator and is not exposed in the dashboard.
_Avoid_: status, phase.

**Inline SQL**:
SQL authored in a notebook cell rather than in a `.sql` file the **Analyst**
keeps. It produces an ordinary `SqlFile` **Job**; the file it runs from belongs
to Dispatch, not to the **Analyst**.
_Avoid_: query string, ad hoc query, snippet.

**Result**:
The rows a completed **Job** produced, read back into Python from the CSV its
**Destination** wrote. A **Job** whose **Destination** is `Table` has no
**Result**.
_Avoid_: output, dataset, result set.

**Notebook workspace**:
The Dispatch-owned directory that holds **Inline SQL** and **Results** for
**Jobs** launched from a notebook, so an **Analyst's** working directory only
ever receives files they asked for by name.
_Avoid_: scratch, temp directory, cache.

## Relationships

- A **Job** has exactly one **Source** and exactly one **Destination**.
- A **Job** whose **Destination** includes `Csv` has exactly one **Result**;
  a `Table`-only **Job** has none.
- A **Job** invokes one or more **Orchestrator scripts** in sequence.
- A user may have at most **two Jobs in `Running` state simultaneously**.
  Launching a third while two are running is hard-refused; it does not queue.
- A **Job** in a terminal state (`Succeeded`/`Failed`/`Cancelled`) remains
  visible in the active dashboard for **seven days**, after which it
  auto-collapses into a separate history view (it is never deleted).
- Legal `(Source, Destination)` combinations:

  | Source ↓ / Destination → | Table | Csv | Table + Csv |
  |---|---|---|---|
  | `SqlFile`        | ✓ | ✓ | ✓ |
  | `SqlTemplate`    | ✓ | — | — |
  | `ExistingTable`  | — | ✓ | — |

- A `SqlTemplate` **Job** is restricted to `Destination = Table`: monthly
  partitioned outputs are intended for downstream analytical use, not bulk CSV
  export.

## Flagged ambiguities

- The existing PowerShell GUI's `Mode` field
  (`Create`/`Download`/`QueryAndDownload`/`MonthlyJob`) conflated **Source** and
  **Destination**. Resolved: split into the two orthogonal axes above.
- The "Auto-generate DROP/CREATE" checkbox in the existing GUI is no longer a
  user-facing concept; it is implicit when `Destination` contains `Table`.
- "Queue" / "fila" in the orchestrator scripts refers to a **Resource Pool**.
