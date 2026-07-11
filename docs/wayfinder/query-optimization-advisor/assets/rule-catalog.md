# Query Optimization Advisor rule catalog

Locked with the sponsor on 2026-07-10 (grilling session, ticket
[Rule catalog](../tickets/0001-rule-catalog.md)). Rule ids were renumbered at
assembly; the grilling transcript's working numbers do not carry over.

## Severity semantics

Severity encodes confidence and impact together:

- **error** — high-confidence detection of a pattern the manual forbids
  outright, with severe cluster impact (or that the platform will reject
  anyway).
- **warning** — the pattern is probably a problem, but has legitimate
  exceptions the analyzer cannot see.
- **info** — stylistic or heuristic guidance where the author may well know
  better.

Enforcement (whether any tier blocks a launch) is the
[enforcement policy ticket](../tickets/0005-enforcement-policy.md)'s decision,
not this catalog's.

## Shared definitions

- **Monitored schemas** — a data-file list of schema names, initially `core`,
  `gco`, `mrs`, matched case-insensitively against the schema qualifier.
  Unqualified table references never match (they are usually the user's own
  temp tables).
- **Partition columns** — the data file maps `schema.table` to its partition
  column(s). A monitored-schema table without an entry defaults to
  `dw_process_date`: a missed check is invisible, while a false warning is
  visible and self-correcting (someone adds the entry).
- **Known table / recommendation** — an entry in the embedded join-strategy
  table (manual Guideline #3; file shape is the
  [join-strategy data file ticket](../tickets/0008-join-strategy-data-file.md)).
  "Shuffle-recommended" tables double as the catalog's "known-large" set.
- **Query block** — one SELECT scope in the AST. Predicates "reference" a
  table when they use its alias or columns within that block.
- **Hints** — the engine adapter records bracket-form (`[BROADCAST]`) and
  comment-form (`/* +BROADCAST */`) hints with source spans; a join hint binds
  to the table reference immediately following the `JOIN` keyword.

## Remediation guidance

Locked by the
[remediation guidance ticket](../tickets/0004-advisory-vs-rewrite.md)
(2026-07-10). Every finding has a fixed two-part shape, plus its rule id and
guideline reference (e.g. `R02 · Guideline #1`):

1. **Detection line — always diagnostic and factual.** States exactly what
   the analyzer did or didn't find, named to the table/block/span ("No
   `dw_process_date` predicate found for `core.cut_clear_dtl_enc` in this
   query block"). Diagnostic phrasing stays true even when the analyzer is
   wrong; a command would not.
2. **Remediation line — typed per rule.** **Imperative step** where the fix
   is deterministic without guessing intent (the data file or username
   supplies the content): R02, R03, R04, R06, R07, R16, R17, R18.
   **Alternative-naming** where the fix is the author's call — name the
   alternative and why, never a hedged command: R01, R05, R08, R09, R10,
   R11, R12, R13, R14, R15. The quoted wording angles under each rule below
   are that alternative-naming content.

Full manual excerpts stay out of finding text (80x24 terminals); whether a
detail view shows them is the
[surface prototype ticket](../tickets/0006-tui-surface-prototype.md)'s call.

## Rules

| Id | Rule | Guideline | Severity |
|---|---|---|---|
| R01 | `select-star-unfiltered` | §3, G#10 | error |
| R02 | `missing-partition-filter` | G#1, §8 | warning |
| R03 | `function-on-partition-column` | §8 dates | warning |
| R04 | `date-range-over-13-months` | G#8 | error |
| R05 | `missing-join-hint` | G#3 | info |
| R06 | `wasteful-join-hint` | G#3 | warning |
| R07 | `dangerous-broadcast-hint` | G#3/#4 | error |
| R08 | `large-table-joined-directly` | G#9 | info |
| R09 | `cartesian-product` | §8 joins | error |
| R10 | `cast-in-join-condition` | §4 | info |
| R11 | `leading-wildcard-like` | §8 strings | info |
| R12 | `regexp-predicate` | §8 strings | info |
| R13 | `union-distinct` | §8 | info |
| R14 | `select-distinct` | §8 | info |
| R15 | `count-distinct-on-monitored-table` | §8 aggregates | info |
| R16 | `destination-table-naming` | G#5 | warning |
| R17 | `ddl-missing-drop` | G#6 | warning |
| R18 | `ddl-location-outside-user-dir` | G#2 | warning |

### R01 `select-star-unfiltered` (error)

Fires only for monitored-schema tables, evaluated per table: the query
block's projection is bare `*` (or `t.*` where `t` is a monitored-schema
table) **and** the block's `WHERE` contains no predicate referencing that
monitored table. A bare `*` over only unmonitored tables (e.g. the user's
own temp tables) never fires. `JOIN ... ON` conditions do not count as
filters.
`LIMIT` never suppresses the finding (the manual explicitly shows `LIMIT 10`
as bad). `SELECT *` over an already-filtered subquery or CTE does not fire —
that is the manual's own recommended pattern.

If the block has *any* predicate on the table (even non-partition), R01 stays
silent and R02 speaks to the partition-filter gap.

Reporting note: when R01 fires for a table/block, suppress R02 for the same
table/block — both conditions hold by definition and one finding per cause is
enough.

### R02 `missing-partition-filter` (warning)

Fires when a monitored-schema table appears in a query block and **no
recognized partition column of that table** (per the data file, with the
`dw_process_date` default) is referenced anywhere in the block's `WHERE`.
A template predicate like `BETWEEN '{date_inicio}' AND '{date_fim}'` counts
as present — the tokens sit inside string literals. Warning because the
filter may legitimately arrive indirectly (via a view or join the analyzer
cannot see through).

### R03 `function-on-partition-column` (warning)

Fires when a recognized partition column appears **only** wrapped in a
function or `CAST` in the block's `WHERE`. A function on the literal side
(`dw_process_date = some_fn(...)`) is fine; a bare predicate on the same
column elsewhere in the block silences the rule (pruning still works). R02
and R03 are disjoint: no reference at all → R02; wrapped-only → R03; bare
present → neither.

### R04 `date-range-over-13-months` (error)

Fires when a predicate on a recognized partition column has two literal
`YYYY-MM-DD` bounds — `BETWEEN 'a' AND 'b'`, or a lower/upper bound pair on
the same column in the same block using `>=`/`>` and `<=`/`<` (the manual's
own recommended idiom is `>= '2023-12-01' AND < '2024-01-01'`) — and the
bounded span exceeds 13 calendar months. Non-literal or one-sided bounds
produce no finding.
`SqlTemplate` Jobs naturally pass: each monthly expansion spans one month,
which *is* the manual's prescribed "break into sub parts".

### R05 `missing-join-hint` (info)

Fires when a known table is joined with no hint. Impala's optimizer usually
chooses correctly when stats exist; this is advice to follow the manual's
recommended list, not a defect.

### R06 `wasteful-join-hint` (warning)

Fires when a broadcast-recommended table is joined with a `SHUFFLE` hint.
Wasteful (needless network partitioning of a small table) but not dangerous.

### R07 `dangerous-broadcast-hint` (error)

Fires when a shuffle-recommended table (e.g. `core.cut_clear_dtl_enc`) is
joined with a `BROADCAST` hint. Broadcasting a multi-TB table replicates it
to every node — the most cluster-hostile mistake in the catalog. Confidence
is total: the hint is explicit and the table is explicitly listed.

### R08 `large-table-joined-directly` (info)

Fires when a shuffle-recommended table appears as a bare table reference on
**either side** of a join — the `FROM` base position counts, since the
manual's G#9 examples filter the big table wherever it sits — rather than
wrapped in a subquery/CTE. Worded as a "consider pre-filtering in a
subquery/CTE" nudge (G#9). Info because Impala's predicate pushdown and
runtime filters often make the direct join fine — the manual's own §4
Scenario 1 joins `CORE.auth_dtl_enc` directly.

### R09 `cartesian-product` (error)

Fires on either of two AST shapes (verified against SQLGlot 30.12.0, which
does **not** normalize them into one): (a) a `CROSS` join node — comma-style
and the explicit keyword both produce it — or (b) a join node whose `ON`
condition is absent or a dialect-synthesized constant `TRUE` (how the Hive
dialect parses `JOIN b` with no `ON`/`USING`). Either shape fires only when
the block's `WHERE` contains no equality predicate linking columns from both
sides.
Old-style `FROM a, b WHERE a.id = b.id` stays silent. Deliberate tiny cross
joins will occasionally be flagged; loud-but-ignorable is the right trade in
a flag-only advisor.

### R10 `cast-in-join-condition` (info)

Fires when an explicit `CAST` wraps a column reference inside a
`JOIN ... ON` equality. Worded "can block runtime-filter pushdown; align
source types where possible" — the manual's own auth examples do this when
types genuinely differ, hence info.

### R11 `leading-wildcard-like` (info)

Fires when a `LIKE` pattern literal starts with `%` or `_`. "Forces a full
scan; anchor the pattern or use `IN`."

### R12 `regexp-predicate` (info)

Fires on a `REGEXP`/`RLIKE` operator in any predicate. "`LIKE` may suffice."

### R13 `union-distinct` (info)

Fires on any `UNION` without `ALL`. "Adds deduplication overhead; use
`UNION ALL` if duplicates are acceptable." Whether duplicates matter is the
author's call — which is exactly why this is info.

### R14 `select-distinct` (info)

Fires on a `DISTINCT` projection. "The manual prefers `GROUP BY`."

### R15 `count-distinct-on-monitored-table` (info)

Fires when `COUNT(DISTINCT ...)` appears in a block referencing a
monitored-schema table. "Use two-step aggregation on large tables." Scoped to
monitored schemas to avoid nagging about small temp tables.

### R16 `destination-table-naming` (warning)

Fires when a Job's destination table name (the New Job form field, not the
SQL text) does not start with `<user>_`, where `<user>` is the launching
user's id. Applies to every Job whose destination creates a table (`Table` /
`Table+Csv`), on all Sources. `table_wrapper` does **not** enforce G#5's
employee-ID prefix — the name is user-supplied and only validated as a plain
identifier — so this is a genuine check, not construction. Warning because
team or shared naming conventions legitimately differ.

### R17 `ddl-missing-drop` (warning)

Scope: self-contained DDL `SqlFile` Jobs only (files `table_wrapper` does not
wrap — on the wrapped path G#6 is satisfied by construction). Fires when a
`CREATE TABLE` statement has no preceding `DROP TABLE IF EXISTS` for the same
table earlier in the file.

### R18 `ddl-location-outside-user-dir` (warning)

Scope: self-contained DDL `SqlFile` Jobs only (same reasoning as R17 — the
wrapped path generates the employee-ID `LOCATION` by construction). Fires
when a `CREATE TABLE` statement's `LOCATION` clause is absent, or its path
does not contain the launching user's id as a segment.

## Per-Source applicability

- **`ExistingTable`** — no SELECT exists; the advisor does not run. (Any UI
  treatment is the surface prototype's decision.) R16 cannot apply either:
  `ExistingTable` legally pairs only with `Csv`, which creates no table.
- **`SqlTemplate`** — analyze the template text **once**, pre-render: the
  tokens sit inside string literals, so the AST is identical across monthly
  expansions. R04 auto-passes (one month per expansion). A token outside a
  string literal makes analysis unavailable per the engine research's adapter
  contract.
- **`SqlFile`** — analyze the user's SQL as it sits on disk, **before**
  `table_wrapper` wraps it (the wrapper is generated and satisfied by
  construction). Self-contained DDL files are analyzed as written, per
  statement for multi-statement files.

## Not a rule

| Guideline | Reason |
|---|---|
| G#2 `LOCATION` and G#6 `DROP TABLE IF EXISTS` on the wrapped path | Satisfied by construction — `dispatch/sql.py: table_wrapper` generates both when it wraps a `SqlFile -> Table` SELECT. On the self-contained-DDL path they are **not** guaranteed and are checked by R17/R18; G#5 naming is never generated and is checked by R16 |
| G#7 quarterly file cleanup; G#11–#15 ports/kernels/processes/environment | Out of scope on the map — not properties of the Job's SQL |
| G#9 as a shape requirement | Softened to R08 (info); its enforceable core is R02 + R06/R07, and a hard subquery-shape rule would contradict §4's runtime-filter guidance |
| GROUP BY cardinality ordering | Needs row counts (metadata) |
| Data-type narrowing (TINYINT/SMALLINT) | Cannot judge intent statically |
| CASE ordering by frequency | Needs data distribution |
| Join order smallest-first | Impala reorders joins unless `STRAIGHT_JOIN` is present |
| NULL-handling patterns | `COALESCE` misuse too niche; `IS NOT NULL` advice too noisy |
| Repeated CASE / same-partition window functions | The manual's own example is common, legitimate SQL |
| Split-path vs CASE restructuring | Architectural advice, not a checkable predicate |

## Parked: needs-metadata (out of v1)

Per the
[metadata availability research](metadata-availability-research.md):
G#4 custom-table size check (`SHOW TABLE STATS`), EXPLAIN-based
partition-pruning verification, and stats freshness. A future metadata effort
behind an explicit Analyze action may revisit them.

## Data file requirements (feeds ticket 0008)

The embedded data file must carry, per entry: `schema.table`, recommended
join strategy (broadcast/shuffle), and partition column(s). Plus the
monitored-schema list. Consumed by R01–R08 and R15. (R16–R18 need only the
launching user's id, which Dispatch already has — no data-file input.)
