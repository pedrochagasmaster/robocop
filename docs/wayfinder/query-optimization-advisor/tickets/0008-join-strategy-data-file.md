---
title: "Decide: the embedded join-strategy data file"
labels: [wayfinder:grilling]
status: closed
assignee: cursor-agent
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

The locked
[rule catalog](../assets/rule-catalog.md) broadened this file beyond join
strategies (see its "Data file requirements" section): it must also carry the
monitored-schema list (`core`, `gco`, `mrs` initially) and per-table
partition column(s), with `dw_process_date` as the default for unlisted
monitored-schema tables.

## Resolution

Grilled with the sponsor on 2026-07-10. The data ships as a **Python
module**, `dispatch/advisor_data.py`, holding plain literals — it rides the
wheel automatically (no `package-data` config, no loader, no runtime
parsing), is type-checkable, and its update procedure (a reviewed PR) is
identical to what a JSON file would need anyway.

Content shape:

- Keys are exact lowercase `"schema.table"` strings, fully expanded from the
  manual's Guideline #3 table: slash variants become separate entries
  (`core.cut_clear_dtl_hsh` / `core.cut_clear_dtl_enc`), multi-database rows
  one entry per schema (`core.product_hierarchy` / `gco.product_hierarchy`),
  and same-named tables stay distinct per schema (`core.aggregate_merchant`
  broadcast vs `mrs.aggregate_merchant` shuffle). No pattern matching in the
  data.
- Values are a small immutable record: `join_strategy`
  (`"broadcast"`/`"shuffle"`) and optional `partition_columns` override.
- Module-level globals: `MONITORED_SCHEMAS` and
  `DEFAULT_PARTITION_COLUMN = "dw_process_date"` (the catalog's two shared
  definitions), plus `DATA_VERSION` (date string) and the manual version
  transcribed from (`v2.0`).
- Seeded verbatim from Guideline #3 at implementation time; revisions land
  as ordinary reviewed PRs with git history as the changelog, documented in
  the module docstring. Whether the TUI displays `DATA_VERSION` is the
  surface prototype's call.

Blocked by the rule catalog because the file's shape follows from the exact
detection condition of the known-table join-strategy rule — and if grilling
drops that rule, this file is not needed at all.
