# No unaudited interactive query tier

Dispatch will not grow a fast, non-Job query path for notebooks. Every query an
Analyst runs — including a ten-row peek — is a Job with a manifest, a run log,
telemetry, an Advisor analysis, and a launch slot.

This is a deliberate no, because the obvious optimisation is right there:
`dispatch/impala.py` already runs `impala-shell` directly with a 30-second
timeout under a metadata capacity lease, and the Browse screen parses its
delimited stdout. Exposing it to notebooks (as `d.peek(...)`, or by importing
`impala` into the kernel) would make `SELECT … LIMIT 100` answer in seconds
instead of minutes, which is exactly what a duckdb or pyspark user expects.

We are not doing it yet for three reasons:

- **Audit.** A Job's manifest and run log are how an Analyst, a Release
  Operator, or a future investigator reconstructs what ran against the cluster.
  A second path that leaves no manifest makes "what did this notebook do?"
  unanswerable.
- **The Advisor.** The gate that stops unfiltered scans of monitored schemas
  reads SQL on the launch path. A peek tier bypasses it by construction, and
  "it's only a LIMIT" is exactly the argument that precedes an accidental
  full-table scan.
- **It would be starved anyway.** The capacity ledger gives launches priority
  over metadata leases (`capacity.try_acquire_metadata` refuses while a launch
  waits), so peeks would fail precisely when the Analyst is busiest.

`d.table("schema.table", limit=100)` covers the same need within the Job model:
slower, audited, and Advisor-visible.

Revisit this if peek latency becomes the reason analysts stop using Dispatch.
The price of admission then is a manifest-or-equivalent audit record, a
telemetry event, an enforced `LIMIT`, and an explicit decision about the
Advisor — not a convenience import.
