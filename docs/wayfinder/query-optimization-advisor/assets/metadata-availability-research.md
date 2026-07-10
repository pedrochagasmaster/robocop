# Metadata availability research

Research date: 2026-07-09

## Decision

Ship the v1 advisor **static-only**: no `EXPLAIN`, no `SHOW TABLE STATS`, no
live metadata calls during Job composition. Embed the manual's recommended
join-strategy table (CORE/GCO/MRS tables and their broadcast/shuffle
recommendations) as data inside `dispatch/`, and classify the rule catalog's
needs-metadata rules as **out of v1**.

Live metadata verification is feasible through existing plumbing and remains a
credible later enhancement behind an explicit user action, but it fails all
three of this ticket's viability tests for v1: latency and UX cost, failure
modes during composition, and offline mock support that does not exist today.

## 1. Existing plumbing: could Dispatch run EXPLAIN and SHOW TABLE STATS?

Mechanically, yes. `dispatch/impala.py` exposes an async `query()` helper that
runs any statement through `impala-shell` with a 30-second timeout via the
sanctioned subprocess gateway (`dispatch/process.py: run_exec`). `show_tables`
and `describe_table` are thin wrappers over it; `explain(sql)` and
`show_table_stats(full_table)` would be the same shape, and
`validate_full_table` already guards identifier injection for the stats call.

Two statement-shape problems make `EXPLAIN` harder than it looks:

- **Wrapped SQL is multi-statement.** A `SqlFile -> Table` Job launches
  `DROP TABLE IF EXISTS ...;` followed by `CREATE TABLE ... AS SELECT ...`
  (`dispatch/sql.py: table_wrapper`). `EXPLAIN` takes a single statement, so
  the advisor would have to explain the inner `SELECT` (or the `CREATE ... AS`
  statement alone), not the SQL Dispatch actually launches.
- **Templates are unrendered at composition time.** A `SqlTemplate` Job still
  contains `{date_inicio}`/`{date_fim}` when the user is in the New Job
  screen. `EXPLAIN` would need one rendered expansion first, and a plan for
  one month does not prove anything about the other expansions.

`SHOW TABLE STATS` has a scoping constraint: it is only meaningful for the
Job's **source** tables. The destination table usually does not exist yet —
the Job creates it — and stats freshness for created tables (`COMPUTE STATS`
after creation) is a post-run concern, adjacent to the post-run PROFILE loop
the map already rules out of scope.

## 2. What the mock layer would need to grow

Today the answer is "everything". The mock `impala-shell` routes only
`SHOW TABLES`, `DESCRIBE`, and `DROP TABLE` to its metadata handler
(`mocks/bin/impala-shell: route_sql`); anything else is a `DATA_QUERY` and
falls through to scenario dispatch. A probe against the current mock confirms
it:

| Query | `happy_path` behavior | Error-scenario behavior |
|---|---|---|
| `EXPLAIN SELECT ...` | Generic "Mock impala-shell ... succeeded" line — no plan text | Scenario error, exit 1 |
| `SHOW TABLE STATS t` | Same generic success line — no stats rows | Scenario error, exit 1 |
| `SHOW COLUMN STATS t` | Same | Same |

So metadata-backed rules are untestable offline until the mock grows:

- new `route_sql` tags (`EXPLAIN`, `SHOW_TABLE_STATS`) plus
  `handle_schema_query` branches emitting realistic fixtures (plan text with
  `partitions=X/Y` lines, pipe-delimited stats rows with `#Rows` and `Size`);
- per-table fixture variation (a small broadcast-suitable table versus a
  large shuffle table) so rules have something to discriminate;
- scenario coverage for stats-missing (`#Rows` of -1 after no
  `COMPUTE STATS`) and stale-stats cases;
- contract tests in `tests/test_mock_contract.py` locking the new routing.

That is a meaningful mock investment (ADR-0004 makes it mandatory, not
optional), and it is only worth paying once the rules that consume the
fixtures are in scope.

## 3. Latency and UX cost

The Textual skill requires all Impala interaction to run through workers or
async-safe helpers and never block the event loop; spinners appear after a
short delay; operations must be cancellable. The existing patterns are:

- `BrowserScreen` awaits `impala.*` inside explicit user actions ("Load
  Tables", "Describe") with inline loading text — the user asked for a
  metadata call and waits for it.
- `NewJobScreen` validation (`_validation_issues`) runs synchronously on every
  form change; it reads only local state (files, Kerberos TTL snapshot, job
  counts). Nothing in the live validation path performs I/O to Impala.

A 30-second-timeout call has no acceptable home in that live validation loop:
typing a table name would fire network calls against a production coordinator,
and a slow or hung call would leave stale findings racing fresh keystrokes.
If live metadata ever ships, the only viable surface is an **explicit
"Analyze" action** — worker plus spinner plus cancel, following the launch
flow's `run_worker(..., exclusive=True)` pattern — which is exactly the
dedicated-surface option already on the TUI prototype ticket's list. That
conclusion holds regardless of v1 scope, so it is recorded here for the
prototype ticket to inherit.

## 4. Failure modes

Every failure the mock scenarios model applies to composition-time metadata
calls: `auth_error` (no Kerberos principal), `all_queues_full` (admission
timeout), `connection_error`, `timeout`, `table_not_found` (a `SqlTemplate`
month table or the Job's own destination). Two structural mitigations exist —
the app already tracks a reactive `kerberos_ttl` snapshot the advisor could
consult before attempting any call, and `impala.query()` raises actionable
errors — but the product requirement stands regardless: **the advisor must
degrade to static-only analysis and never block composition** when metadata is
unavailable. A user with no ticket must still be able to compose, get static
findings, press `K` to kinit, and launch.

Static-only v1 satisfies this trivially: there is nothing to degrade.

## 5. The static alternative is sufficient for v1

The manual's broadcast/shuffle guidance is already mostly static:

- Guideline #3 ships a **fixed recommended join-strategy table** for named
  CORE/GCO/MRS tables. Embedding that table as data answers "is this known
  table joined with the recommended hint?" with zero metadata calls.
- Guideline #4 ("check size, below 1 GB use broadcast") applies to **custom
  tables** and genuinely needs `SHOW TABLE STATS`. That check — and
  EXPLAIN-based partition-pruning verification — are the parked
  needs-metadata rules.

The engine research already established that the analysis layer can locate
join hints and the joined table names statically, so the embedded table is
directly consumable. The map's fog item about updating the recommendation
table when the Code Optimization Team revises it becomes a data-file update,
not a code change.

## Consequences for the rest of the map

- **Rule catalog**: classify Guideline #4 size checks, partition-pruning
  verification via EXPLAIN, and stats-freshness checks as needs-metadata,
  out of v1. The known-table join-strategy rule stays in, backed by the
  embedded table.
- **TUI prototype**: no worker/spinner surface is required for v1 analysis
  (static analysis of files this small is fast); the dedicated Analyze action
  remains the designated future home for metadata-backed checks.
- **Testing/mocks fog**: v1 needs no new `impala-shell` mock routing; the
  mock growth list in section 2 becomes part of the future metadata effort.
- **Spec assembly**: the spec should state the static-only boundary and the
  embedded join-strategy data file (location, format, update procedure).

## Sources

- `dispatch/impala.py`, `dispatch/process.py` — existing metadata plumbing.
- `dispatch/screens/browser.py`, `dispatch/screens/new_job.py` — current UX
  patterns for metadata calls and live validation.
- `mocks/bin/impala-shell`, `mocks/scenarios/` — probe of current mock
  behavior for `EXPLAIN` / `SHOW TABLE STATS` (2026-07-09).
- `.agents/skills/dispatch-textual-tui/SKILL.md` — worker and event-loop
  rules.
- [Impala optimization manual](impala-optimization-manual.md) — Guidelines #3
  and #4, EXPLAIN/PROFILE sections.
- [SQL analysis engine research](sql-analysis-engine-research.md) — static
  analysis capabilities the embedded table relies on.
