# Notebook queries are eagerly submitted Jobs backed by a snapshot SQL file

Analysts want to run SQL written in a notebook cell (`d.sql("SELECT …")`)
rather than in a `.sql` file. Dispatch writes that Inline SQL to a file it owns
inside the Notebook workspace and launches an ordinary `SqlFile` Job through
`dispatch job launch`. `sql()` submits immediately and returns the `Job`, so
laziness and chained transformations are not part of the API.

Nothing about the Job model changes: `manifest.create_job` already snapshots
the SQL to `<job_dir>/job.sql` and the orchestrator runs that snapshot, so a
file has always been the executed artifact. Inline SQL only changes who authors
the file.

## Considered options

- **A fourth `Source` type, `SqlText`.** Rejected. `Source` is a documented
  product concept with a legal `(Source, Destination)` matrix
  (`manifest.LEGAL_CELLS`), and a new member would ripple into the argv
  builders, the TUI's New Job form, the Advisor's form rules, and every place
  that renders a Source — all to describe a file whose author happens to be a
  notebook. The Job that runs is byte-for-byte a `SqlFile` Job.
- **A `dispatch job launch --sql-text -` flag reading SQL from stdin.**
  Deferred, not rejected. It would remove the intermediate file, but the file
  is not a workaround: it is the provenance record that `manifest`'s
  `sql_path_at_launch` points at, and it survives after the launch so
  `dispatch job show` stays truthful. Revisit if a caller appears that cannot
  write to the workspace.
- **Lazy queries, like duckdb's relations or a pyspark DataFrame.** Rejected.
  Laziness in those tools exists so an engine can fuse transformations before
  planning; Dispatch has nothing to fuse — it hands one SQL text to one
  orchestrator. Laziness would only delay validation, Kerberos, capacity, and
  Advisor refusals until a later line, and it would need a second object
  alongside `Job`, which already owns `wait`/`watch`/`logs`/`cancel`.
- **A separate blocking verb (`d.query(text) -> DataFrame`).** Rejected as
  redundant: `d.sql(text).to_df()` is the same line, and one verb keeps one
  mental model — naming data submits a Job.

## Consequences

- `d.sql(...)` consumes one of the two launch slots even if its Result is never
  read. `Job.cancel()` is the remedy; `wait_for_slot` makes the cap wait
  instead of refusing.
- Reading a table is `d.table("schema.table", limit=…)`, not a chained
  `.limit()`, because there is no pre-submit object to chain onto. Without
  `limit` the Job is `ExistingTable → Csv` and exports the whole table (the
  orchestrator's `select * from …`); with `limit` Dispatch generates
  `SELECT * FROM <validated table> LIMIT <int>` and launches `SqlFile → Csv`.
- That generated SQL is analysed by the Advisor like any other SQL, so an
  unfiltered peek at a monitored schema (`core`, `gco`, `mrs`) can be refused
  until acknowledged, while the unbounded `ExistingTable` export cannot be —
  the Advisor only sees SQL text. This inversion is deliberate: Dispatch does
  not silently acknowledge findings on the Analyst's behalf.
- Notebook Jobs appear in the TUI dashboard and telemetry like any other Job.
  That is the point: a notebook is not a side channel.
