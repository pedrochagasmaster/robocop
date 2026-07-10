---
title: "Decide: the embedded join-strategy data file"
labels: [wayfinder:grilling]
status: open
assignee: none
blocked-by: [0001-rule-catalog]
---

## Question

The
[metadata availability research](0003-metadata-availability-research.md)
replaced live `SHOW TABLE STATS` checks with the manual's fixed recommended
join-strategy table (Guideline #3's CORE/GCO/MRS list), embedded as data
inside `dispatch/`. Pin down that data file:

- **Location and format** — a checked-in file under `dispatch/` (JSON? a
  Python literal module?) that ships in the wheel; it must survive the
  edge-deploy bundle path and be readable without new dependencies.
- **Content shape** — schema/table key, recommended strategy
  (broadcast/shuffle), and whatever the rule catalog's known-table rule needs
  (severity override? manual section reference for the finding text?). Note
  the manual keys every row by database, some rows cover name variants
  (`_hsh`/`_enc`), and the same table name can carry different
  recommendations per database (`aggregate_merchant`: BROADCAST in CORE,
  Shuffle in MRS), so keys must be schema-qualified.
- **Update procedure** — the Code Optimization Team revises the
  recommendation list; document how a revision lands (edit the data file in a
  normal PR) and how users see which list version their findings used.

Blocked by the rule catalog because the file's shape follows from the exact
detection condition of the known-table join-strategy rule — and if grilling
drops that rule, this file is not needed at all.
