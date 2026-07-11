---
title: "Rule catalog: which manual guidelines become machine-checkable rules"
labels: [wayfinder:grilling]
status: closed
assignee: cursor-agent
blocked-by: []
---

## Question

Which guidelines from the
[Impala optimization manual](../assets/impala-optimization-manual.md) become
rules the advisor actually checks, and at what severity?

Grill through the manual's checklist with the sponsor, sorting each guideline
into: **rule** (statically checkable against the SQL text of a Job),
**needs-metadata rule** (checkable only with table stats / EXPLAIN — the
metadata-availability research resolved these to **out of v1**; record them
as parked, not as catalog entries), or **not a rule** (already out of scope
on the map, or unverifiable). For each accepted rule, record severity
(error / warning / info) and the exact detection condition. The known-table
join-strategy rule stays static: it reads the manual's recommended table,
embedded as data per the metadata research.

Candidate rules visible from a first read, to seed the grilling:

- `SELECT *` from a `core.*` table without a `dw_process_date` filter
- Missing partition-column filter entirely (no `dw_process_date` in WHERE)
- Function applied to a partition column (breaks partition pruning)
- Date range wider than 13 months in one query
- Join against a table in the manual's broadcast/shuffle table without the
  recommended hint, or with the wrong hint
- `[BROADCAST]` hint on a known-large table (e.g. `cut_clear_dtl_enc`)
- Large known table joined unfiltered (filters not pushed into the
  subquery/CTE before the join)
- Leading-wildcard `LIKE '%...'`, `REGEXP` where `LIKE` suffices
- `UNION` where `UNION ALL` likely suffices; `SELECT DISTINCT` instead of
  `GROUP BY`; `COUNT(DISTINCT ...)` on core tables
- Comma-join / missing join condition (Cartesian product risk)
- `CAST` inside a join condition (blocks runtime-filter pushdown)
- Note: Dispatch already generates `DROP TABLE IF EXISTS` + employee-ID
  `LOCATION` paths itself (`dispatch/sql.py: table_wrapper`), so Guidelines
  #2/#5/#6 may be satisfied by construction rather than checked.

The answer is the locked rule catalog: rule id, guideline reference,
detection condition, severity, and whether it needs metadata.

## Resolution

Grilled with the sponsor on 2026-07-10; the locked catalog is
[the rule catalog asset](../assets/rule-catalog.md). Eighteen rules ship in
v1: four error (`SELECT *` unfiltered on a monitored table, >13-month
literal ranges, `BROADCAST` on a shuffle-recommended table, Cartesian
products), six warning (missing partition filter, function-wrapped partition
column, `SHUFFLE` on a broadcast-recommended table, destination-table
naming, and the two self-contained-DDL checks for `DROP TABLE IF EXISTS`
and employee-ID `LOCATION`), and eight info (missing hint, direct
large-table join, `CAST` in join conditions, and the
string/set-operation/aggregation style rules). Severity encodes
confidence × impact. Monitored schemas, per-table partition columns (with a
`dw_process_date` default for unlisted tables), and join-strategy
recommendations all live in the embedded data file. `table_wrapper`
guarantees G#2/G#6 only on the wrapped path — the self-contained-DDL path
and G#5 naming get real rules (R16–R18). Environment-hygiene items and
needs-metadata checks are recorded as not-a-rule or parked. `SqlTemplate`
Jobs are analyzed once on the template text; `ExistingTable` Jobs are not
analyzed.
