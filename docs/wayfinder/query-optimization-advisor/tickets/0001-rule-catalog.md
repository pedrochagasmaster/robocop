---
title: "Rule catalog: which manual guidelines become machine-checkable rules"
labels: [wayfinder:grilling]
status: open
assignee: none
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
